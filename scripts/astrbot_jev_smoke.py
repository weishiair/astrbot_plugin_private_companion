#!/usr/bin/env python3
"""Read-only AstrBot/JEV smoke check, with an explicit opt-in paid probe."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


PLUGIN_NAME = "astrbot_plugin_private_companion"
PAGE_PATH = f"/api/v1/plugins/extensions/{PLUGIN_NAME}/page"


def safe_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def request_json(base_url: str, path: str, api_key: str, *, method: str = "GET") -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    body = b"{}" if method == "POST" else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "private-companion-jev-smoke/1",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(2_000_000)
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}") from None
    except URLError as exc:
        raise RuntimeError(f"{method} {path}: connection failed ({type(exc.reason).__name__})") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"{method} {path}: response was not valid JSON") from None


def unwrap_data(value: Any) -> Any:
    current = value
    for _ in range(4):
        if not isinstance(current, dict) or "data" not in current:
            break
        if current.get("success") is True or set(current) <= {"status", "message", "data", "success", "ts"}:
            current = current["data"]
            continue
        break
    return current


def find_plugin(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        identity = " ".join(
            str(value.get(key) or "")
            for key in ("name", "id", "plugin_name", "module_name", "star_name")
        ).lower()
        if PLUGIN_NAME in identity or "private companion" in identity:
            return value
        for child in value.values():
            found = find_plugin(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_plugin(child)
            if found:
                return found
    return {}


def probe_summary(value: Any) -> dict[str, Any]:
    item = unwrap_data(value)
    if not isinstance(item, dict):
        return {}
    return {
        key: item.get(key)
        for key in (
            "status",
            "source",
            "started_at",
            "finished_at",
            "elapsed_ms",
            "http_status",
            "error",
        )
        if key in item
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the AstrBot plugin and JEV diagnostics without printing credentials."
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="explicitly run one JEV connectivity probe (may consume tokens)",
    )
    args = parser.parse_args()

    base_url = os.environ.get("ASTRBOT_BASE_URL", "").strip()
    api_key = os.environ.get("ASTRBOT_API_KEY", "").strip()
    if not base_url or not api_key:
        print("Set ASTRBOT_BASE_URL and ASTRBOT_API_KEY before running this script.", file=sys.stderr)
        return 2
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print("ASTRBOT_BASE_URL must be an absolute HTTP(S) URL.", file=sys.stderr)
        return 2

    try:
        plugins_payload = request_json(base_url, "/api/v1/plugins", api_key)
        overview_payload = request_json(base_url, f"{PAGE_PATH}/overview", api_key)
        plugin = find_plugin(plugins_payload)
        overview = unwrap_data(overview_payload)
        jev = overview.get("jev", {}) if isinstance(overview, dict) else {}
        totals = jev.get("totals", {}) if isinstance(jev, dict) else {}
        breaker = jev.get("breaker", {}) if isinstance(jev, dict) else {}
        report: dict[str, Any] = {
            "base_url": safe_url(base_url),
            "transport_warning": (
                "AstrBot is using plaintext HTTP; prefer HTTPS before exposing the OpenAPI externally."
                if parsed.scheme == "http"
                else ""
            ),
            "plugin": {
                "found": bool(plugin),
                "version": plugin.get("version") or plugin.get("plugin_version") or "",
                "activated": plugin.get("activated", plugin.get("enabled")),
            },
            "jev": {
                "active": jev.get("active") if isinstance(jev, dict) else None,
                "enabled": jev.get("enabled") if isinstance(jev, dict) else None,
                "warmed": jev.get("warmed") if isinstance(jev, dict) else None,
                "warmup_attempted": jev.get("warmup_attempted") if isinstance(jev, dict) else None,
                "enabled_tasks": jev.get("enabled_tasks", []) if isinstance(jev, dict) else [],
                "attempts": totals.get("attempts") if isinstance(totals, dict) else None,
                "breaker_open": breaker.get("open") if isinstance(breaker, dict) else None,
                "last_probe": probe_summary(jev.get("last_probe", {})) if isinstance(jev, dict) else {},
            },
        }
        if args.probe:
            report["manual_probe"] = probe_summary(
                request_json(base_url, f"{PAGE_PATH}/jev/probe", api_key, method="POST")
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except RuntimeError as exc:
        print(f"Smoke check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
