# -*- coding: utf-8 -*-
"""System One 异步客户端。

相对旧实现的关键差异：

- 全程异步，不再用 ``urllib`` 阻塞事件循环。
- 复用一条 aiohttp 长连接：实测单次往返从 ~900ms 降到 ~280ms，这是 JEV 能进
  低延迟判断链的前提。
- 用信号量约束并发，避免同一时刻大量判断把上游打满后集体变慢。
- 端点族由 :mod:`domains.decision.jev_endpoint` 解析，Vercel 网关与 TypeSafe
  官方直连共用同一协议实现。
- 失败不再静默：返回结构化错误，供上层做熔断和诊断。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .jev_endpoint import (
    JevEndpointProfile,
    credential_hint,
    resolve_profile,
)
from .jev_primitives import (
    JevAnswer,
    JevQuestionError,
    choice_question,
    noul_question,
    parse_answers,
    score_question,
    usage_of,
)

logger = logging.getLogger("astrbot_plugin_private_companion.jev_engine")

DEFAULT_TIMEOUT_SECONDS = 1.6
DEFAULT_MAX_CONCURRENCY = 4
MAX_STATE_CHARS = 4000


@dataclass
class JevResult:
    """Outcome of one System One evaluation."""

    ok: bool = False
    answers: dict[str, JevAnswer] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    model: str = ""
    error: str = ""
    status: int = 0

    @property
    def answer(self) -> JevAnswer | None:
        """Convenience accessor when exactly one question was asked."""
        if len(self.answers) == 1:
            return next(iter(self.answers.values()))
        return None

    def get(self, question_id: str) -> JevAnswer | None:
        return self.answers.get(question_id)


class JevClient:
    """Async System One client with a reused connection pool."""

    def __init__(
        self,
        *,
        api_key: str = "",
        kind: str = "auto",
        gateway_url: str = "",
        model: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = max(0.15, float(timeout or DEFAULT_TIMEOUT_SECONDS))
        self.max_concurrency = max(1, int(max_concurrency or DEFAULT_MAX_CONCURRENCY))
        self.profile: JevEndpointProfile = resolve_profile(
            kind=kind,
            api_key=self.api_key,
            gateway_url=gateway_url,
            model=model,
        )
        self._session: Any = None
        self._session_lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def reconfigure(
        self,
        *,
        api_key: str = "",
        kind: str = "auto",
        gateway_url: str = "",
        model: str = "",
        timeout: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        """Apply new settings and rebuild pool guards when their shape changes."""
        new_key = str(api_key or "").strip()
        new_profile = resolve_profile(
            kind=kind,
            api_key=new_key,
            gateway_url=gateway_url,
            model=model,
        )
        target_changed = (
            new_profile != self.profile or new_key != self.api_key
        )
        new_concurrency = (
            self.max_concurrency
            if max_concurrency is None
            else max(1, int(max_concurrency))
        )
        concurrency_changed = new_concurrency != self.max_concurrency
        self.api_key = new_key
        self.profile = new_profile
        self.max_concurrency = new_concurrency
        if timeout is not None:
            self.timeout = max(0.15, float(timeout))
        if target_changed or concurrency_changed:
            self._drop_session()
        if concurrency_changed:
            self._semaphore = None

    def _drop_session(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                asyncio.get_running_loop().create_task(session.close())
            except RuntimeError:
                # No running loop: the session belongs to a dead loop.
                pass

    async def aclose(self) -> None:
        """Close the pooled connection; safe to call repeatedly."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass

    async def _ensure_session(self) -> Any:
        try:
            import aiohttp
        except Exception as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(f"aiohttp 不可用：{exc}") from exc

        loop = asyncio.get_running_loop()
        if self._session is not None and self._loop is loop and not self._session.closed:
            return self._session
        if self._session_lock is None or self._loop is not loop:
            # Loop changed (AstrBot reload): an aiohttp session is bound to the
            # loop that created it and must never be returned on the new loop.
            old_session, self._session = self._session, None
            self._session_lock = asyncio.Lock()
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
            self._loop = loop
            if old_session is not None and not old_session.closed:
                try:
                    await old_session.close()
                except Exception:
                    pass
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            connector = aiohttp.TCPConnector(
                limit=self.max_concurrency + 2,
                keepalive_timeout=120,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            return self._session

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        state: Any,
        questions: Mapping[str, Any],
        *,
        model: str = "",
        timeout: float | None = None,
    ) -> JevResult:
        """Evaluate ``questions`` against ``state`` in one request.

        ``timeout`` bounds the whole call, queueing included, because callers
        like the message-debounce path hand JEV a budget taken from the user's
        reply latency rather than from the network.
        """
        if not self.is_configured:
            return JevResult(error="not_configured")
        if not questions:
            return JevResult(error="no_questions")

        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, default=str)
        state_text = str(state_text or "").strip()
        if not state_text:
            return JevResult(error="empty_state")
        if len(state_text) > MAX_STATE_CHARS:
            state_text = state_text[:MAX_STATE_CHARS]

        payload = {
            "model": str(model or self.profile.model),
            "state": state_text,
            "questions": dict(questions),
        }
        effective_timeout = max(0.15, float(timeout if timeout is not None else self.timeout))
        started = time.perf_counter()
        try:
            status, body = await asyncio.wait_for(
                self._post(payload, effective_timeout),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            return JevResult(elapsed_ms=self._elapsed_ms(started), error="timeout")
        except Exception as exc:
            return JevResult(
                elapsed_ms=self._elapsed_ms(started),
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )

        elapsed_ms = self._elapsed_ms(started)
        if status != 200:
            return JevResult(
                ok=False,
                status=status,
                elapsed_ms=elapsed_ms,
                error=self._describe_http_error(status, body),
            )
        try:
            data = json.loads(body)
        except Exception:
            return JevResult(ok=False, status=status, elapsed_ms=elapsed_ms, error="invalid_json")
        answers = parse_answers(data)
        if not answers:
            return JevResult(ok=False, status=status, elapsed_ms=elapsed_ms, error="empty_answers")
        return JevResult(
            ok=True,
            answers=answers,
            usage=usage_of(data),
            elapsed_ms=elapsed_ms,
            model=str(data.get("model") or payload.get("model") or ""),
            status=status,
        )

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
        """Perform the HTTP round trip on the pooled session."""
        import aiohttp

        await self._ensure_session()
        semaphore = self._semaphore
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.max_concurrency)
            self._semaphore = semaphore
        async with semaphore:
            session = self._session
            if session is None or session.closed:
                raise RuntimeError("jev session unavailable")
            async with session.post(
                self.profile.url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                return response.status, await response.text()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _describe_http_error(self, status: int, body: str) -> str:
        """Turn a failure into something a user can act on."""
        detail = ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = str(
                    parsed.get("message")
                    or parsed.get("error_type")
                    or parsed.get("detail")
                    or ""
                )
        except Exception:
            detail = str(body or "")[:160]
        detail = detail[:200]
        hint = ""
        if status == 401:
            hint = "凭据被拒绝：" + (credential_hint(self.profile, self.api_key) or "请检查 API Key 是否属于当前端点。")
        elif status == 400 and "model" in detail.lower():
            hint = f"模型名 '{self.profile.model}' 不被当前端点接受，请检查 JEV 模型配置。"
        elif status == 404:
            hint = f"端点路径不存在，请检查网关地址：{self.profile.safe_url()}"
        elif status == 429:
            hint = "上游限流。"
        return f"HTTP {status} {detail}".strip() + (f"（{hint}）" if hint else "")

    # ------------------------------------------------------------------
    # 便捷封装
    # ------------------------------------------------------------------

    async def noul(
        self,
        state: str,
        instructions: str,
        *,
        true_hint: str = "",
        false_hint: str = "",
        question_id: str = "should_reply",
        timeout: float | None = None,
    ) -> JevResult:
        try:
            question = noul_question(instructions, true_hint=true_hint, false_hint=false_hint)
            return await self.evaluate(state, {question_id: question}, timeout=timeout)
        except JevQuestionError as exc:
            return JevResult(error=f"question_error: {exc}")

    async def choice(
        self,
        state: str,
        instructions: str,
        criteria: Mapping[str, str],
        *,
        question_id: str = "choice",
        timeout: float | None = None,
    ) -> JevResult:
        try:
            question = choice_question(instructions, criteria)
            return await self.evaluate(state, {question_id: question}, timeout=timeout)
        except JevQuestionError as exc:
            return JevResult(error=f"question_error: {exc}")

    async def score(
        self,
        state: str,
        instructions: str,
        *,
        low: str,
        high: str,
        question_id: str = "score",
        timeout: float | None = None,
    ) -> JevResult:
        try:
            question = score_question(instructions, low=low, high=high)
            return await self.evaluate(state, {question_id: question}, timeout=timeout)
        except JevQuestionError as exc:
            return JevResult(error=f"question_error: {exc}")

    async def probe(self, *, timeout: float | None = None) -> JevResult:
        """Cheap round trip used by the panel's connectivity check."""
        return await self.noul(
            "连通性探测：这是一次不涉及任何真实判断的调用。",
            "这条状态是否是一段中文文本？",
            question_id="probe",
            timeout=timeout,
        )

    async def warmup(self, *, timeout: float | None = None) -> JevResult:
        """Pay the DNS/TLS/connect cost up front.

        Measured on a cold client the first request took ~1.78s against a 1.6s
        budget, so it timed out and was discarded; once the connection was
        pooled the same request settled at ~0.3s. Warming up at plugin start and
        after every config change keeps that first wasted round trip off the
        user's message path.
        """
        if not self.is_configured:
            return JevResult(error="not_configured")
        warmup_timeout = max(1.0, float(timeout if timeout is not None else max(self.timeout, 4.0)))
        result = await self.probe(timeout=warmup_timeout)
        if not result.ok:
            # Upstream error bodies are retained in the redacted diagnostics,
            # but should never be copied verbatim into process logs.
            logger.info(
                "Jev warmup did not succeed (status=%s); first real call may be slower",
                int(result.status or 0),
            )
        else:
            logger.debug("Jev warmup ok in %sms", result.elapsed_ms)
        return result


# ----------------------------------------------------------------------
# 旧接口兼容
# ----------------------------------------------------------------------


class JevDecisionEngine:
    """同步兼容外观，供尚未迁移到异步调用点的旧代码继续导入。

    新代码应直接使用 :class:`JevClient`。这里的同步方法是阻塞兼容桥；即使内部
    在线程池执行，从异步调用点使用它仍会阻塞调用线程，不能替代真正的 ``await``。
    """

    def __init__(
        self,
        api_key: str = "",
        gateway_url: str = "",
        model: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        kind: str = "auto",
    ) -> None:
        self._client = JevClient(
            api_key=api_key,
            kind=kind,
            gateway_url=gateway_url,
            model=model,
            timeout=timeout,
        )

    @property
    def is_configured(self) -> bool:
        return self._client.is_configured

    @property
    def profile(self) -> JevEndpointProfile:
        return self._client.profile

    def evaluate_sync(self, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        """Blocking compatibility evaluation; async callers must use ``evaluate``."""
        result = self._run(self._client.evaluate(state, questions))
        if not result.ok:
            logger.warning("Jev evaluation failed: %s", result.error)
            return {}
        return {qid: answer.value for qid, answer in result.answers.items()}

    def should_group_reply(
        self,
        text: str,
        *,
        bot_name: str = "",
        scene: Mapping[str, Any] | None = None,
        threshold: float = 0.65,
    ) -> bool | None:
        """Legacy helper; prefer the async gate in ``jev_gate``."""
        if not self._client.is_configured or not str(text or "").strip():
            return None
        name = str(bot_name or "Bot").strip() or "Bot"
        state = f"群聊发言：{text}\nBot身份：{name}"
        if scene:
            state += f"\n对话场景：{json.dumps(scene, ensure_ascii=False, default=str)}"
        stop = self._run(
            self._client.noul(
                state,
                f"发言者是否明确让 {name} 闭嘴、不要回复、走开，或指出不是在问 {name}？",
                true_hint=f"明确要求 {name} 停止发言或不要回应",
                false_hint="没有要求停止，也没有否定在叫 Bot",
                question_id="is_asking_to_stop",
            )
        )
        invite = self._run(
            self._client.noul(
                state,
                f"发言者是否在呼唤 {name}、向其提问，或提出 {name} 适合自然参与的话题？",
                true_hint=f"明确指向 {name}，或话题明显需要 {name} 参与",
                false_hint=f"在对其他人说话、自言自语，或话题与 {name} 无关",
                question_id="is_relevant_or_inviting",
            )
        )
        stop_answer = stop.get("is_asking_to_stop") if stop.ok else None
        invite_answer = invite.get("is_relevant_or_inviting") if invite.ok else None
        if stop_answer is None and invite_answer is None:
            return None
        stop_prob = stop_answer.probability if stop_answer else None
        if stop_prob is not None and stop_prob >= 0.7:
            logger.info("Jev: user asking bot to stop (p=%.2f)", stop_prob)
            return False
        invite_prob = invite_answer.probability if invite_answer else None
        if invite_prob is None:
            return None
        if invite_prob >= threshold:
            logger.info("Jev: bot invited to reply (p=%.2f)", invite_prob)
            return True
        return False

    def is_user_asking_to_stop(self, text: str) -> bool | None:
        if not self._client.is_configured or not str(text or "").strip():
            return None
        result = self._run(
            self._client.noul(
                text,
                "发言者是否要求对方闭嘴、别回了、不要理我、停止说话？",
                true_hint="明确要求停止说话",
                false_hint="没有提出停止要求",
                question_id="should_stop",
            )
        )
        if not result.ok:
            return None
        answer = result.get("should_stop")
        probability = answer.probability if answer else None
        if probability is None:
            return None
        return probability >= 0.7

    @staticmethod
    def _run(coro: Any) -> JevResult:
        """Run a coroutine to completion for legacy synchronous callers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # This compatibility path still waits synchronously for the worker.
        # Production async call sites use JevClient directly and never enter it.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_TIMEOUT_SECONDS",
    "JevClient",
    "JevDecisionEngine",
    "JevResult",
]
