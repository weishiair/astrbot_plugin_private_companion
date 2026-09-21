# -*- coding: utf-8 -*-
"""JEV 判定委托：插件侧接线。

把 :mod:`domains.decision` 的客户端与闸门接进插件：读取配置、复用插件的 Token
记账与日限额、暴露诊断信息，并保证所有调用都是异步的。

设计约束（都来自实测与仓库既有约定）：

- 判断只走异步路径。旧实现用 ``urllib`` 同步直连，把整个事件循环卡住一个往返。
- 第一次调用前先预热连接。冷启动建连实测 ~1.8s，会超出判定预算；预热后稳定在
  0.25–0.31s。
- 失败必须可见。JEV 不可用时返回 ``None`` 让调用方回落原模型，同时把原因写进
  审计和日志，不再出现"开关开着、实际静默失效"。
- JEV 的 Token 计入插件既有账本，因此 Token 页能看到、日硬限额能约束。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .domains.decision import (
    JevClient,
    JevGate,
    resolve_enabled_tasks,
)

logger = logging.getLogger("astrbot_plugin_private_companion.jev")

# JEV 不是 OpenAI 兼容的聊天补全，但在账本里需要一个稳定的 provider 标识，
# 这样 Token 页能把它的消耗与模型调用分开显示。
JEV_PROVIDER_BUDGET_LABEL = "jev:systemone"


class _JevUsageShim:
    """Minimal shape that ``_extract_llm_usage`` understands."""

    __slots__ = ("usage",)

    def __init__(self, usage: dict[str, int]) -> None:
        # 账本按 input/output 命名解析，System One 的用量字段正好同名。
        self.usage = {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }


def _jev_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on", "enable", "enabled", "开启", "启用", "是"}:
            return True
        if text in {"false", "0", "no", "off", "disable", "disabled", "关闭", "停用", "否", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _jev_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _jev_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _jev_text(value: Any) -> str:
    return str(value or "").strip()


class JevDecisionMixin:
    """TypeSafe Jev / System One 判定委托。"""

    # ------------------------------------------------------------------
    # 内部状态
    # ------------------------------------------------------------------

    def _jev_settings(self) -> dict[str, Any]:
        """Read JEV settings from the runtime attributes set at config load."""
        return {
            "enabled": _jev_bool(getattr(self, "enable_jev_decision", False)),
            "api_key": _jev_text(getattr(self, "jev_api_key", "")),
            "kind": _jev_text(getattr(self, "jev_endpoint_kind", "auto")) or "auto",
            "gateway_url": _jev_text(getattr(self, "jev_gateway_url", "")),
            "model": _jev_text(getattr(self, "jev_model", "")),
            "timeout": _jev_float(getattr(self, "jev_timeout_seconds", 1.6), 1.6, 0.15, 30.0),
            "concurrency": _jev_int(getattr(self, "jev_max_concurrency", 4), 4, 1, 32),
            "enabled_tasks": getattr(self, "jev_enabled_tasks", None),
            "threshold_overrides": getattr(self, "jev_threshold_overrides", ""),
            "fail_threshold": _jev_int(getattr(self, "jev_fail_threshold", 3), 3, 1, 50),
            "fail_open_seconds": _jev_float(getattr(self, "jev_fail_open_seconds", 90.0), 90.0, 1.0, 3600.0),
            "count_toward_limit": _jev_bool(getattr(self, "jev_count_toward_token_limit", True), True),
        }

    def _jev_gate(self) -> JevGate:
        """Return the gate, creating or reconfiguring it to match current settings."""
        settings = self._jev_settings()
        gate = getattr(self, "_jev_gate_obj", None)
        if gate is None:
            gate = JevGate(
                client=JevClient(
                    api_key=settings["api_key"],
                    kind=settings["kind"],
                    gateway_url=settings["gateway_url"],
                    model=settings["model"],
                    timeout=settings["timeout"],
                    max_concurrency=settings["concurrency"],
                ),
                master_enabled=settings["enabled"],
                enabled_tasks=settings["enabled_tasks"],
            )
            self._jev_gate_obj = gate
            self._jev_warmed = False
        else:
            gate.client.reconfigure(
                api_key=settings["api_key"],
                kind=settings["kind"],
                gateway_url=settings["gateway_url"],
                model=settings["model"],
                timeout=settings["timeout"],
            )
        gate.configure(
            master_enabled=settings["enabled"],
            enabled_tasks=settings["enabled_tasks"],
            threshold_overrides=settings["threshold_overrides"],
            fail_threshold=settings["fail_threshold"],
            fail_open_seconds=settings["fail_open_seconds"],
        )
        self._jev_count_toward_limit = settings["count_toward_limit"]
        return gate

    def _jev_reload(self) -> None:
        """Drop cached state so the next call re-reads config.

        Called after config load and on every config save; the pooled HTTP
        connection is kept unless the endpoint target actually changed.
        """
        gate = getattr(self, "_jev_gate_obj", None)
        if gate is not None:
            self._jev_gate()
        self._jev_warmed = False

    def _jev_ensure_warm(self) -> None:
        """Warm the connection once per config generation, off the message path."""
        if getattr(self, "_jev_warmed", False):
            return
        gate = getattr(self, "_jev_gate_obj", None)
        if gate is None or not gate.master_enabled or not gate.client.is_configured:
            return
        self._jev_warmed = True
        try:
            asyncio.get_running_loop().create_task(self._jev_warm_task())
        except RuntimeError:
            # No loop yet (sync bootstrap); the first judgment will warm instead.
            self._jev_warmed = False

    async def _jev_warm_task(self) -> None:
        try:
            gate = getattr(self, "_jev_gate_obj", None)
            if gate is not None:
                await gate.client.warmup()
        except Exception as exc:
            logger.debug("Jev warmup skipped: %s", exc)

    async def aclose_jev(self) -> None:
        """Release the pooled connection; called on plugin unload."""
        gate = getattr(self, "_jev_gate_obj", None)
        if gate is not None:
            try:
                await gate.client.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 判定入口
    # ------------------------------------------------------------------

    def _jev_should_attempt(self) -> bool:
        """Cheap gate used before every delegation.

        Reads settings directly instead of the gate object: the gate is created
        lazily by :meth:`_jev_gate`, and this check runs first, so consulting the
        gate here would report "not ready" on every call and silently disable
        the whole feature.
        """
        settings = self._jev_settings()
        if not settings["enabled"] or not settings["api_key"]:
            return False
        if not settings["count_toward_limit"]:
            return True
        # Honour the plugin's hard daily limit: JEV 也是外部推理消耗，
        # 达到硬限额时不应继续产生费用。
        try:
            return int(self._llm_daily_budget_remaining()) != 0
        except Exception:
            return True

    async def _jev_noul(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        true_hint: str = "",
        false_hint: str = "",
        min_confidence: float | None = None,
        timeout: float | None = None,
    ) -> bool | None:
        """Yes/no judgment via JEV; ``None`` means "run the original path"."""
        if not self._jev_should_attempt():
            return None
        self._jev_ensure_warm()
        gate = self._jev_gate()
        before = gate.stats()["usage"]
        decision = await gate.decide_noul(
            task=task,
            state=state,
            instructions=instructions,
            true_hint=true_hint,
            false_hint=false_hint,
            min_confidence=min_confidence,
            timeout=timeout,
        )
        self._jev_settle_usage(gate, before, task, state, decision_label=f"noul={decision}")
        return decision

    async def _jev_choice(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        criteria: dict[str, str],
        allowed: tuple[str, ...] = (),
        min_confidence: float | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Single-choice judgment via JEV; ``None`` means "run the original path"."""
        if not self._jev_should_attempt():
            return None
        self._jev_ensure_warm()
        gate = self._jev_gate()
        before = gate.stats()["usage"]
        label = await gate.decide_choice(
            task=task,
            state=state,
            instructions=instructions,
            criteria=criteria,
            allowed=allowed,
            min_confidence=min_confidence,
            timeout=timeout,
        )
        self._jev_settle_usage(gate, before, task, state, decision_label=f"choice={label}")
        return label

    async def _jev_score(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        low: str,
        high: str,
        min_confidence: float | None = None,
        timeout: float | None = None,
    ) -> float | None:
        """0-100 score via JEV; ``None`` means "run the original path"."""
        if not self._jev_should_attempt():
            return None
        self._jev_ensure_warm()
        gate = self._jev_gate()
        before = gate.stats()["usage"]
        score = await gate.decide_score(
            task=task,
            state=state,
            instructions=instructions,
            low=low,
            high=high,
            min_confidence=min_confidence,
            timeout=timeout,
        )
        self._jev_settle_usage(gate, before, task, state, decision_label=f"score={score}")
        return score

    # ------------------------------------------------------------------
    # 记账与诊断
    # ------------------------------------------------------------------

    def _jev_settle_usage(
        self,
        gate: JevGate,
        before: dict[str, int],
        task: str,
        state: str,
        *,
        decision_label: str,
    ) -> None:
        """Write the delta of JEV token usage into the plugin's existing ledger."""
        try:
            after = gate.stats()["usage"]
            delta = {
                key: max(0, int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0))
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
        except Exception:
            return
        if not delta.get("total_tokens"):
            # 失败或未发出请求时不产生记录；失败原因已经进入 JEV 审计。
            return
        recorder = getattr(self, "_record_llm_usage", None)
        if not callable(recorder):
            return
        try:
            recorder(
                provider_id=JEV_PROVIDER_BUDGET_LABEL,
                task=f"jev_{task}",
                prompt=str(state or ""),
                completion=str(decision_label or ""),
                elapsed_ms=0,
                success=True,
                resp=_JevUsageShim(delta),
                budget_exempt=not bool(getattr(self, "_jev_count_toward_limit", True)),
            )
        except Exception as exc:
            logger.debug("Jev usage recording failed: %s", exc)

    def jev_diagnostics(self) -> dict[str, Any]:
        """Panel-facing snapshot: config, endpoint, counters, breaker, audit."""
        settings = self._jev_settings()
        gate = getattr(self, "_jev_gate_obj", None)
        payload: dict[str, Any] = {
            "enabled": settings["enabled"],
            "api_key_set": bool(settings["api_key"]),
            "endpoint_kind": settings["kind"],
            "gateway_url": settings["gateway_url"],
            "model": settings["model"],
            "timeout_seconds": settings["timeout"],
            "warmed": bool(getattr(self, "_jev_warmed", False)),
            "enabled_tasks": list(resolve_enabled_tasks(settings["enabled_tasks"])),
        }
        if gate is None:
            payload["active"] = False
            payload["note"] = "尚未初始化；首次判定或预热后可用。"
            return payload
        payload["active"] = bool(gate.master_enabled and gate.client.is_configured)
        payload.update(gate.stats())
        payload["recent"] = gate.recent_audit(10)
        return payload


__all__ = ["JevDecisionMixin"]