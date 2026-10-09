"""Read-only menu quota. Never probe Codex or refresh a successful timestamp."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from . import codex_collector_health, data_cache, desktop_service
from .freshness_config import ai_source_policy


def account_key() -> str | None:
    """Compare local login identity without returning any credential or id."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        identity = tokens.get("account_id") if isinstance(tokens, dict) else None
        if not isinstance(identity, str) or not identity:
            return None
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    except (OSError, ValueError, TypeError):
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def snapshot(*, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ttl = ai_source_policy()["codex"]["stale_after_s"]
    result = {"remaining_percent": None, "last_success_at": None,
              "expires_at": None, "max_age_seconds": ttl,
              "source": "Codex 本机采集 · 7D", "reason": "unavailable",
              "paused": desktop_service.gate.paused}
    group = data_cache.get_group("ai.codex") or {}
    value = group.get("value")
    if not isinstance(value, dict):
        return result
    stamp = group.get("last_success")
    if _number(stamp) and stamp > 0:
        result.update(last_success_at=stamp, expires_at=stamp + ttl)
    else:
        result["reason"] = "invalid_timestamp"
        return result
    if not 0 <= now - stamp < ttl:
        result["reason"] = "stale"
        return result
    if group.get("status") != "fresh":
        return result
    health = codex_collector_health.snapshot(now=int(now), stale_after=ttl)
    if (health["status"] != "current" or not _number(health.get("last_success_at"))
            or not stamp <= health["last_success_at"] <= now):
        result["reason"] = "collector_unavailable"
        return result
    identity = account_key()
    if not identity or identity != value.get("account_key"):
        result["reason"] = "account_mismatch"
        return result
    if value.get("source") != "mac":
        result["reason"] = "wrong_source"
        return result
    duration = value.get("duration_minutes")
    used = value.get("used_percent")
    if not _number(duration) or duration != 10080:
        result["reason"] = "missing_7d"
        return result
    if not _number(used) or not 0 <= used <= 100:
        result["reason"] = "invalid_percent"
        return result
    # Match the panel's lround convention. Zero is a valid exhausted quota.
    result.update(remaining_percent=math.floor(100 - used + 0.5), reason=None)
    return result
