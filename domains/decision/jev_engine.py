# -*- coding: utf-8 -*-
"""
Jev System One Decision Engine Client for AstrBot Private Companion.
Uses Vercel AI Gateway endpoint: https://ai-gateway.vercel.sh/typesafe/v1/systemone
Model: typesafe-ai/jev
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import time
from typing import Any, Mapping

logger = logging.getLogger("astrbot_plugin_private_companion.jev_engine")

DEFAULT_JEV_GATEWAY_URL = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
DEFAULT_JEV_MODEL = "typesafe-ai/jev"


class JevDecisionEngine:
    """TypeSafe Jev System One Decision Client."""

    def __init__(
        self,
        api_key: str = "",
        gateway_url: str = DEFAULT_JEV_GATEWAY_URL,
        model: str = DEFAULT_JEV_MODEL,
        timeout: float = 3.0,
    ):
        self.api_key = (api_key or "").strip()
        self.gateway_url = (gateway_url or DEFAULT_JEV_GATEWAY_URL).strip()
        self.model = (model or DEFAULT_JEV_MODEL).strip()
        self.timeout = max(0.5, float(timeout or 3.0))

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def evaluate_sync(
        self,
        state: str | Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Synchronously evaluate state against typed questions."""
        if not self.is_configured:
            return {}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }

        t0 = time.time()
        try:
            req = urllib.request.Request(
                self.gateway_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                elapsed = time.time() - t0
                logger.debug("Jev evaluation completed in %.1fms", elapsed * 1000)
                return data.get("answers", {})
        except urllib.error.HTTPError as e:
            logger.warning("Jev HTTP error %s: %s", e.code, e.read().decode("utf-8", errors="ignore"))
            return {}
        except Exception as e:
            logger.warning("Jev evaluation error: %s", e)
            return {}

    def should_group_reply(
        self,
        text: str,
        *,
        bot_name: str = "和泉纱雾",
        scene: Mapping[str, Any] | None = None,
        threshold: float = 0.65,
    ) -> bool | None:
        """
        Evaluate if bot should reply to a group message.
        Returns:
            True: Jev confident should reply
            False: Jev confident should NOT reply
            None: Jev unavailable or unconfigured (fallback to rules)
        """
        if not self.is_configured or not text.strip():
            return None

        state_info = f"群聊发言：{text}\nBot身份：{bot_name}"
        if scene:
            state_info += f"\n对话场景：{json.dumps(scene, ensure_ascii=False)}"

        questions = {
            "is_asking_to_stop": {
                "type": "noul",
                "instructions": f"发言者是否明确让Bot({bot_name})闭嘴、不要回复、走开或指出不是在问Bot？"
            },
            "is_relevant_or_inviting": {
                "type": "noul",
                "instructions": f"发言者是否在呼唤Bot({bot_name})、向其提问、或提出Bot适合自然参与的话题？"
            }
        }

        answers = self.evaluate_sync(state_info, questions)
        if not answers:
            return None

        stop_score = answers.get("is_asking_to_stop", {}).get("noul", 0.0)
        invite_score = answers.get("is_relevant_or_inviting", {}).get("noul", 0.0)

        # If user explicitly told bot to stop/shut up
        if stop_score >= 0.7:
            logger.info("Jev: user asking bot to stop (score=%.2f)", stop_score)
            return False

        # If user invited or asked bot
        if invite_score >= threshold:
            logger.info("Jev: bot invited to reply (score=%.2f)", invite_score)
            return True

        return False

    def is_user_asking_to_stop(self, text: str) -> bool | None:
        """Evaluate if user in private or group chat is asking bot to shut up."""
        if not self.is_configured or not text.strip():
            return None

        questions = {
            "should_stop": {
                "type": "noul",
                "instructions": "发言者是否要求对方闭嘴、别回了、不要理我、停止说话？"
            }
        }
        answers = self.evaluate_sync(text, questions)
        if not answers:
            return None
        return answers.get("should_stop", {}).get("noul", 0.0) >= 0.7
