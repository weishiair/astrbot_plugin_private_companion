# -*- coding: utf-8 -*-
"""Jev/System One 端点画像与自动适配。

同时支持两条通路：

- TypeSafe 官方直连：``https://api.typesafe.ai/v1/systemone``，密钥形如 ``apikey_...``，
  模型名 ``jev-latest``。
- Vercel AI Gateway 转发：``https://ai-gateway.vercel.sh/typesafe/v1/systemone``，
  密钥形如 ``vck_...``，模型名走网关命名空间 ``typesafe-ai/jev``。

两个端点使用同一套请求/响应协议，差异只在主机、密钥体系和模型命名空间，所以这里把
差异收敛成一份画像，由客户端在选择端点时消费。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

KIND_AUTO = "auto"
KIND_TYPESAFE = "typesafe"
KIND_VERCEL = "vercel"
KIND_CUSTOM = "custom"

ENDPOINT_KINDS = (KIND_AUTO, KIND_TYPESAFE, KIND_VERCEL, KIND_CUSTOM)

TYPESAFE_DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_DEFAULT_MODEL = "jev-latest"
TYPESAFE_KEY_PREFIX = "apikey_"

VERCEL_DEFAULT_URL = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
VERCEL_DEFAULT_MODEL = "typesafe-ai/jev"
VERCEL_KEY_PREFIXES = ("vck_", "vck-")

TYPESAFE_HOST_MARKERS = ("api.typesafe.ai", "typesafe.ai")
VERCEL_HOST_MARKERS = ("ai-gateway.vercel.sh", "vercel.sh")


@dataclass(frozen=True)
class JevEndpointProfile:
    """Resolved endpoint identity: where to post, which model, and why."""

    kind: str
    url: str
    model: str
    reason: str

    @property
    def is_vercel(self) -> bool:
        return self.kind == KIND_VERCEL

    def safe_url(self) -> str:
        """URL suitable for logs and diagnostics (no credentials or query)."""
        try:
            parsed = urlsplit(self.url)
            netloc = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except Exception:
            return str(self.url or "").split("?", 1)[0].split("#", 1)[0][:400]

    def describe(self) -> str:
        return f"{self.kind} ({self.safe_url()}, model={self.model})"


def _text(value: Any, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def infer_kind(api_key: str = "", gateway_url: str = "") -> tuple[str, str]:
    """Infer the endpoint family from the key prefix, then from the URL host.

    The key prefix is the stronger signal: a ``vck_`` credential is only valid on
    the Vercel gateway, and an ``apikey_`` credential only on TypeSafe directly,
    regardless of what URL was left in the field.
    """
    key = _text(api_key, 200).lower()
    url = _text(gateway_url, 400).lower()
    if any(key.startswith(prefix) for prefix in VERCEL_KEY_PREFIXES):
        return KIND_VERCEL, "api_key_prefix"
    if key.startswith(TYPESAFE_KEY_PREFIX):
        return KIND_TYPESAFE, "api_key_prefix"
    if url:
        if any(marker in url for marker in VERCEL_HOST_MARKERS):
            return KIND_VERCEL, "gateway_url_host"
        if any(marker in url for marker in TYPESAFE_HOST_MARKERS):
            return KIND_TYPESAFE, "gateway_url_host"
    return KIND_TYPESAFE, "default"


def classify_url(gateway_url: str) -> str:
    """Classify a URL by host, or ``""`` when it is not a known family."""
    url = _text(gateway_url, 400).lower()
    if not url:
        return ""
    if any(marker in url for marker in VERCEL_HOST_MARKERS):
        return KIND_VERCEL
    if any(marker in url for marker in TYPESAFE_HOST_MARKERS):
        return KIND_TYPESAFE
    return ""


def resolve_profile(
    *,
    kind: str = KIND_AUTO,
    api_key: str = "",
    gateway_url: str = "",
    model: str = "",
) -> JevEndpointProfile:
    """Resolve the endpoint to use.

    The endpoint family is decided in this order: explicit ``kind`` → API-key
    prefix → gateway-URL host → TypeSafe default.

    The URL is honoured unless it belongs to a *different* known family than the
    credential does. In that case it is ignored and the conflict is recorded in
    ``reason``: a ``vck_`` key cannot authenticate against the TypeSafe host (nor
    the reverse), so preferring the stale URL would only produce a 401. This
    matters on upgrade, because older versions shipped the Vercel URL as the
    schema default and existing installs still have it persisted.
    """
    requested_kind = _text(kind, 24).lower() or KIND_AUTO
    if requested_kind not in ENDPOINT_KINDS:
        requested_kind = KIND_AUTO

    url_override = _text(gateway_url, 400)
    model_override = _text(model, 200)
    url_family = classify_url(url_override)

    def family_defaults(family: str) -> tuple[str, str]:
        if family == KIND_VERCEL:
            return VERCEL_DEFAULT_URL, VERCEL_DEFAULT_MODEL
        return TYPESAFE_DEFAULT_URL, TYPESAFE_DEFAULT_MODEL

    if requested_kind == KIND_CUSTOM:
        default_url, default_model = family_defaults(KIND_TYPESAFE)
        return JevEndpointProfile(
            kind=KIND_CUSTOM,
            url=url_override or default_url,
            model=model_override or default_model,
            reason="configured_custom",
        )

    if requested_kind in (KIND_TYPESAFE, KIND_VERCEL):
        resolved_kind, reason = requested_kind, "configured"
    else:
        inferred, reason = infer_kind(api_key, url_override)
        resolved_kind = inferred if inferred != KIND_CUSTOM else KIND_TYPESAFE

    default_url, default_model = family_defaults(resolved_kind)

    if not url_override:
        url = default_url
    elif url_family and url_family != resolved_kind:
        # Credential and URL disagree; the credential decides.
        url = default_url
        reason = "api_key_family_over_gateway_url"
    else:
        # Same family, or a URL we cannot classify (self-hosted mirror/proxy).
        url = url_override

    if url_override and not url_family and requested_kind == KIND_AUTO:
        # Honour an unclassifiable URL verbatim, but say so, because the model
        # namespace cannot be inferred from it.
        reason = "custom_gateway_url"

    return JevEndpointProfile(
        kind=resolved_kind,
        url=url,
        model=model_override or default_model,
        reason=reason,
    )


def credential_hint(profile: JevEndpointProfile, api_key: str) -> str:
    """Return an actionable note when the credential cannot work on the endpoint.

    Used for diagnostics only; the gate never blocks a call on this, because a
    proxy or a future gateway may accept either credential shape.
    """
    key = _text(api_key, 200).lower()
    if not key:
        return "未填写 API Key。"
    if profile.kind == KIND_VERCEL and not any(key.startswith(p) for p in VERCEL_KEY_PREFIXES):
        return "当前走 Vercel AI Gateway，但 Key 不像 vck_ 开头的网关 Key；若这是 TypeSafe 官方 Key，请把端点族设为 typesafe 或改为 auto。"
    if profile.kind == KIND_TYPESAFE and key.startswith(VERCEL_KEY_PREFIXES):
        return "当前走 TypeSafe 官方直连，但 Key 是 vck_ 开头的网关 Key；请把端点族设为 vercel 或改为 auto。"
    return ""


__all__ = [
    "ENDPOINT_KINDS",
    "JevEndpointProfile",
    "KIND_AUTO",
    "KIND_CUSTOM",
    "KIND_TYPESAFE",
    "KIND_VERCEL",
    "TYPESAFE_DEFAULT_MODEL",
    "TYPESAFE_DEFAULT_URL",
    "VERCEL_DEFAULT_MODEL",
    "VERCEL_DEFAULT_URL",
    "classify_url",
    "credential_hint",
    "infer_kind",
    "resolve_profile",
]
