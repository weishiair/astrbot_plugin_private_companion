# -*- coding: utf-8 -*-
"""JEV 判定域的单元测试：端点适配、原语契约、闸门策略、插件侧配置。

这些用例全部离线运行：闸门测试注入假客户端，因此不依赖网络也不消耗额度。
需要真实端点的联调放在 tests/test_jev_live.py，未配置 Key 时自动跳过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domains.decision import (  # noqa: E402
    DEFAULT_ENABLED_TASKS,
    JEV_TASKS,
    KIND_CUSTOM,
    KIND_TYPESAFE,
    KIND_VERCEL,
    TASK_GROUP_AIR_GUARD,
    TASK_GROUP_FOLLOWUP,
    TASK_GROUP_INTERJECT,
    TASK_REST_WAKEUP,
    TASK_SMART_SILENCE,
    JevAnswer,
    JevClient,
    JevEndpointProfile,
    JevGate,
    JevQuestionError,
    JevResult,
    choice_question,
    credential_hint,
    noul_question,
    normalize_enabled_tasks,
    parse_answers,
    resolve_enabled_tasks,
    resolve_profile,
    score_question,
)
from domains.decision.jev_endpoint import (  # noqa: E402
    TYPESAFE_DEFAULT_MODEL,
    TYPESAFE_DEFAULT_URL,
    VERCEL_DEFAULT_MODEL,
    VERCEL_DEFAULT_URL,
)


# ----------------------------------------------------------------------
# 端点画像
# ----------------------------------------------------------------------


class TestEndpointResolution:
    def test_typesafe_key_prefix_wins(self):
        profile = resolve_profile(kind="auto", api_key="apikey_abc")
        assert profile.kind == KIND_TYPESAFE
        assert profile.url == TYPESAFE_DEFAULT_URL
        assert profile.model == TYPESAFE_DEFAULT_MODEL

    def test_vercel_key_prefix_wins(self):
        profile = resolve_profile(kind="auto", api_key="vck_abc")
        assert profile.kind == KIND_VERCEL
        assert profile.url == VERCEL_DEFAULT_URL
        assert profile.model == VERCEL_DEFAULT_MODEL

    def test_key_prefix_beats_conflicting_url(self):
        """A vck_ credential is only valid on the gateway, whatever URL was left behind.

        On upgrade this matters: older versions shipped the Vercel URL as the
        schema default, so existing installs still have it persisted next to a
        TypeSafe key.
        """
        profile = resolve_profile(
            kind="auto",
            api_key="vck_abc",
            gateway_url=TYPESAFE_DEFAULT_URL,
        )
        assert profile.kind == KIND_VERCEL
        assert profile.url == VERCEL_DEFAULT_URL
        assert profile.model == VERCEL_DEFAULT_MODEL
        assert profile.reason == "api_key_family_over_gateway_url"

    def test_stale_vercel_url_does_not_break_a_typesafe_key(self):
        """The upgrade path: old Vercel default URL persisted + new TypeSafe key."""
        profile = resolve_profile(
            kind="auto",
            api_key="apikey_abc",
            gateway_url=VERCEL_DEFAULT_URL,
        )
        assert profile.kind == KIND_TYPESAFE
        assert profile.url == TYPESAFE_DEFAULT_URL
        assert profile.model == TYPESAFE_DEFAULT_MODEL

    def test_same_family_url_override_is_kept(self):
        profile = resolve_profile(kind="auto", api_key="vck_abc", gateway_url=VERCEL_DEFAULT_URL)
        assert profile.kind == KIND_VERCEL
        assert profile.url == VERCEL_DEFAULT_URL

    def test_unclassifiable_url_is_honoured_and_labelled(self):
        profile = resolve_profile(kind="auto", api_key="", gateway_url="https://mirror.internal/v1/systemone")
        assert profile.url == "https://mirror.internal/v1/systemone"
        assert profile.reason == "custom_gateway_url"

    def test_url_host_used_when_key_absent(self):
        profile = resolve_profile(kind="auto", gateway_url=VERCEL_DEFAULT_URL)
        assert profile.kind == KIND_VERCEL
        profile = resolve_profile(kind="auto", gateway_url=TYPESAFE_DEFAULT_URL)
        assert profile.kind == KIND_TYPESAFE

    def test_custom_url_is_honoured_verbatim(self):
        """An unclassifiable host is kept, and flagged so the model namespace is not assumed."""
        profile = resolve_profile(kind="auto", api_key="apikey_x", gateway_url="https://mirror.internal/v1/systemone")
        assert profile.url == "https://mirror.internal/v1/systemone"
        assert profile.reason == "custom_gateway_url"

    def test_custom_kind_marks_the_profile_explicitly(self):
        profile = resolve_profile(
            kind=KIND_CUSTOM,
            api_key="apikey_x",
            gateway_url="https://mirror.internal/v1/systemone",
        )
        assert profile.kind == KIND_CUSTOM
        assert profile.url == "https://mirror.internal/v1/systemone"

    def test_explicit_kind_overrides_prefix_inference(self):
        profile = resolve_profile(kind=KIND_TYPESAFE, api_key="vck_abc")
        assert profile.kind == KIND_TYPESAFE
        assert profile.model == TYPESAFE_DEFAULT_MODEL

    def test_explicit_model_and_url_override_defaults(self):
        profile = resolve_profile(
            kind=KIND_VERCEL,
            api_key="vck_abc",
            gateway_url="https://gw.example/typesafe/v1/systemone",
            model="custom/model",
        )
        assert profile.url == "https://gw.example/typesafe/v1/systemone"
        assert profile.model == "custom/model"

    def test_unknown_kind_falls_back_to_auto(self):
        profile = resolve_profile(kind="nonsense", api_key="apikey_abc")
        assert profile.kind == KIND_TYPESAFE

    def test_defaults_stay_empty_so_auto_detection_is_not_shadowed(self):
        """The schema defaults for url/model must be empty.

        A non-empty default would be read as an explicit override and pin every
        user to one endpoint family regardless of their key.
        """
        import json

        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        items = schema["basic_config"]["items"]
        assert items["jev_gateway_url"]["default"] == ""
        assert items["jev_model"]["default"] == ""
        assert items["jev_endpoint_kind"]["default"] == "auto"

    def test_credential_hint_flags_mismatch(self):
        vercel = resolve_profile(kind=KIND_VERCEL, api_key="apikey_abc")
        assert "typesafe" in credential_hint(vercel, "apikey_abc").lower()
        typesafe = resolve_profile(kind=KIND_TYPESAFE, api_key="vck_abc")
        assert "vercel" in credential_hint(typesafe, "vck_abc").lower()
        matched = resolve_profile(kind=KIND_TYPESAFE, api_key="apikey_abc")
        assert credential_hint(matched, "apikey_abc") == ""


# ----------------------------------------------------------------------
# 原语
# ----------------------------------------------------------------------


class TestPrimitives:
    def test_noul_criteria_uses_literal_true_false_keys(self):
        """The endpoint reads criteria keys semantically.

        Measured on one utterance, ``true``/``false`` returned 0.29 where
        ``the answer is yes`` / ``yes`` / ``1`` all returned ~0.59, so the
        documented spelling is the only well-calibrated one.
        """
        question = noul_question("是否在问 Bot？", true_hint="是", false_hint="否")
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"true", "false"}

    def test_noul_without_hints_omits_criteria(self):
        question = noul_question("是否成立？")
        assert "criteria" not in question

    def test_partial_hints_still_send_both_poles(self):
        question = noul_question("是否成立？", true_hint="是")
        assert set(question["criteria"]) == {"true", "false"}

    def test_noul_requires_instructions(self):
        with pytest.raises(JevQuestionError):
            noul_question("   ")

    def test_choice_criteria_must_be_a_dict_with_two_labels(self):
        question = choice_question("意图", {"a": "问 Bot", "b": "和他人说话"})
        assert question["criteria"] == {"a": "问 Bot", "b": "和他人说话"}
        with pytest.raises(JevQuestionError):
            choice_question("意图", {"only": "一个"})

    def test_score_criteria_is_a_two_element_list(self):
        """The endpoint rejects a dict here: criteria must be a list of two anchors."""
        question = score_question("紧迫度", low="不急", high="很急")
        assert question["criteria"] == ["不急", "很急"]
        with pytest.raises(JevQuestionError):
            score_question("紧迫度", low="", high="很急")

    def test_parse_answers_normalizes_all_three_primitives(self):
        payload = {
            "model": "jev-1.13.0",
            "answers": {
                "a": {"type": "noul", "noul": 0.85},
                "b": {"type": "choice", "choice": "ask_bot", "confidence": 0.97,
                      "probabilities": {"ask_bot": 0.99, "chat_others": 0.01}},
                "c": {"type": "score", "score": 0.13, "confidence": 0.74,
                      "legend": {"0": "不急", "1": "很急"}},
            },
            "usage": {"input_tokens": 321, "output_tokens": 26},
        }
        answers = parse_answers(payload)
        assert answers["a"].probability == pytest.approx(0.85)
        assert answers["b"].label == "ask_bot"
        assert answers["b"].confidence == pytest.approx(0.97)
        assert answers["b"].probabilities["ask_bot"] == pytest.approx(0.99)
        assert answers["c"].score_100 == pytest.approx(13.0)
        assert answers["c"].legend["0"] == "不急"

    def test_score_is_scaled_to_100(self):
        """System One returns 0..1 while several call sites expect 0..100."""
        answer = JevAnswer(question_id="q", primitive="score", value=0.65)
        assert answer.score_ratio == pytest.approx(0.65)
        assert answer.score_100 == pytest.approx(65.0)

    def test_primitive_inferred_when_type_field_missing(self):
        """A gateway that trims ``type`` should still yield a usable answer."""
        answers = parse_answers({"answers": {"q": {"noul": 0.4}}})
        assert answers["q"].primitive == "noul"
        assert answers["q"].probability == pytest.approx(0.4)

    def test_unparseable_entries_are_dropped(self):
        """A non-dict entry is dropped; a well-formed one survives."""
        answers = parse_answers({"answers": {"bad": "not-a-dict", "good": {"type": "noul", "noul": 0.4}}})
        assert "bad" not in answers
        assert answers["good"].probability == pytest.approx(0.4)

    def test_entry_without_usable_primitive_is_dropped(self):
        answers = parse_answers({"answers": {"q": {"confidence": 0.9}}})
        assert "q" not in answers

    def test_no_answers_yields_empty(self):
        assert parse_answers({}) == {}
        assert parse_answers({"answers": []}) == {}
        assert parse_answers(None) == {}

    def test_meets_honours_threshold_and_confidence(self):
        answer = JevAnswer(question_id="q", primitive="noul", value=0.8)
        assert answer.meets(0.5)
        assert not answer.meets(0.9)
        assert answer.meets(0.5, confidence=0.7)
        assert not answer.meets(0.5, confidence=0.9)


# ----------------------------------------------------------------------
# 任务注册表
# ----------------------------------------------------------------------


class TestTaskRegistry:
    def test_task_names_are_unique(self):
        names = [spec.task for spec in JEV_TASKS]
        assert len(names) == len(set(names))

    def test_enabled_list_accepts_list_comma_string_and_all(self):
        assert normalize_enabled_tasks(["smart_silence"]) == ["smart_silence"]
        assert normalize_enabled_tasks("smart_silence, group_interject") == [
            "smart_silence",
            "group_interject",
        ]
        assert set(normalize_enabled_tasks("all")) == {spec.task for spec in JEV_TASKS}

    def test_unknown_task_names_are_dropped(self):
        assert normalize_enabled_tasks("smart_silence,not_a_task") == ["smart_silence"]
        assert normalize_enabled_tasks(None) == []
        assert normalize_enabled_tasks("") == []

    def test_empty_config_falls_back_to_shipped_defaults(self):
        assert resolve_enabled_tasks("") == list(DEFAULT_ENABLED_TASKS)
        assert resolve_enabled_tasks(None) == list(DEFAULT_ENABLED_TASKS)

    def test_defaults_exclude_task_that_replaces_a_free_regex_path(self):
        """group_wakeup_context replaces regex scoring, so it costs rather than saves."""
        assert "group_wakeup_context" not in DEFAULT_ENABLED_TASKS


# ----------------------------------------------------------------------
# 闸门（注入假客户端，离线）
# ----------------------------------------------------------------------


class _FakeClient:
    """Scripted client: each call pops the next queued result."""

    def __init__(self, results=None, profile=None, configured=True, timeout=1.6):
        self.profile = profile or JevEndpointProfile(
            kind=KIND_TYPESAFE, url=TYPESAFE_DEFAULT_URL, model=TYPESAFE_DEFAULT_MODEL, reason="test"
        )
        self.timeout = timeout
        self.is_configured = configured
        self._results = list(results or [])
        self.calls = []

    def _next(self):
        return self._results.pop(0) if self._results else JevResult(error="exhausted")

    async def noul(self, state, instructions, *, true_hint="", false_hint="", question_id="q", timeout=None):
        self.calls.append(("noul", question_id, state))
        return self._next()

    async def choice(self, state, instructions, criteria, *, question_id="q", timeout=None):
        self.calls.append(("choice", question_id, state))
        return self._next()

    async def score(self, state, instructions, *, low, high, question_id="q", timeout=None):
        self.calls.append(("score", question_id, state))
        return self._next()

    async def warmup(self, *, timeout=None):
        return JevResult(ok=True)

    def reconfigure(self, *, api_key="", kind="auto", gateway_url="", model="", timeout=None):
        """Mirror JevClient.reconfigure so the mixin can drive this fake."""
        self.is_configured = bool(str(api_key or "").strip())
        if timeout is not None:
            self.timeout = timeout

    async def aclose(self):
        return None


def _noul_result(value, usage=None, question_id="decision"):
    return JevResult(
        ok=True,
        answers={question_id: JevAnswer(question_id=question_id, primitive="noul", value=value)},
        usage=usage or {"input_tokens": 300, "output_tokens": 20, "total_tokens": 320},
        elapsed_ms=280,
    )


def _choice_result(label, confidence=0.9, question_id="decision"):
    return JevResult(
        ok=True,
        answers={
            question_id: JevAnswer(
                question_id=question_id, primitive="choice", value=label, confidence=confidence
            )
        },
        usage={"input_tokens": 300, "output_tokens": 20, "total_tokens": 320},
        elapsed_ms=280,
    )


def _score_result(value, confidence=0.5, question_id="score"):
    return JevResult(
        ok=True,
        answers={
            question_id: JevAnswer(
                question_id=question_id, primitive="score", value=value, confidence=confidence
            )
        },
        usage={"input_tokens": 300, "output_tokens": 20, "total_tokens": 320},
        elapsed_ms=280,
    )


def _run(coro):
    return asyncio.run(coro)


class TestGatePolicy:
    def test_disabled_master_switch_returns_none(self):
        gate = JevGate(client=_FakeClient([_noul_result(0.99)]), master_enabled=False)
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is None

    def test_task_not_in_enabled_list_returns_none(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.99)]),
            master_enabled=True,
            enabled_tasks=[TASK_SMART_SILENCE],
        )
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is None

    def test_unconfigured_client_returns_none(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.99)], configured=False),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is None

    def test_high_probability_returns_true(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.95)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is True

    def test_low_probability_returns_false_not_none(self):
        """A confident "no" is an answer, and must not be confused with "unavailable"."""
        gate = JevGate(
            client=_FakeClient([_noul_result(0.05)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is False

    def test_low_confidence_falls_back_to_llm(self):
        """Ambiguous probabilities must let the original model path run."""
        gate = JevGate(
            client=_FakeClient([_noul_result(0.52)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
            audit_limit=50,
        )
        result = _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        assert result is None
        assert gate.stats()["totals"]["low_confidence"] == 1
        assert gate.stats()["totals"]["fallback_to_llm"] == 1

    def test_audit_reason_names_the_threshold_and_floor(self):
        """The audit must not read as self-contradictory.

        ``decision`` records what JEV answered while ``reason`` explains why the
        gate overrode it; earlier the reason was the bare phrase 低置信度回落,
        which next to ``decision=silent`` looked like a logging bug.
        """
        gate = JevGate(
            client=_FakeClient([_noul_result(0.52)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        entry = gate.recent_audit(1)[0]
        assert entry["fallback"] == "llm"
        assert "低于置信度下限" in entry["reason"]
        assert "0.52" in entry["reason"]

    def test_answer_clearing_threshold_but_not_floor_still_falls_back(self):
        """0.55 clears the default threshold; a stricter floor must still defer.

        This is the band that produced the confusing audit entry.
        """
        gate = JevGate(
            client=_FakeClient([_noul_result(0.58)]),
            master_enabled=True,
            enabled_tasks=[TASK_SMART_SILENCE],
        )
        assert _run(gate.decide_noul(task=TASK_SMART_SILENCE, state="x", instructions="y")) is None
        entry = gate.recent_audit(1)[0]
        assert entry["decision"] == "reply"
        assert entry["fallback"] == "llm"

    def test_acceptance_above_floor_has_no_fallback_marker(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.95)]),
            master_enabled=True,
            enabled_tasks=[TASK_SMART_SILENCE],
        )
        assert _run(gate.decide_noul(task=TASK_SMART_SILENCE, state="x", instructions="y")) is True
        entry = gate.recent_audit(1)[0]
        assert entry["reason"] == ""
        assert "fallback" not in entry

    def test_choice_audit_names_the_confidence(self):
        gate = JevGate(
            client=_FakeClient([_choice_result("ask_bot", confidence=0.1)]),
            master_enabled=True,
            enabled_tasks=[TASK_REST_WAKEUP],
        )
        assert _run(gate.decide_choice(
            task=TASK_REST_WAKEUP, state="s", instructions="i", criteria={"ask_bot": "a"}
        )) is None
        entry = gate.recent_audit(1)[0]
        assert "0.1" in entry["reason"]

    def test_score_audit_names_the_confidence(self):
        gate = JevGate(
            client=_FakeClient([_score_result(0.9, confidence=0.05)]),
            master_enabled=True,
            enabled_tasks=[TASK_REST_WAKEUP],
        )
        assert _run(gate.decide_score(
            task=TASK_REST_WAKEUP, state="s", instructions="i", low="l", high="h"
        )) is None
        entry = gate.recent_audit(1)[0]
        assert "0.05" in entry["reason"]

    def test_threshold_override_is_applied(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.6)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        gate.configure(threshold_overrides={"group_followup_judge": 0.9})
        # 0.6 clears the default threshold but not the override; the answer is
        # confident enough that it is still a definite "no".
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is False

    def test_error_returns_none_and_counts_failure(self):
        gate = JevGate(
            client=_FakeClient([JevResult(ok=False, error="HTTP 401 unauthorized")]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is None
        stats = gate.stats()
        assert stats["totals"]["failed"] == 1
        assert "401" in stats["breaker"]["last_error"]

    def test_timeout_is_counted_separately(self):
        gate = JevGate(
            client=_FakeClient([JevResult(ok=False, error="timeout")]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        assert gate.stats()["totals"]["timeout"] == 1

    def test_breaker_opens_after_threshold_and_stops_attempting(self):
        client = _FakeClient([JevResult(ok=False, error="boom") for _ in range(5)])
        gate = JevGate(client=client, master_enabled=True, enabled_tasks=[TASK_GROUP_FOLLOWUP])
        gate.configure(fail_threshold=2, fail_open_seconds=60)
        for _ in range(4):
            assert _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y")) is None
        assert gate.breaker_open is True
        # Only the first two attempts reached the network.
        assert len(client.calls) == 2
        assert gate.stats()["totals"]["skipped_breaker"] == 2

    def test_success_resets_breaker_counters(self):
        gate = JevGate(
            client=_FakeClient([JevResult(ok=False, error="boom"), _noul_result(0.95)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        gate.configure(fail_threshold=3, fail_open_seconds=60)
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        assert gate.breaker_state()["consecutive_failures"] == 1
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        assert gate.breaker_state()["consecutive_failures"] == 0
        assert gate.breaker_open is False

    def test_usage_accumulates_across_calls(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.95), _noul_result(0.95)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="x", instructions="y"))
        usage = gate.stats()["usage"]
        assert usage["input_tokens"] == 600
        assert usage["total_tokens"] == 640

    def test_audit_records_decision_and_probability(self):
        gate = JevGate(
            client=_FakeClient([_noul_result(0.93)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="s", instructions="i"))
        entry = gate.recent_audit(1)[0]
        assert entry["task"] == TASK_GROUP_FOLLOWUP
        assert entry["decision"] == "reply"
        assert entry["probability"] == pytest.approx(0.93)

    def test_choice_returns_label_and_rejects_unexpected_labels(self):
        gate = JevGate(
            client=_FakeClient([_choice_result("ask_bot"), _choice_result("something_else")]),
            master_enabled=True,
            enabled_tasks=[TASK_REST_WAKEUP],
        )
        label = _run(gate.decide_choice(
            task=TASK_REST_WAKEUP, state="s", instructions="i", criteria={"ask_bot": "a"}, allowed=("ask_bot",)
        ))
        assert label == "ask_bot"
        rejected = _run(gate.decide_choice(
            task=TASK_REST_WAKEUP, state="s", instructions="i", criteria={"ask_bot": "a"}, allowed=("ask_bot",)
        ))
        assert rejected is None

    def test_score_returns_0_to_100(self):
        gate = JevGate(
            client=_FakeClient([_score_result(0.72, confidence=0.8)]),
            master_enabled=True,
            enabled_tasks=[TASK_REST_WAKEUP],
        )
        score = _run(gate.decide_score(
            task=TASK_REST_WAKEUP, state="s", instructions="i", low="low", high="high"
        ))
        assert score == pytest.approx(72.0)

    def test_state_and_instructions_reach_the_client(self):
        client = _FakeClient([_noul_result(0.95)])
        gate = JevGate(client=client, master_enabled=True, enabled_tasks=[TASK_GROUP_FOLLOWUP])
        _run(gate.decide_noul(task=TASK_GROUP_FOLLOWUP, state="THE-STATE", instructions="THE-QUESTION"))
        kind, _qid, state = client.calls[0]
        assert kind == "noul"
        assert state == "THE-STATE"


# ----------------------------------------------------------------------
# 客户端离线行为
# ----------------------------------------------------------------------


class TestClientOffline:
    def test_unconfigured_client_never_calls_out(self):
        client = JevClient(api_key="")
        result = _run(client.noul("state", "instructions"))
        assert result.ok is False
        assert result.error == "not_configured"

    def test_empty_state_is_rejected_before_network(self):
        client = JevClient(api_key="apikey_x")
        result = _run(client.evaluate("   ", {"q": noul_question("x")}))
        assert result.error == "empty_state"

    def test_no_questions_is_rejected(self):
        client = JevClient(api_key="apikey_x")
        assert _run(client.evaluate("state", {})).error == "no_questions"

    def test_oversized_state_is_truncated_not_rejected(self):
        from domains.decision.jev_engine import MAX_STATE_CHARS

        assert MAX_STATE_CHARS >= 1000

    def test_reconfigure_drops_session_when_target_changes(self):
        client = JevClient(api_key="apikey_x", kind=KIND_TYPESAFE)
        assert client.profile.kind == KIND_TYPESAFE
        client.reconfigure(api_key="vck_y", kind="auto")
        assert client.profile.kind == KIND_VERCEL

    def test_invalid_question_reports_question_error(self):
        client = JevClient(api_key="apikey_x")
        result = _run(client.noul("state", "   "))
        assert result.ok is False
        assert result.error.startswith("question_error")

    def test_gateway_error_message_is_actionable(self):
        client = JevClient(api_key="apikey_x", kind=KIND_VERCEL)
        message = client._describe_http_error(401, '{"message":"Authentication failed"}')
        assert "401" in message
        assert "typesafe" in message.lower()

    def test_model_error_message_names_the_model(self):
        client = JevClient(api_key="apikey_x", kind=KIND_TYPESAFE, model="nope/nope")
        message = client._describe_http_error(400, '{"message":"Unknown model: nope/nope"}')
        assert "nope/nope" in message


# ----------------------------------------------------------------------
# 插件侧 mixin
# ----------------------------------------------------------------------


class _MixinHarness:
    """Bare host exposing only what the mixin reads."""

    def __init__(self, **settings):
        self.recorded: list[dict] = []
        for key, value in settings.items():
            setattr(self, key, value)


def _mixin_host(**settings):
    from astrbot_plugin_private_companion.jev_decision import JevDecisionMixin

    class Host(JevDecisionMixin, _MixinHarness):
        def _llm_daily_budget_remaining(self):
            return 100

        def _record_llm_usage(self, **kwargs):
            self.recorded.append(kwargs)

    host = Host(**settings)
    host.recorded = []
    return host


class TestPluginMixin:
    def test_disabled_by_default(self):
        host = _mixin_host(enable_jev_decision=False, jev_api_key="apikey_x")
        assert host._jev_should_attempt() is False

    def test_enabled_without_key_does_not_attempt(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="")
        assert host._jev_should_attempt() is False

    def test_enabled_with_key_attempts(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x")
        assert host._jev_should_attempt() is True

    def test_zero_daily_budget_stops_attempts(self):
        from astrbot_plugin_private_companion.jev_decision import JevDecisionMixin

        class Host(JevDecisionMixin, _MixinHarness):
            def _llm_daily_budget_remaining(self):
                return 0

        host = Host(enable_jev_decision=True, jev_api_key="apikey_x")
        assert host._jev_should_attempt() is False

    def test_endpoint_kind_defaults_to_auto(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x")
        assert host._jev_settings()["kind"] == "auto"

    def test_invalid_endpoint_kind_is_ignored(self):
        host = _mixin_host(
            enable_jev_decision=True, jev_api_key="apikey_x", jev_endpoint_kind="bogus"
        )
        # 校验发生在 bootstrap 的 _cfg 边界；mixin 只保证不因此崩溃。
        settings = host._jev_settings()
        assert settings["kind"] in {"auto", "bogus"}

    def test_timeout_is_clamped_to_a_sane_range(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x", jev_timeout_seconds=0.0001)
        assert host._jev_settings()["timeout"] >= 0.15
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x", jev_timeout_seconds=9999)
        assert host._jev_settings()["timeout"] <= 30.0

    def test_diagnostics_before_initialisation_reports_inactive(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x")
        diagnostics = host.jev_diagnostics()
        assert diagnostics["active"] is False
        assert diagnostics["api_key_set"] is True

    def test_gate_uses_auto_detected_endpoint_for_typesafe_key(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="apikey_x")
        gate = host._jev_gate()
        assert gate.client.profile.kind == KIND_TYPESAFE

    def test_gate_uses_auto_detected_endpoint_for_vercel_key(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="vck_x")
        gate = host._jev_gate()
        assert gate.client.profile.kind == KIND_VERCEL
        assert gate.client.profile.model == VERCEL_DEFAULT_MODEL

    def test_diagnostics_reports_gateway_and_breaker(self):
        host = _mixin_host(enable_jev_decision=True, jev_api_key="vck_x")
        host._jev_gate()
        diagnostics = host.jev_diagnostics()
        assert diagnostics["active"] is True
        assert diagnostics["endpoint"].startswith("vercel")
        assert "breaker" in diagnostics
        assert diagnostics["breaker"]["open"] is False

    def test_disabled_mixin_returns_none_without_network(self):
        host = _mixin_host(enable_jev_decision=False, jev_api_key="apikey_x")
        result = _run(host._jev_noul(task=TASK_GROUP_FOLLOWUP, state="s", instructions="i"))
        assert result is None

    def test_usage_is_recorded_into_the_plugin_ledger(self):
        from astrbot_plugin_private_companion.jev_decision import JevDecisionMixin

        recorded: list[dict] = []

        class Host(JevDecisionMixin, _MixinHarness):
            def _llm_daily_budget_remaining(self):
                return 100

            def _record_llm_usage(self, **kwargs):
                recorded.append(kwargs)

        host = Host(enable_jev_decision=True, jev_api_key="apikey_x")
        host._jev_gate_obj = JevGate(
            client=_FakeClient([_noul_result(0.95)]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        host._jev_count_toward_limit = True
        result = _run(host._jev_noul(task=TASK_GROUP_FOLLOWUP, state="s", instructions="i"))
        assert result is True
        assert len(recorded) == 1
        entry = recorded[0]
        assert entry["provider_id"] == "jev:systemone"
        assert entry["task"] == f"jev_{TASK_GROUP_FOLLOWUP}"
        assert entry["budget_exempt"] is False
        # The shim must expose usage in the shape _extract_llm_usage parses.
        assert entry["resp"].usage["input_tokens"] == 300

    def test_failed_call_records_no_usage(self):
        from astrbot_plugin_private_companion.jev_decision import JevDecisionMixin

        recorded: list[dict] = []

        class Host(JevDecisionMixin, _MixinHarness):
            def _llm_daily_budget_remaining(self):
                return 100

            def _record_llm_usage(self, **kwargs):
                recorded.append(kwargs)

        host = Host(enable_jev_decision=True, jev_api_key="apikey_x")
        host._jev_gate_obj = JevGate(
            client=_FakeClient([JevResult(ok=False, error="boom")]),
            master_enabled=True,
            enabled_tasks=[TASK_GROUP_FOLLOWUP],
        )
        host._jev_count_toward_limit = True
        assert _run(host._jev_noul(task=TASK_GROUP_FOLLOWUP, state="s", instructions="i")) is None
        assert recorded == []

    def test_usage_shim_matches_token_budget_parser(self):
        """Verify the shim against the real ledger parser when it is importable."""
        token_budget = pytest.importorskip("astrbot_plugin_private_companion.token_budget")
        if not hasattr(token_budget, "TokenBudgetMixin"):
            pytest.skip("TokenBudgetMixin unavailable")
        from astrbot_plugin_private_companion.jev_decision import _JevUsageShim

        mixin = token_budget.TokenBudgetMixin()
        usage = mixin._extract_llm_usage(
            _JevUsageShim({"input_tokens": 300, "output_tokens": 20, "total_tokens": 320}),
            "prompt",
            "completion",
        )
        assert usage["prompt_tokens"] == 300
        assert usage["completion_tokens"] == 20
        assert usage["total_tokens"] == 320
        assert usage["estimated"] is False


# ----------------------------------------------------------------------
# 接线守卫
# ----------------------------------------------------------------------


class TestWiringGuards:
    def test_config_loader_sets_instance_attributes(self):
        """The first integration read settings that were never loaded.

        ``_persona_value`` resolves instance attributes, so without a bootstrap
        loader ``jev_api_key`` was always "" and the feature was inert.
        """
        source = (ROOT / "plugin_bootstrap.py").read_text(encoding="utf-8")
        assert "self.jev_api_key" in source
        assert "self.enable_jev_decision" in source
        assert "self.jev_endpoint_kind" in source
        assert "_initialize_jev_config(self, c)" in source

    def test_group_wakeup_no_longer_blocks_the_event_loop(self):
        """The old path called urllib synchronously inside an async handler."""
        source = (ROOT / "group_wakeup.py").read_text(encoding="utf-8")
        assert "import urllib" not in source
        assert "evaluate_sync" not in source
        assert "jev_verdict" in source

    def test_engine_is_async_and_reuses_a_session(self):
        """The engine must be async and pool its connection.

        Assert on imports and calls rather than raw substrings: the module
        docstring legitimately mentions urllib when explaining what it replaced.
        """
        import ast

        source = (ROOT / "domains/decision/jev_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "urllib" not in imported, "阻塞式 urllib 不应再被导入"
        assert "aiohttp" in imported

        assert "keepalive_timeout" in source
        async_methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        assert "evaluate" in async_methods
        assert "post" in {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }

    def test_interject_prefilter_only_skips_on_confident_no(self):
        """The interject prefilter may only ever *remove* a generation call.

        It returns False solely when JEV is confident the bot should stay out;
        every other outcome (disabled, unavailable, unsure, or a yes) must
        return None so the original model path still produces the reply text.
        """
        import ast

        source = (ROOT / "group_observation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "_group_interject_jev_prefilter" in methods

        # The call site must guard on `is False`, never on a falsy check, or an
        # abstaining None would be treated as "do not interject".
        assert "if jev_prefilter is False:" in source
        assert "if not jev_prefilter:" not in source