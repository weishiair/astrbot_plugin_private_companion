# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quart import Quart

from astrbot_plugin_private_companion.page_api import (
    PAGE_API_PREFIX,
    PrivateCompanionPageApi,
)


class _RegisteringContext:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, list[str], str]] = []

    def register_web_api(
        self,
        route: str,
        handler: object,
        methods: list[str],
        description: str,
    ) -> None:
        self.calls.append((route, handler, methods, description))


class PageApiRouteBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = _RegisteringContext()
        self.api = PrivateCompanionPageApi(SimpleNamespace(context=self.context))

    def test_route_bindings_include_overview_and_qzone_method_variants(self) -> None:
        bindings = self.api.route_bindings()
        by_route = {
            (path, tuple(methods)): (handler, description)
            for path, handler, methods, description in bindings
        }

        self.assertIn(("/overview", ("GET",)), by_route)
        self.assertIn(("/jev/probe", ("POST",)), by_route)
        self.assertIn(("/qzone/post", ("GET",)), by_route)
        self.assertIn(("/qzone/post", ("POST",)), by_route)
        self.assertIn(("/bookshelf/unlock", ("POST",)), by_route)
        self.assertIn(("/bookshelf/session", ("GET", "POST")), by_route)
        self.assertIn(("/bookshelf/image", ("GET",)), by_route)
        self.assertIn(("/bookshelf/image_data", ("GET",)), by_route)
        self.assertIn(("/bookshelf/delete", ("POST",)), by_route)
        self.assertIn(("/bookshelf/rate", ("POST",)), by_route)
        self.assertIn(("/bookshelf/tags", ("POST",)), by_route)
        self.assertIn(("/bookshelf/comments/update", ("POST",)), by_route)
        self.assertIn(("/bookshelf/reading_state", ("POST",)), by_route)

        overview_handler = inspect.unwrap(by_route[("/overview", ("GET",))][0])
        jev_probe_handler = inspect.unwrap(by_route[("/jev/probe", ("POST",))][0])
        qzone_get_handler = inspect.unwrap(by_route[("/qzone/post", ("GET",))][0])
        qzone_post_handler = inspect.unwrap(by_route[("/qzone/post", ("POST",))][0])
        self.assertEqual(overview_handler.__name__, "get_overview")
        self.assertEqual(jev_probe_handler.__name__, "run_jev_probe")
        self.assertEqual(qzone_get_handler.__name__, "get_qzone_detail")
        self.assertEqual(qzone_post_handler.__name__, "publish_qzone_post")

    def test_register_routes_reuses_bindings_with_astrbot_prefix(self) -> None:
        bindings = self.api.route_bindings()

        with patch.object(self.api, "route_bindings", return_value=bindings) as route_bindings:
            self.api.register_routes()

        route_bindings.assert_called_once_with()
        self.assertEqual(len(self.context.calls), len(bindings))
        self.assertEqual(
            self.context.calls,
            [
                (f"{PAGE_API_PREFIX}{path}", handler, methods, description)
                for path, handler, methods, description in bindings
            ],
        )
        registered_methods = {
            (route, tuple(methods)) for route, _handler, methods, _description in self.context.calls
        }
        self.assertIn((f"{PAGE_API_PREFIX}/overview", ("GET",)), registered_methods)
        self.assertIn((f"{PAGE_API_PREFIX}/jev/probe", ("POST",)), registered_methods)
        self.assertIn((f"{PAGE_API_PREFIX}/qzone/post", ("GET",)), registered_methods)
        self.assertIn((f"{PAGE_API_PREFIX}/qzone/post", ("POST",)), registered_methods)

    def test_panel_literal_api_calls_match_registered_routes(self) -> None:
        registered = {
            (path, method)
            for path, _handler, methods, _description in self.api.route_bindings()
            for method in methods
        }
        registered_paths = {path for path, _method in registered}
        call_pattern = re.compile(
            r"\b(?P<helper>postJson|fetchJson)\(\s*[\"'`](?P<path>/[^\"'`?${]*)"
        )
        pages_root = Path(__file__).resolve().parents[1] / "pages"

        for panel_dir in ("companion-panel", "陪伴面板"):
            script = (pages_root / panel_dir / "app.js").read_text(encoding="utf-8")
            calls = [
                (match.group("helper"), match.group("path"))
                for match in call_pattern.finditer(script)
            ]
            missing_paths = sorted({path for _helper, path in calls if path not in registered_paths})
            missing_posts = sorted(
                {path for helper, path in calls if helper == "postJson" and (path, "POST") not in registered}
            )
            with self.subTest(panel=panel_dir):
                self.assertEqual([], missing_paths)
                self.assertEqual([], missing_posts)
                self.assertNotIn("disabled_archive", script)


class PageAssetPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_asset_prefix_follows_standalone_and_legacy_request_contexts(self) -> None:
        api = PrivateCompanionPageApi(SimpleNamespace())
        app = Quart(__name__)

        self.assertEqual(api._page_asset_prefix(), PAGE_API_PREFIX)

        async with app.test_request_context("/api/v1/overview"):
            self.assertEqual(api._page_asset_prefix(), "/api/v1")
            self.assertEqual(
                api._bookshelf_image_url(
                    "album 1",
                    data_root=Path.cwd(),
                    page_index=2,
                ),
                "/api/v1/bookshelf/image?album_id=album%201&page=2",
            )

        async with app.test_request_context(f"{PAGE_API_PREFIX}/overview"):
            self.assertEqual(api._page_asset_prefix(), PAGE_API_PREFIX)
            self.assertEqual(
                api._bookshelf_image_url(
                    "album 1",
                    data_root=Path.cwd(),
                    page_index=2,
                ),
                f"{PAGE_API_PREFIX}/bookshelf/image?album_id=album%201&page=2",
            )

    async def test_generic_archive_item_detection_and_id_normalization(self) -> None:
        api = PrivateCompanionPageApi(SimpleNamespace())

        self.assertTrue(api._is_bookshelf_archive_item({"type": "archive_item", "album_id": "42"}))
        self.assertTrue(api._is_bookshelf_archive_item({"key": "archive_item:42"}))
        self.assertTrue(api._is_bookshelf_archive_item({"key": "archive-42"}))
        self.assertFalse(api._is_bookshelf_archive_item({"type": "legacy_archive_item", "album_id": "42"}))
        self.assertFalse(api._is_bookshelf_archive_item({"type": "jm_album", "album_id": "42"}))
        self.assertEqual("42", api._bookshelf_album_id({"id": "archive-42"}))
        self.assertEqual("42", api._bookshelf_album_id({"key": "archive-42"}))


if __name__ == "__main__":
    unittest.main()
