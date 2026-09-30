"""Quality and bounded fallback rules for banked Codex reset expiries.

The weekly rate-limit window and the per-opportunity expiry list are separate
observations.  A successful weekly read must never make an absent expiry list
look freshly verified.  Times in this module are Unix seconds (UTC); timezone
conversion belongs only to the display layer.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any


CACHE_GRACE_SECONDS = 20 * 60
VERIFIED_MAX_AGE_SECONDS = 2 * 60 * 60
MAX_UNIX_SECONDS = 4_102_444_800  # 2100-01-01; also rejects milliseconds.
OBSERVATIONS = {"verified", "zero", "missing", "null", "invalid", "inconsistent", "revoked"}
_FINGERPRINT = re.compile(r"^[0-9a-f]{32}$")


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 100 else None


def _verified_at(payload: dict) -> int | None:
    value = payload.get("reset_expiry_verified_at")
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return int(value)
    # Rows written by older collectors have no independent observation time.
    # Their original sample time is the only safe migration fallback.
    if payload.get("reset_expiry_source") == "api":
        value = payload.get("ts")
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return int(value)
    return None


def _valid_list(value: Any, count: int | None, now: int) -> bool:
    return (
        count is not None
        and isinstance(value, list)
        and len(value) == count
        and all(type(x) is int and now < x <= MAX_UNIX_SECONDS for x in value)
        and value == sorted(value)
    )


def _observation(payload: dict, now: int) -> str:
    count = _count(payload.get("reset_credits_available"))
    if count == 0:
        return "zero"  # A confirmed zero clears dates even if details are absent.
    stated = payload.get("reset_expiry_observation")
    if stated in {"revoked", "inconsistent", "invalid"}:
        return stated  # An explicit negative quality signal cannot be upgraded.
    raw = payload.get("reset_expiry_list")
    if _valid_list(raw, count, now):
        return "verified"
    if "reset_expiry_list" not in payload:
        return "missing"
    if raw is None:
        return "null" if stated != "missing" else "missing"
    return "inconsistent" if isinstance(raw, list) else "invalid"


def reconcile(previous: dict | None, incoming: dict, *, now: int | None = None) -> dict:
    """Merge one authenticated sample without inventing an expiry date.

    Only a missing/null detail field can borrow a previous *verified* list,
    and only for the same account and count within a short fixed grace period.
    The original verification timestamp is never advanced by that fallback.
    """
    now = int(time.time()) if now is None else int(now)
    result = dict(incoming)
    result.pop("reset_expiry_verified_at", None)  # server-owned
    result.pop("reset_expiry_status", None)
    observation = _observation(result, now)
    result["reset_expiry_observation"] = observation
    count = _count(result.get("reset_credits_available"))
    sample_ts = result.get("ts")
    sample_at = int(sample_ts) if type(sample_ts) in (int, float) and math.isfinite(sample_ts) else now

    if observation in {"verified", "zero"}:
        result["reset_expiry_list"] = [] if observation == "zero" else list(result["reset_expiry_list"])
        result["reset_expiry_source"] = "api"
        result["reset_expiry_status"] = observation
        result["reset_expiry_verified_at"] = min(now, sample_at)
        identity = result.get("reset_expiry_identity")
        if observation == "zero" or not isinstance(identity, str) or not _FINGERPRINT.fullmatch(identity):
            result.pop("reset_expiry_identity", None)
        result.pop("reset_expiry_note", None)
        return effective(result, now=now)

    previous = previous or {}
    old_at = _verified_at(previous)
    old_count = _count(previous.get("reset_credits_available"))
    same_account = bool(result.get("account_key")) and result.get("account_key") == previous.get("account_key")
    can_borrow = (
        observation in {"missing", "null"}
        and count is not None and count > 0 and count == old_count
        and same_account
        and old_at is not None and 0 <= now - old_at <= CACHE_GRACE_SECONDS
        and previous.get("reset_expiry_source") in {"api", "api_cache"}
        and _valid_list(previous.get("reset_expiry_list"), count, now)
    )
    if can_borrow:
        result["reset_expiry_list"] = list(previous["reset_expiry_list"])
        result["reset_expiry_source"] = "api_cache"
        result["reset_expiry_status"] = "cached"
        result["reset_expiry_verified_at"] = old_at
        if previous.get("reset_expiry_identity"):
            result["reset_expiry_identity"] = previous["reset_expiry_identity"]
        else:
            result.pop("reset_expiry_identity", None)
        result["reset_expiry_note"] = f"detail_{observation}; bounded_cache"
    else:
        result["reset_expiry_list"] = None
        result["reset_expiry_source"] = None
        result["reset_expiry_status"] = observation
        result.pop("reset_expiry_identity", None)
        result.pop("reset_expiry_note", None)
    return result


def effective(payload: dict, *, now: int | None = None) -> dict:
    """Read-time guard: a persisted list cannot outlive its source or dates."""
    now = int(time.time()) if now is None else int(now)
    result = dict(payload)
    count = _count(result.get("reset_credits_available"))
    source = result.get("reset_expiry_source")
    verified_at = _verified_at(result)
    age = now - verified_at if verified_at is not None else None
    max_age = CACHE_GRACE_SECONDS if source == "api_cache" else VERIFIED_MAX_AGE_SECONDS
    if (
        source not in {"api", "api_cache"}
        or age is None or not 0 <= age <= max_age
        or not _valid_list(result.get("reset_expiry_list"), count, now)
    ):
        result["reset_expiry_list"] = None
        result["reset_expiry_source"] = None
        if source in {"api", "api_cache"}:
            result["reset_expiry_status"] = "expired" if age is not None and age > max_age else "invalidated"
        return result
    return result
