# -*- coding: utf-8 -*-
"""判定闸门：把 JEV 的可用性、熔断、阈值和审计收在一处。

调用方只表达"我要把哪个判断交给 JEV"，闸门负责其余一切：

- 任务是否启用、本次是否值得尝试；
- 熔断：连续失败后短期不再尝试，避免 JEV 故障时每条消息都白等一个超时；
- 阈值与置信度：只有结论足够确定才返回，否则回 ``None`` 让调用方回落到原模型；
- 审计：每次尝试都留下可查记录，不再出现"开着但静默失效"。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .jev_engine import JevClient, JevResult
from .jev_primitives import JevAnswer
from .jev_tasks import JevTaskSpec, resolve_enabled_tasks, task_spec


@dataclass
class JevAuditEntry:
    """One recorded JEV attempt."""

    ts: float
    task: str
    ok: bool
    decision: str
    elapsed_ms: int
    reason: str = ""
    threshold: float = 0.0
    probability: float | None = None
    confidence: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    fallback: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": round(self.ts, 2),
            "task": self.task,
            "ok": bool(self.ok),
            "decision": self.decision,
            "elapsed_ms": int(self.elapsed_ms),
            "reason": self.reason,
            "threshold": round(self.threshold, 3),
        }
        if self.probability is not None:
            payload["probability"] = round(self.probability, 4)
        if self.confidence is not None:
            payload["confidence"] = round(self.confidence, 4)
        if self.usage:
            payload["usage"] = dict(self.usage)
        if self.fallback:
            payload["fallback"] = self.fallback
        return payload


class JevGate:
    """Per-plugin JEV delegation gate."""

    def __init__(
        self,
        *,
        client: JevClient | None = None,
        enabled_tasks: Any = None,
        master_enabled: bool = False,
        audit_limit: int = 200,
    ) -> None:
        self.client = client or JevClient()
        self.master_enabled = bool(master_enabled)
        self._enabled_tasks = resolve_enabled_tasks(enabled_tasks)
        self._threshold_overrides: dict[str, float] = {}
        self._audit: deque[JevAuditEntry] = deque(maxlen=max(20, int(audit_limit)))
        # Circuit breaker state.
        self.fail_threshold = 3
        self.fail_open_seconds = 90.0
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._last_error = ""
        self._last_success_ts = 0.0
        self._totals: dict[str, int] = {
            "attempts": 0,
            "ok": 0,
            "failed": 0,
            "timeout": 0,
            "skipped_breaker": 0,
            "low_confidence": 0,
            "fallback_to_llm": 0,
        }
        self._usage_totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @property
    def enabled_tasks(self) -> list[str]:
        return list(self._enabled_tasks)

    @property
    def threshold_overrides(self) -> dict[str, float]:
        return dict(self._threshold_overrides)

    def configure(
        self,
        *,
        master_enabled: bool | None = None,
        enabled_tasks: Any = None,
        threshold_overrides: Any = None,
        fail_threshold: int | None = None,
        fail_open_seconds: float | None = None,
    ) -> None:
        if master_enabled is not None:
            self.master_enabled = bool(master_enabled)
        if enabled_tasks is not None:
            self._enabled_tasks = resolve_enabled_tasks(enabled_tasks)
        if threshold_overrides is not None:
            self._threshold_overrides = self._coerce_overrides(threshold_overrides)
        if fail_threshold is not None:
            self.fail_threshold = max(1, int(fail_threshold))
        if fail_open_seconds is not None:
            self.fail_open_seconds = max(1.0, float(fail_open_seconds))

    @staticmethod
    def _coerce_overrides(value: Any) -> dict[str, float]:
        """Accept a dict or a JSON-ish text blob of per-task thresholds."""
        import json

        data = value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                data = json.loads(text)
            except Exception:
                return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, float] = {}
        for key, raw in data.items():
            name = str(key or "").strip()
            if not name:
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if 0.0 < number <= 1.0:
                result[name] = number
        return result

    def is_task_enabled(self, task: str) -> bool:
        return bool(self.master_enabled) and task in self._enabled_tasks

    def threshold_for(self, spec: JevTaskSpec) -> float:
        return float(self._threshold_overrides.get(spec.task, spec.threshold))

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    @property
    def breaker_open(self) -> bool:
        return time.time() < self._open_until

    def breaker_state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "open": now < self._open_until,
            "remaining_seconds": max(0, int(self._open_until - now)) if now < self._open_until else 0,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "last_success_ts": round(self._last_success_ts, 2),
        }

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._last_error = ""
        self._last_success_ts = time.time()
        self._totals["ok"] += 1

    def _record_failure(self, error: str, *, timed_out: bool = False) -> None:
        self._consecutive_failures += 1
        self._last_error = str(error or "")[:200]
        self._totals["failed"] += 1
        if timed_out:
            self._totals["timeout"] += 1
        if self._consecutive_failures >= self.fail_threshold:
            self._open_until = time.time() + self.fail_open_seconds

    def _accumulate_usage(self, usage: dict[str, int]) -> None:
        for key in self._usage_totals:
            self._usage_totals[key] += int(usage.get(key, 0) or 0)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _append_audit(self, entry: JevAuditEntry) -> None:
        self._audit.append(entry)

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        items = list(self._audit)[-max(1, int(limit)):]
        return [entry.to_dict() for entry in reversed(items)]

    def stats(self) -> dict[str, Any]:
        totals = dict(self._totals)
        attempts = totals.get("attempts", 0)
        totals["success_rate"] = round(totals.get("ok", 0) / attempts, 3) if attempts else 0.0
        return {
            "master_enabled": self.master_enabled,
            "configured": bool(self.client.is_configured),
            "endpoint": self.client.profile.describe(),
            "endpoint_reason": self.client.profile.reason,
            "timeout_seconds": round(self.client.timeout, 2),
            "enabled_tasks": list(self._enabled_tasks),
            "threshold_overrides": dict(self._threshold_overrides),
            "totals": totals,
            "usage": dict(self._usage_totals),
            "breaker": self.breaker_state(),
        }

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    async def decide_noul(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        true_hint: str = "",
        false_hint: str = "",
        question_id: str = "decision",
        min_confidence: float | None = None,
        timeout: float | None = None,
    ) -> bool | None:
        """Return True/False for a yes/no judgment, or None to fall back.

        ``None`` is the only fallback signal: it means the caller must run its
        original model path. A confident ``False`` is a real answer and must not
        be confused with "JEV unavailable".
        """
        spec = task_spec(task)
        if spec is None or not self.is_task_enabled(task):
            return None
        if not self.client.is_configured:
            # Gate-level guard, not just the caller's: an unconfigured client
            # would otherwise burn a circuit-breaker failure on every call and
            # eventually report a breaker-open state for what is really just a
            # missing API key.
            return None
        threshold = self.threshold_for(spec)
        floor = spec.min_confidence if min_confidence is None else float(min_confidence)

        if self.breaker_open:
            self._totals["skipped_breaker"] += 1
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="breaker_open",
                    elapsed_ms=0,
                    reason="熔断中，未尝试",
                    threshold=threshold,
                    fallback="llm",
                )
            )
            return None

        self._totals["attempts"] += 1
        result = await self.client.noul(
            state,
            instructions,
            true_hint=true_hint,
            false_hint=false_hint,
            question_id=question_id,
            timeout=timeout if timeout is not None else spec.budget_seconds,
        )
        answer = result.get(question_id) if result.ok else None
        probability = answer.probability if answer else None

        if not result.ok or probability is None:
            timed_out = result.error == "timeout"
            self._record_failure(result.error or "no_answer", timed_out=timed_out)
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="error",
                    elapsed_ms=result.elapsed_ms,
                    reason=result.error or "no_answer",
                    threshold=threshold,
                    fallback="llm",
                )
            )
            return None

        self._record_success()
        self._accumulate_usage(result.usage)
        confidence = answer.confidence if answer else None
        decision = probability >= threshold
        below_floor = floor > 0 and max(probability, 1.0 - probability) < floor
        self._append_audit(
            JevAuditEntry(
                ts=time.time(),
                task=task,
                ok=True,
                # ``decision`` records what JEV answered; ``accepted`` records
                # whether the gate took it. They differ when the answer clears
                # the threshold but not the confidence floor, and collapsing
                # them made the audit read as "decision=reply, reason=低置信度"
                # which looks self-contradictory when diagnosing.
                decision="reply" if decision else "silent",
                elapsed_ms=result.elapsed_ms,
                reason=(
                    f"概率 {probability:.2f} 过阈值 {threshold:.2f}，但低于置信度下限 {floor:.2f}，回落原模型"
                    if below_floor
                    else ""
                ),
                threshold=threshold,
                probability=probability,
                confidence=confidence,
                usage=result.usage,
                fallback="llm" if below_floor else "",
            )
        )
        if below_floor:
            self._totals["low_confidence"] += 1
            self._totals["fallback_to_llm"] += 1
            return None
        return decision

    async def decide_choice(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        criteria: dict[str, str],
        question_id: str = "decision",
        min_confidence: float | None = None,
        timeout: float | None = None,
        allowed: tuple[str, ...] = (),
    ) -> str | None:
        """Return the chosen label, or None to fall back."""
        spec = task_spec(task)
        if spec is None or not self.is_task_enabled(task):
            return None
        if not self.client.is_configured:
            # Gate-level guard, not just the caller's: an unconfigured client
            # would otherwise burn a circuit-breaker failure on every call and
            # eventually report a breaker-open state for what is really just a
            # missing API key.
            return None
        floor = spec.min_confidence if min_confidence is None else float(min_confidence)
        if self.breaker_open:
            self._totals["skipped_breaker"] += 1
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="breaker_open",
                    elapsed_ms=0,
                    reason="熔断中，未尝试",
                    fallback="llm",
                )
            )
            return None

        self._totals["attempts"] += 1
        result = await self.client.choice(
            state,
            instructions,
            criteria,
            question_id=question_id,
            timeout=timeout if timeout is not None else spec.budget_seconds,
        )
        answer: JevAnswer | None = result.get(question_id) if result.ok else None
        label = answer.label if answer is not None else None

        if not result.ok or not label:
            self._record_failure(result.error or "no_label", timed_out=result.error == "timeout")
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="error",
                    elapsed_ms=result.elapsed_ms,
                    reason=result.error or "no_label",
                    fallback="llm",
                )
            )
            return None

        if allowed and label not in allowed:
            self._record_success()
            self._accumulate_usage(result.usage)
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=True,
                    decision="unexpected_label",
                    elapsed_ms=result.elapsed_ms,
                    reason=f"标签 {label} 不在允许集合内",
                    confidence=answer.confidence if answer else None,
                    usage=result.usage,
                    fallback="llm",
                )
            )
            self._totals["fallback_to_llm"] += 1
            return None

        self._record_success()
        self._accumulate_usage(result.usage)
        confidence = answer.confidence if answer else None
        below_floor = floor > 0 and (confidence is None or confidence < floor)
        self._append_audit(
            JevAuditEntry(
                ts=time.time(),
                task=task,
                ok=True,
                decision=label,
                elapsed_ms=result.elapsed_ms,
                reason=(
                    f"标签 {label} 置信度 {confidence if confidence is not None else '缺失'}"
                    f"低于下限 {floor:.2f}，回落原模型"
                    if below_floor
                    else ""
                ),
                confidence=confidence,
                usage=result.usage,
                fallback="llm" if below_floor else "",
            )
        )
        if below_floor:
            self._totals["low_confidence"] += 1
            self._totals["fallback_to_llm"] += 1
            return None
        return label

    async def decide_score(
        self,
        *,
        task: str,
        state: str,
        instructions: str,
        low: str,
        high: str,
        question_id: str = "score",
        min_confidence: float | None = None,
        timeout: float | None = None,
    ) -> float | None:
        """Return a 0-100 score, or None to fall back."""
        spec = task_spec(task)
        if spec is None or not self.is_task_enabled(task):
            return None
        if not self.client.is_configured:
            # Gate-level guard, not just the caller's: an unconfigured client
            # would otherwise burn a circuit-breaker failure on every call and
            # eventually report a breaker-open state for what is really just a
            # missing API key.
            return None
        floor = spec.min_confidence if min_confidence is None else float(min_confidence)
        if self.breaker_open:
            self._totals["skipped_breaker"] += 1
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="breaker_open",
                    elapsed_ms=0,
                    reason="熔断中，未尝试",
                    fallback="llm",
                )
            )
            return None

        self._totals["attempts"] += 1
        result = await self.client.score(
            state,
            instructions,
            low=low,
            high=high,
            question_id=question_id,
            timeout=timeout if timeout is not None else spec.budget_seconds,
        )
        answer: JevAnswer | None = result.get(question_id) if result.ok else None
        score = answer.score_100 if answer is not None else None

        if not result.ok or score is None:
            self._record_failure(result.error or "no_score", timed_out=result.error == "timeout")
            self._append_audit(
                JevAuditEntry(
                    ts=time.time(),
                    task=task,
                    ok=False,
                    decision="error",
                    elapsed_ms=result.elapsed_ms,
                    reason=result.error or "no_score",
                    fallback="llm",
                )
            )
            return None

        self._record_success()
        self._accumulate_usage(result.usage)
        confidence = answer.confidence if answer else None
        below_floor = floor > 0 and (confidence is None or confidence < floor)
        self._append_audit(
            JevAuditEntry(
                ts=time.time(),
                task=task,
                ok=True,
                decision=f"score={score:.0f}",
                elapsed_ms=result.elapsed_ms,
                reason=(
                    f"打分置信度 {confidence if confidence is not None else '缺失'}"
                    f"低于下限 {floor:.2f}，回落原模型"
                    if below_floor
                    else ""
                ),
                confidence=confidence,
                usage=result.usage,
                fallback="llm" if below_floor else "",
            )
        )
        if below_floor:
            self._totals["low_confidence"] += 1
            self._totals["fallback_to_llm"] += 1
            return None
        return score


__all__ = ["JevAuditEntry", "JevGate"]