# -*- coding: utf-8 -*-
"""System One 三种原语的构造与解析。

线上实测确认的协议形状：

- ``noul``   → 提问 ``{"type","instructions","criteria":{"true","false"}}``
               （``criteria`` 可选 dict）→ 应答 ``{"type":"noul","noul":0.95}``
- ``choice`` → 提问 ``{"type","instructions","criteria":{label: description}}``
               （``criteria`` 必填 dict）→ 应答 ``{"type":"choice","choice":"a",
               "confidence":0.97,"probabilities":{...}}``
- ``score``  → 提问 ``{"type","instructions","criteria":[low, high]}``
               （``criteria`` 必填两元素 list）→ 应答 ``{"type":"score","score":0.05,
               "confidence":0.89,"legend":{"0":..,"1":..},"probabilities":{...}}``

三个原语可以在同一次请求里混用，服务端并行且相互独立地评估。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

TYPE_NOUL = "noul"
TYPE_CHOICE = "choice"
TYPE_SCORE = "score"

PRIMITIVES = (TYPE_NOUL, TYPE_CHOICE, TYPE_SCORE)

# System One 的 score 原语返回 0..1；插件内部多个判断使用 0..100 语义。
SCORE_SCALE = 100.0


class JevQuestionError(ValueError):
    """Raised when a question cannot be built into a valid request payload."""


@dataclass
class JevAnswer:
    """One parsed answer, normalized across the three primitives."""

    question_id: str
    primitive: str
    value: Any = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    legend: dict[str, str] = field(default_factory=dict)

    @property
    def probability(self) -> float | None:
        """Noul probability (0..1) when this is a noul answer."""
        if self.primitive != TYPE_NOUL:
            return None
        try:
            return max(0.0, min(1.0, float(self.value)))
        except (TypeError, ValueError):
            return None

    @property
    def score_ratio(self) -> float | None:
        """Score answer normalized to 0..1."""
        if self.primitive != TYPE_SCORE:
            return None
        try:
            return max(0.0, min(1.0, float(self.value)))
        except (TypeError, ValueError):
            return None

    @property
    def score_100(self) -> float | None:
        """Score answer scaled to the 0..100 range used inside the plugin."""
        ratio = self.score_ratio
        return None if ratio is None else ratio * SCORE_SCALE

    @property
    def label(self) -> str | None:
        """Choice label, or ``None`` when the answer is not a choice."""
        if self.primitive != TYPE_CHOICE:
            return None
        text = str(self.value or "").strip()
        return text or None

    def meets(self, threshold: float, *, confidence: float | None = None) -> bool:
        """True when a noul probability clears ``threshold``.

        ``confidence`` additionally requires the answer to be decisive enough;
        System One omits confidence for noul (a two-outcome distribution is
        fully described by the probability itself), so the probability is used
        as its own confidence when the field is absent.
        """
        probability = self.probability
        if probability is None or probability < threshold:
            return False
        if confidence is None:
            return True
        return probability >= confidence

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            if isinstance(value, bool):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_payload(cls, question_id: str, payload: Any) -> "JevAnswer | None":
        """Parse one answer entry, tolerating missing or unusual ``type`` fields."""
        if not isinstance(payload, Mapping):
            return None
        primitive = str(payload.get("type") or "").strip().lower()
        if primitive not in PRIMITIVES:
            # Fall back to whichever primitive-specific field is present, so a
            # gateway that trims ``type`` still produces a usable answer.
            primitive = next(
                (kind for kind in PRIMITIVES if payload.get(kind) is not None),
                "",
            )
        if not primitive:
            return None

        probabilities_raw = payload.get("probabilities")
        probabilities: dict[str, float] = {}
        if isinstance(probabilities_raw, Mapping):
            for label, prob in probabilities_raw.items():
                number = cls._coerce_float(prob)
                if number is not None:
                    probabilities[str(label)] = number

        legend_raw = payload.get("legend")
        legend: dict[str, str] = {}
        if isinstance(legend_raw, Mapping):
            legend = {str(k): str(v) for k, v in legend_raw.items()}

        return cls(
            question_id=question_id,
            primitive=primitive,
            value=payload.get(primitive),
            confidence=cls._coerce_float(payload.get("confidence")),
            probabilities=probabilities,
            legend=legend,
        )


def noul_question(
    instructions: str,
    *,
    true_hint: str = "",
    false_hint: str = "",
) -> dict[str, Any]:
    """Build a noul ("is this true?") question.

    The criteria keys must be the literal ``true``/``false``. The endpoint
    accepts any dict keys without complaining, but it reads them semantically:
    measured on the same utterance, the canonical keys returned 0.29 where
    ``the answer is yes`` / ``yes`` / ``1`` all returned ~0.59. Only the
    documented spelling is well calibrated.
    """
    text = str(instructions or "").strip()
    if not text:
        raise JevQuestionError("noul 提问需要 instructions")
    question: dict[str, Any] = {"type": TYPE_NOUL, "instructions": text}
    true_text = str(true_hint or "").strip()
    false_text = str(false_hint or "").strip()
    if true_text or false_text:
        # Both anchors are sent whenever either is given: a half-specified
        # criteria leaves the model to invent the missing pole.
        question["criteria"] = {
            "true": true_text or "该判断成立",
            "false": false_text or "该判断不成立",
        }
    return question


def choice_question(instructions: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    """Build a choice question. ``criteria`` must map label → description."""
    text = str(instructions or "").strip()
    if not text:
        raise JevQuestionError("choice 提问需要 instructions")
    labels: dict[str, str] = {}
    for label, description in (criteria or {}).items():
        key = str(label or "").strip()
        if not key:
            continue
        labels[key] = str(description or "").strip()
    if len(labels) < 2:
        raise JevQuestionError("choice 提问至少需要两个候选标签")
    return {"type": TYPE_CHOICE, "instructions": text, "criteria": labels}


def score_question(instructions: str, *, low: str, high: str) -> dict[str, Any]:
    """Build a score question. The endpoint requires exactly two anchors."""
    text = str(instructions or "").strip()
    if not text:
        raise JevQuestionError("score 提问需要 instructions")
    low_text = str(low or "").strip()
    high_text = str(high or "").strip()
    if not low_text or not high_text:
        raise JevQuestionError("score 提问需要 low/high 两个锚点描述")
    return {"type": TYPE_SCORE, "instructions": text, "criteria": [low_text, high_text]}


def parse_answers(payload: Any) -> dict[str, JevAnswer]:
    """Extract ``answers`` from a System One response body."""
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("answers")
    if not isinstance(raw, Mapping):
        return {}
    answers: dict[str, JevAnswer] = {}
    for question_id, entry in raw.items():
        answer = JevAnswer.from_payload(str(question_id), entry)
        if answer is not None:
            answers[str(question_id)] = answer
    return answers


def usage_of(payload: Any) -> dict[str, int]:
    """Extract token usage so JEV spend can join the plugin's accounting."""
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        usage[key] = int(value)
    if "total_tokens" not in usage and usage:
        usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return usage


__all__ = [
    "PRIMITIVES",
    "SCORE_SCALE",
    "TYPE_CHOICE",
    "TYPE_NOUL",
    "TYPE_SCORE",
    "JevAnswer",
    "JevQuestionError",
    "choice_question",
    "noul_question",
    "parse_answers",
    "score_question",
    "usage_of",
]