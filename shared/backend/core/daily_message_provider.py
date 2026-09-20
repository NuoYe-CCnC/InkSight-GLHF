"""License-gated adapters for possible daily-message services.

This module intentionally performs no background work and is not wired into
the production scheduler.  A provider must pass the content-rights gate before
``fetch_approved`` can reach the network.  As of the 2026-09-14 audit, none of
the researched providers passes that gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable


class ProviderError(ValueError):
    """Provider data is malformed or unsafe for the panel."""


class ContentRightsUnverified(ProviderError):
    """Network/cache/display use is blocked pending content-rights evidence."""


PROVIDERS = {
    "affirmations": {
        "endpoint": "https://www.affirmations.dev",
        "documentation": "https://github.com/annthurium/affirmations",
        "language": "en",
        "software_license": "MIT",
        "content_license": "unverified",
        "attribution": "unverified",
        "free_limit": "not stated in reviewed official README",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "software MIT notice does not establish each sentence's rights",
    },
    "zenquotes": {
        "endpoint": "https://zenquotes.io/api/today",
        "documentation": "https://docs.zenquotes.io/zenquotes-documentation/",
        "language": "en",
        "software_license": "not applicable to hosted API",
        "content_license": "unverified",
        "attribution": "free use requires a link to zenquotes.io",
        "free_limit": "5 requests per 30 seconds per IP by default",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "attribution and API limits do not prove quotation rights or translation rights",
    },
    "quotable": {
        "endpoint": "https://api.quotable.io/quotes/random?limit=1&maxLength=96",
        "documentation": "https://github.com/lukePeavey/quotable",
        "language": "en",
        "software_license": "MIT for API implementation",
        "content_license": "unverified",
        "attribution": "not confirmed for each quotation",
        "free_limit": "180 requests per minute per IP in reviewed README",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "data repository declares open source but exposes no verified data license file",
    },
    "ctext": {
        "endpoint": "https://api.ctext.org/",
        "documentation": "https://ctext.org/tools/api",
        "language": "zh-classical",
        "software_license": "not applicable to hosted API",
        "content_license": "work-and-edition-specific",
        "attribution": "source citation required by normal academic practice",
        "free_limit": "limited unauthenticated textual access; higher access varies by account",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "modern translations remain copyrighted; exact work and edition must be verified",
    },
    "hitokoto": {
        "endpoint": "https://v1.hitokoto.cn/?encode=json",
        "documentation": "https://developer.hitokoto.cn/sentence/",
        "language": "zh",
        "software_license": "Apache-2.0 API code; AGPL-3.0 sentence bundle",
        "content_license": "mixed and not wholly owned by provider",
        "attribution": "provider asks callers to link the sentence UUID",
        "free_limit": "documented endpoint QPS 2",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "official sentence-bundle notice acknowledges third-party ownership and removals",
    },
    "jinrishici": {
        "endpoint": "https://v2.jinrishici.com/one.json",
        "documentation": "https://www.jinrishici.com/doc/",
        "language": "zh-classical",
        "software_license": "not applicable to hosted API",
        "content_license": "network-sourced; provider disclaims infringement/error risk",
        "attribution": "terms apply; commercial use is not offered",
        "free_limit": "5 seconds per IP up to 25 requests in reviewed terms",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "not suitable as a general open-source default; token/IP privacy duties also apply",
    },
    "wikimedia": {
        "endpoint": "https://zh.wikiquote.org/w/api.php",
        "documentation": "https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines",
        "language": "zh",
        "software_license": "MediaWiki project-specific",
        "content_license": "usually CC BY-SA 4.0/GFDL, with page-specific exceptions",
        "attribution": "link to the reused page or equivalent author history",
        "free_limit": "dynamic rate limits; identify User-Agent and obey backoff",
        "release_eligible": False,
        "cache_allowed": False,
        "reason": "random pages can include modern/fair-use material; exact page and revision need review",
    },
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML = re.compile(r"<[^>]*>")
_HAN = re.compile(r"[\u3400-\u9fff]")


def provider_matrix() -> dict:
    """Return detached provider metadata for UI/docs without network access."""
    return json.loads(json.dumps(PROVIDERS, ensure_ascii=False))


def _extract(provider_id: str, payload: Any) -> tuple[str, str | None, str]:
    if provider_id == "affirmations" and isinstance(payload, dict):
        return payload.get("affirmation"), None, ""
    if provider_id == "zenquotes" and isinstance(payload, list) and payload:
        row = payload[0] if isinstance(payload[0], dict) else {}
        return row.get("q"), row.get("a"), str(row.get("h") or "")
    if provider_id == "quotable":
        row = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(row, dict):
            return row.get("content"), row.get("author"), str(row.get("_id") or "")
    if provider_id == "hitokoto" and isinstance(payload, dict):
        return payload.get("hitokoto"), payload.get("from_who"), str(payload.get("uuid") or "")
    if provider_id == "jinrishici" and isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
        return data.get("content"), origin.get("author"), str(data.get("id") or "")
    raise ProviderError(f"unsupported or malformed provider payload: {provider_id}")


def normalize_payload(provider_id: str, payload: Any, *, fetched_at: int | None = None) -> dict:
    """Normalize untrusted JSON; never translate, truncate, render HTML, or call a model."""
    spec = PROVIDERS.get(provider_id)
    if not spec:
        raise ProviderError(f"unknown provider: {provider_id}")
    text, author, remote_id = _extract(provider_id, payload)
    if not isinstance(text, str):
        raise ProviderError("message text is missing")
    text = unicodedata.normalize("NFC", text).strip()
    if not text or _CONTROL.search(text) or _HTML.search(text) or "\n" in text or "\r" in text:
        raise ProviderError("message contains unsupported markup or control characters")
    if len(text) > 96 or len(text.encode("utf-8")) > 384:
        raise ProviderError("message exceeds panel budget; it was not truncated")
    lang = "zh" if _HAN.search(text) else "en"
    stamp = int(fetched_at if fetched_at is not None else datetime.now(timezone.utc).timestamp())
    digest = hashlib.sha256(f"{provider_id}|{remote_id}|{text}".encode("utf-8")).hexdigest()[:20]
    return {
        "id": remote_id or digest,
        "text": text,
        "lang": lang,
        "source": provider_id,
        "source_url": spec["endpoint"],
        "author": author.strip() if isinstance(author, str) and author.strip() else None,
        "content_license": spec["content_license"],
        "license_evidence_url": spec["documentation"],
        "fetched_at": stamp,
        "release_eligible": bool(spec["release_eligible"]),
        "cache_allowed": bool(spec["cache_allowed"]),
    }


def panel_eligible(item: dict) -> tuple[bool, str]:
    """Require verified rights and Chinese text before an item may reach the panel."""
    if item.get("release_eligible") is not True:
        return False, "content-rights-unverified"
    if item.get("lang") != "zh":
        return False, "chinese-translation-unavailable"
    if item.get("content_license") in (None, "", "unverified"):
        return False, "content-license-missing"
    return True, "ok"


def cache_eligible(item: dict) -> bool:
    ok, _ = panel_eligible(item)
    return ok and item.get("cache_allowed") is True


def fetch_approved(provider_id: str, *, timeout: float = 8.0,
                   opener: Callable = urllib.request.urlopen) -> dict:
    """Fetch one item only after the provider has passed the release gate."""
    spec = PROVIDERS.get(provider_id)
    if not spec:
        raise ProviderError(f"unknown provider: {provider_id}")
    if spec["release_eligible"] is not True:
        raise ContentRightsUnverified(spec["reason"])
    request = urllib.request.Request(
        spec["endpoint"], headers={"Accept": "application/json",
                                   "User-Agent": "InkSight/1.0 daily-message audit"})
    with opener(request, timeout=timeout) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ProviderError("provider response exceeds 64 KiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider response is not UTF-8 JSON") from exc
    item = normalize_payload(provider_id, payload)
    ok, reason = panel_eligible(item)
    if not ok:
        raise ProviderError(reason)
    return item


def retry_delays() -> tuple[int, ...]:
    """Bounded retry plan for a future approved provider; no request is made here."""
    return (0, 60, 300)
