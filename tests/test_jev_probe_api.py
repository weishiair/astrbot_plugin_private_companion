# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


class JevProbeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_wraps_only_the_probe_snapshot(self) -> None:
        expected = {
            "status": "ok",
            "source": "manual",
            "started_at": 1.0,
            "finished_at": 2.0,
            "elapsed_ms": 15,
            "http_status": 200,
            "error": "",
        }

        async def probe():
            return dict(expected)

        api = PrivateCompanionPageApi(SimpleNamespace(jev_probe=probe))
        response = await api.run_jev_probe()
        self.assertTrue(response["success"])
        self.assertEqual(response["data"], expected)

    async def test_missing_probe_capability_returns_503(self) -> None:
        api = PrivateCompanionPageApi(SimpleNamespace())
        response = await api.run_jev_probe()
        self.assertFalse(response["success"])
        self.assertEqual(response.http_status, 503)

    async def test_exception_response_does_not_echo_sensitive_details(self) -> None:
        async def probe():
            raise RuntimeError("Authorization: Bearer should-not-escape")

        api = PrivateCompanionPageApi(SimpleNamespace(jev_probe=probe))
        response = await api.run_jev_probe()
        self.assertFalse(response["success"])
        self.assertEqual(response.http_status, 500)
        self.assertNotIn("should-not-escape", str(response))


if __name__ == "__main__":
    unittest.main()
