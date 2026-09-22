# -*- coding: utf-8 -*-
"""JEV 判定任务注册表。

每一项描述"哪个判断可以交给 JEV、用什么原语、阈值多少、默认是否开启"。

只登记**纯粹的判断**（是/否、单选、打分）。需要生成自然语言的任务一律不进这张
表——JEV 是非自回归概率模型，不做文本生成，把它放进生成链只会得到劣质文本。
所以群聊插话这类任务只把"要不要插话"交给 JEV，正文仍由原模型生成。

``default_enabled`` 的取舍依据实测延迟：复用连接后单次往返约 280ms（首次建连约
900ms）。给 JEV 的预算必须留得下这个数，否则每次都会超时再回落到模型，等于白付
一次网络往返。因此总预算只有 0.8s 的智能收口默认关闭，需要用户明确知道这一点。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TASK_SMART_SILENCE = "smart_silence"
TASK_GROUP_FOLLOWUP = "group_followup_judge"
TASK_GROUP_AIR_GUARD = "group_air_reply_guard"
TASK_SMART_DEBOUNCE = "smart_message_debounce"
TASK_GROUP_MEMBER_SAFETY = "group_member_safety"
TASK_GROUP_INTERJECT = "group_interject"
TASK_REST_WAKEUP = "rest_wakeup_judge"
TASK_GROUP_WAKEUP_CONTEXT = "group_wakeup_context"
TASK_PROACTIVE_PERSONA = "proactive_persona_judge"
TASK_EMOTION_JUDGEMENT = "emotion_judgement"


@dataclass(frozen=True)
class JevTaskSpec:
    """How one judgment task is delegated to JEV."""

    task: str
    label: str
    primitive: str
    default_enabled: bool
    runtime_wired: bool = False
    threshold: float = 0.5
    min_confidence: float = 0.0
    budget_seconds: float = 1.6
    note: str = ""

    @property
    def enabled_key(self) -> str:
        return f"jev_task_{self.task}_enabled"

    @property
    def threshold_key(self) -> str:
        return f"jev_task_{self.task}_threshold"

    def describe(self) -> str:
        state = "默认开" if self.default_enabled else "默认关"
        return f"{self.label}（{self.primitive}, {state}, 预算 {self.budget_seconds:g}s）"


JEV_TASKS: tuple[JevTaskSpec, ...] = (
    JevTaskSpec(
        task=TASK_GROUP_FOLLOWUP,
        label="群聊续接判断",
        primitive="noul",
        default_enabled=True,
        runtime_wired=True,
        threshold=0.5,
        min_confidence=0.55,
        budget_seconds=1.6,
        note="判断没 @Bot 的后续发言是否仍在对 Bot 说话。纯是否判断，原本要一次 8 token 的模型调用。",
    ),
    JevTaskSpec(
        task=TASK_GROUP_AIR_GUARD,
        label="群聊沉默闸门",
        primitive="noul",
        default_enabled=True,
        runtime_wired=True,
        threshold=0.5,
        min_confidence=0.55,
        budget_seconds=1.6,
        note="判断本轮是否应该保持沉默。与续接判断共用上游语义，但结论方向相反。",
    ),
    JevTaskSpec(
        task=TASK_SMART_SILENCE,
        label="智能沉默判定",
        primitive="noul",
        default_enabled=True,
        runtime_wired=True,
        threshold=0.55,
        min_confidence=0.6,
        budget_seconds=1.4,
        note="判断这条回复是否该被吞掉。原本用模型出 JSON，阈值 0.66。",
    ),
    JevTaskSpec(
        task=TASK_GROUP_INTERJECT,
        label="群聊主动插话判断",
        primitive="noul",
        default_enabled=True,
        runtime_wired=True,
        threshold=0.6,
        min_confidence=0.6,
        budget_seconds=1.6,
        note="只接管“要不要插话”；插话正文仍由原模型生成，JEV 不产出文本。",
    ),
    JevTaskSpec(
        task=TASK_GROUP_MEMBER_SAFETY,
        label="群成员风控判定",
        primitive="noul",
        default_enabled=False,
        threshold=0.5,
        min_confidence=0.6,
        budget_seconds=1.8,
        note="判断发言是否带恶意。风控误判代价高，默认关闭，建议先用面板测试比对再开。",
    ),
    JevTaskSpec(
        task=TASK_REST_WAKEUP,
        label="休息醒来判断",
        primitive="score",
        default_enabled=False,
        threshold=0.65,
        min_confidence=0.25,
        budget_seconds=1.8,
        note="给“是否值得在休息时段打扰”打 0-100 分。阈值对应原 rest_reply_llm_threshold；"
             "score 原语在连续区间上的 confidence 天然偏低（实测 0.39），门槛不能照 noul 取。",
    ),
    JevTaskSpec(
        task=TASK_GROUP_WAKEUP_CONTEXT,
        label="群聊唤醒线索词判断",
        primitive="noul",
        default_enabled=False,
        threshold=0.5,
        min_confidence=0.55,
        budget_seconds=1.6,
        note="替代原提交里那段正则评分。默认关闭：这条路径原本是零成本的关键词匹配，"
             "交给 JEV 会增加一次调用，换来的是准确率而不是省钱，应由用户自己权衡后开启。",
    ),
    JevTaskSpec(
        task=TASK_SMART_DEBOUNCE,
        label="智能收口判断",
        primitive="noul",
        default_enabled=False,
        threshold=0.5,
        min_confidence=0.6,
        budget_seconds=0.6,
        note="判断消息是否还没说完。总预算只有 0.8s，JEV 单次往返约 0.28s，余量偏紧，默认关闭。",
    ),
    JevTaskSpec(
        task=TASK_PROACTIVE_PERSONA,
        label="主动人格判定",
        primitive="choice",
        default_enabled=False,
        threshold=0.5,
        min_confidence=0.6,
        budget_seconds=1.8,
        note="在 send/defer/drop 之间选择；需要改写时仍回落模型。默认关闭，避免影响主动消息节奏。",
    ),
    JevTaskSpec(
        task=TASK_EMOTION_JUDGEMENT,
        label="情绪变化判断",
        primitive="choice",
        default_enabled=False,
        threshold=0.5,
        min_confidence=0.6,
        budget_seconds=1.8,
        note="判断情绪事件类型。字段多且需要 target/severity，JEV 只接管事件类型；默认关闭。",
    ),
)

TASK_BY_NAME: dict[str, JevTaskSpec] = {spec.task: spec for spec in JEV_TASKS}

DEFAULT_ENABLED_TASKS: tuple[str, ...] = tuple(
    spec.task for spec in JEV_TASKS if spec.default_enabled
)

WIRED_JEV_TASKS: tuple[str, ...] = tuple(
    spec.task for spec in JEV_TASKS if spec.runtime_wired
)


def task_spec(task: str) -> JevTaskSpec | None:
    return TASK_BY_NAME.get(str(task or "").strip())


def all_task_names() -> tuple[str, ...]:
    return tuple(TASK_BY_NAME)


def normalize_enabled_tasks(value: Any) -> list[str]:
    """Coerce a config value into a list of known task names.

    Accepts a list/tuple/set, a comma or newline separated string, and the
    sentinel ``all``. Unknown names are dropped so a stale config entry cannot
    silently widen the delegation surface.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            items: list[Any] = []
        elif text.lower() in {"all", "*", "全部"}:
            return list(all_task_names())
        else:
            items = [part for part in text.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = []
    result: list[str] = []
    for item in items:
        name = str(item or "").strip()
        if name in TASK_BY_NAME and name not in result:
            result.append(name)
    return result


def resolve_enabled_tasks(config_value: Any) -> list[str]:
    """Return the effective task set, falling back to the shipped defaults."""
    if config_value is None or config_value == "":
        return list(DEFAULT_ENABLED_TASKS)
    if isinstance(config_value, (list, tuple, set, frozenset)) and not config_value:
        return list(DEFAULT_ENABLED_TASKS)
    return normalize_enabled_tasks(config_value)


__all__ = [
    "DEFAULT_ENABLED_TASKS",
    "JEV_TASKS",
    "TASK_BY_NAME",
    "WIRED_JEV_TASKS",
    "TASK_EMOTION_JUDGEMENT",
    "TASK_GROUP_AIR_GUARD",
    "TASK_GROUP_FOLLOWUP",
    "TASK_GROUP_INTERJECT",
    "TASK_GROUP_MEMBER_SAFETY",
    "TASK_GROUP_WAKEUP_CONTEXT",
    "TASK_PROACTIVE_PERSONA",
    "TASK_REST_WAKEUP",
    "TASK_SMART_DEBOUNCE",
    "TASK_SMART_SILENCE",
    "JevTaskSpec",
    "all_task_names",
    "normalize_enabled_tasks",
    "resolve_enabled_tasks",
    "task_spec",
]
