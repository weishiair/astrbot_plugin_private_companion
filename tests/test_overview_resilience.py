# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


class _OverviewPlugin:
    def __init__(self) -> None:
        self._data_lock = asyncio.Lock()
        self.data = {"users": {}, "groups": {}}
        self.enabled = True
        self.bot_name = "test"

    @staticmethod
    def _configured_group_ids() -> list[str]:
        return []

    @staticmethod
    def _configured_group_blacklist_ids() -> list[str]:
        return []

    @staticmethod
    def _group_allowed_by_access_mode(_group_id: str) -> bool:
        return True

    @staticmethod
    def _roleplay_knowledge_summary() -> dict:
        return {"available": True}

    @staticmethod
    def jev_diagnostics() -> dict:
        return {"enabled": True, "active": True, "warmed": True}


def test_overview_survives_one_broken_optional_section() -> None:
    api = PrivateCompanionPageApi(_OverviewPlugin())
    api._overview_data_snapshot_locked = lambda _data: {"users": {}, "groups": {}}
    api._reaction_expression_runtime_summary = lambda _data: {}
    api._token_overview_payload = lambda _usage, _balance: {}

    section_methods = (
        "_feature_flags",
        "_proactive_intensity_summary",
        "_proactive_only_mode_snapshot",
        "_body_monitor_integration_summary",
        "_provider_settings",
        "_runtime_settings",
        "_deepseek_peak_routing_summary",
        "_livingmemory_summary",
        "_req041_runtime_summary",
    )
    for method_name in section_methods:
        setattr(api, method_name, lambda: {"available": True})

    data_section_methods = (
        "_proactive_chat_summary",
        "_expression_learning_scope_summary",
        "_cache_summary",
        "_screen_companion_summary",
        "_worldbook_summary",
        "_proactive_candidate_summary",
        "_proactive_task_summary",
        "_message_debounce_summary",
        "_bilibili_summary",
        "_news_summary",
        "_web_exploration_summary",
        "_qzone_summary",
        "_jm_cosmos_summary",
        "_creative_summary",
        "_skill_growth_summary",
        "_personal_goal_summary",
        "_food_menu_summary",
        "_external_ability_summary",
        "_life_observation_summary",
        "_daily_state_summary",
        "_daily_timeline_summary",
        "_daily_outfit_summary",
    )
    for method_name in data_section_methods:
        setattr(api, method_name, lambda _data: {"available": True})

    async def bookshelf_summary(_data, *, unlocked: bool) -> dict:
        return {"available": True, "unlocked": unlocked}

    def broken_livingmemory() -> dict:
        raise RuntimeError("optional extension probe failed")

    api._bookshelf_summary = bookshelf_summary
    api._livingmemory_summary = broken_livingmemory

    result = asyncio.run(api.get_overview())

    assert result["success"] is True
    overview = result["data"]
    assert overview["livingmemory"] == {}
    assert overview["news"] == {"available": True}
    assert overview["jev"] == {"enabled": True, "active": True, "warmed": True}
    assert overview["overview_health"] == {
        "degraded": True,
        "sections": ["livingmemory"],
    }


def test_companion_plugin_summary_tolerates_content_status_failure() -> None:
    plugin = _OverviewPlugin()

    def broken_content_status() -> dict:
        raise RuntimeError("content extension is still starting")

    plugin._content_companion_status = broken_content_status
    summary = PrivateCompanionPageApi(plugin)._companion_plugins_summary()

    assert summary["content"] == {
        "installed": False,
        "enabled": False,
        "available": False,
        "reason": "content_companion_unavailable",
    }
