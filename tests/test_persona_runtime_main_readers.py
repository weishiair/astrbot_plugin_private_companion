from __future__ import annotations

import ast
import asyncio
import copy
import json
import logging
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_private_companion.persona_config import runtime_persona_setting
from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    prompt_section,
    render_prompt_sections,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_methods(*names: str) -> dict[str, Any]:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin"
    )
    selected = [
        copy.deepcopy(node) for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    for node in selected:
        node.decorator_list = []
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": Any,
        "json": json,
        "logger": logging.getLogger(__name__),
        "re": re,
        "runtime_persona_setting": runtime_persona_setting,
        "PromptRenderMode": PromptRenderMode,
        "PromptSection": PromptSection,
        "prompt_section": prompt_section,
        "render_prompt_sections": render_prompt_sections,
        "time": time,
        "_single_line": lambda value, limit=240: " ".join(str(value or "").split())[:limit],
    }
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return {name: namespace[name] for name in names}


METHODS = _load_methods(
    "_normalize_persona_voice_text",
    "_format_persona_voice_channel_prompt_section",
    "_format_persona_voice_channel_prompt",
    "_review_group_question_wakeup_reply_before_send",
    "_rest_reply_llm_score",
)


class _MainReadersHarness:
    _normalize_persona_voice_text = METHODS["_normalize_persona_voice_text"]
    _format_persona_voice_channel_prompt_section = METHODS[
        "_format_persona_voice_channel_prompt_section"
    ]
    _format_persona_voice_channel_prompt = METHODS["_format_persona_voice_channel_prompt"]
    _review_group_question_wakeup_reply_before_send = METHODS["_review_group_question_wakeup_reply_before_send"]
    _rest_reply_llm_score = METHODS["_rest_reply_llm_score"]

    enable_persona_voice_channels = True
    persona_conversation_voice_prompt = "主人格对话风格"
    response_review_provider_id = "primary-review"
    group_followup_judge_provider_id = "primary-followup"
    mai_style_provider_id = "primary-style"
    rest_wakeup_provider_id = "primary-wake"
    llm_provider_id = "primary-llm"

    def __init__(self) -> None:
        self.values = {
            "persona_conversation_voice_prompt": "次人格对话风格",
            "response_review_provider_id": "persona-review",
            "group_followup_judge_provider_id": "persona-followup",
            "mai_style_provider_id": "persona-style",
            "rest_wakeup_provider_id": "persona-wake",
            "llm_provider_id": "persona-llm",
            "rest_reply_llm_threshold": 65,
        }
        self.provider_calls: list[str] = []
        self.jev_calls: list[dict[str, Any]] = []
        self.jev_decision: bool | None = None
        self.jev_error: Exception | None = None

    def persona_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, getattr(self, key, default))

    @staticmethod
    def _task_provider(*values: Any) -> str:
        return next((str(value) for value in values if value), "")

    @staticmethod
    def _extract_group_id_from_event(_event: Any) -> str:
        return "group-1"

    @staticmethod
    def _get_group(_group_id: str) -> dict[str, Any]:
        return {}

    @staticmethod
    def _format_group_recent_flow_for_review(*_args: Any, **_kwargs: Any) -> str:
        return ""

    async def _llm_call(self, _prompt: str, *, provider_id: str = "", **_kwargs: Any) -> str:
        self.provider_calls.append(provider_id)
        return '{"decision":"send","reason":"ok","score":80,"should_reply":true}'

    async def _jev_noul(self, **kwargs: Any) -> bool | None:
        self.jev_calls.append(dict(kwargs))
        if self.jev_error is not None:
            raise self.jev_error
        return self.jev_decision

    @staticmethod
    def _parse_json_object(value: str) -> dict[str, Any]:
        return json.loads(value)

    @staticmethod
    def _extract_json_payload(value: str) -> dict[str, Any]:
        return json.loads(value)


class PersonaRuntimeMainReadersTests(unittest.TestCase):
    @staticmethod
    def _group_event() -> SimpleNamespace:
        return SimpleNamespace(
            private_companion_group_scene={"trigger": "group_wakeup_question", "reason": "test"},
            private_companion_group_text="有人会吗",
            message_str="有人会吗",
            get_sender_id=lambda: "member-1",
        )

    def test_voice_prompt_uses_active_persona_value(self) -> None:
        harness = _MainReadersHarness()
        text = harness._format_persona_voice_channel_prompt("conversation")

        self.assertIn("次人格对话风格", text)
        self.assertNotIn("主人格对话风格", text)

    def test_group_and_rest_review_use_active_persona_providers(self) -> None:
        harness = _MainReadersHarness()
        event = self._group_event()

        review = asyncio.run(
            harness._review_group_question_wakeup_reply_before_send(event, reply_text="我可以帮忙")
        )
        score, _reason = asyncio.run(
            harness._rest_reply_llm_score(
                text="醒醒",
                schedule_text="休息",
                runtime={"label": "午休"},
                is_private_chat=True,
            )
        )

        self.assertEqual("send", review["decision"])
        self.assertEqual(80, score)
        self.assertEqual(["persona-review", "persona-wake"], harness.provider_calls)
        self.assertEqual("group_question_wakeup_reply_review", harness.jev_calls[0]["task"])

    def test_group_question_review_jev_decisions_skip_model(self) -> None:
        event = self._group_event()
        for delegated, expected in ((True, "send"), (False, "drop")):
            with self.subTest(delegated=delegated):
                harness = _MainReadersHarness()
                harness.jev_decision = delegated

                result = asyncio.run(
                    harness._review_group_question_wakeup_reply_before_send(
                        event,
                        reply_text="可以检查一下依赖版本",
                    )
                )

                self.assertEqual(expected, result["decision"])
                self.assertEqual([], harness.provider_calls)
                self.assertEqual(1, len(harness.jev_calls))
                call = harness.jev_calls[0]
                self.assertEqual("group_question_wakeup_reply_review", call["task"])
                self.assertIn("有人会吗", call["state"])
                self.assertIn("可以检查一下依赖版本", call["state"])

    def test_group_question_review_jev_runs_without_model_provider(self) -> None:
        harness = _MainReadersHarness()
        harness.jev_decision = False
        harness.values.update(
            response_review_provider_id="",
            group_followup_judge_provider_id="",
            mai_style_provider_id="",
        )

        result = asyncio.run(
            harness._review_group_question_wakeup_reply_before_send(
                self._group_event(),
                reply_text="我来插一句",
            )
        )

        self.assertEqual("drop", result["decision"])
        self.assertEqual([], harness.provider_calls)

    def test_group_question_review_jev_error_falls_back_to_model(self) -> None:
        harness = _MainReadersHarness()
        harness.jev_error = RuntimeError("offline")

        result = asyncio.run(
            harness._review_group_question_wakeup_reply_before_send(
                self._group_event(),
                reply_text="我可以帮忙",
            )
        )

        self.assertEqual("send", result["decision"])
        self.assertEqual(["persona-review"], harness.provider_calls)


if __name__ == "__main__":
    unittest.main()
