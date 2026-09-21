"""Decision domain: TypeSafe Jev / System One delegation for low-latency judgements.

- :mod:`jev_endpoint`   —— 端点画像与自动适配（Vercel 网关 / TypeSafe 直连）
- :mod:`jev_primitives` —— noul / choice / score 三种原语的构造与解析
- :mod:`jev_engine`     —— 异步客户端（连接复用、并发约束、可诊断错误）
- :mod:`jev_tasks`      —— 可交给 JEV 的判定任务注册表
- :mod:`jev_gate`       —— 启停、阈值、熔断与审计
"""
from .jev_endpoint import (
    KIND_AUTO,
    KIND_CUSTOM,
    KIND_TYPESAFE,
    KIND_VERCEL,
    JevEndpointProfile,
    classify_url,
    credential_hint,
    infer_kind,
    resolve_profile,
)
from .jev_engine import JevClient, JevDecisionEngine, JevResult
from .jev_gate import JevAuditEntry, JevGate
from .jev_primitives import (
    JevAnswer,
    JevQuestionError,
    choice_question,
    noul_question,
    parse_answers,
    score_question,
    usage_of,
)
from .jev_tasks import (
    DEFAULT_ENABLED_TASKS,
    JEV_TASKS,
    TASK_BY_NAME,
    TASK_EMOTION_JUDGEMENT,
    TASK_GROUP_AIR_GUARD,
    TASK_GROUP_FOLLOWUP,
    TASK_GROUP_INTERJECT,
    TASK_GROUP_MEMBER_SAFETY,
    TASK_GROUP_WAKEUP_CONTEXT,
    TASK_PROACTIVE_PERSONA,
    TASK_REST_WAKEUP,
    TASK_SMART_DEBOUNCE,
    TASK_SMART_SILENCE,
    JevTaskSpec,
    all_task_names,
    normalize_enabled_tasks,
    resolve_enabled_tasks,
    task_spec,
)

__all__ = [
    "DEFAULT_ENABLED_TASKS",
    "JEV_TASKS",
    "KIND_AUTO",
    "KIND_CUSTOM",
    "KIND_TYPESAFE",
    "KIND_VERCEL",
    "TASK_BY_NAME",
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
    "JevAnswer",
    "JevAuditEntry",
    "JevClient",
    "JevDecisionEngine",
    "JevEndpointProfile",
    "JevGate",
    "JevQuestionError",
    "JevResult",
    "JevTaskSpec",
    "all_task_names",
    "choice_question",
    "classify_url",
    "credential_hint",
    "infer_kind",
    "normalize_enabled_tasks",
    "noul_question",
    "parse_answers",
    "resolve_enabled_tasks",
    "resolve_profile",
    "score_question",
    "task_spec",
    "usage_of",
]
