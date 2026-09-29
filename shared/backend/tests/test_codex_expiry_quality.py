"""Banked reset dates must remain truthful across partial collector samples."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import time

import pytest

from core.codex_expiry import CACHE_GRACE_SECONDS, effective, reconcile
from core.codex_usage_store import get_codex_usage, set_codex_usage
from core.db import close_all


NOW = 1_800_000_000
ACCOUNT = "a" * 20


def sample(**extra):
    value = {
        "account_key": ACCOUNT,
        "ts": NOW,
        "reset_credits_available": 2,
        "reset_expiry_list": [NOW + 3600, NOW + 7200],
        "reset_expiry_observation": "verified",
        "windows": [{"duration_minutes": 10080, "used_percent": 25}],
    }
    value.update(extra)
    return value


def test_missing_and_null_borrow_only_within_fixed_grace():
    first = reconcile(None, sample(), now=NOW)
    assert first["reset_expiry_status"] == "verified"
    missing = sample(ts=NOW + 10)
    del missing["reset_expiry_list"]
    cached = reconcile(first, missing, now=NOW + 10)
    assert cached["reset_expiry_list"] == first["reset_expiry_list"]
    assert cached["reset_expiry_status"] == "cached"
    assert cached["reset_expiry_verified_at"] == NOW
    null = reconcile(cached, sample(ts=NOW + 20, reset_expiry_list=None), now=NOW + 20)
    assert null["reset_expiry_observation"] == "null"
    assert null["reset_expiry_verified_at"] == NOW
    assert effective(null, now=NOW + CACHE_GRACE_SECONDS + 1)["reset_expiry_list"] is None


@pytest.mark.parametrize("change", [
    {"reset_credits_available": 0, "reset_expiry_list": None},
    {"reset_credits_available": 1, "reset_expiry_list": None},
    {"account_key": "b" * 20, "reset_expiry_list": None},
    {"reset_expiry_list": [NOW + 3600], "reset_expiry_observation": "inconsistent"},
    {"reset_expiry_list": None, "reset_expiry_observation": "revoked"},
])
def test_changes_never_borrow_old_dates(change):
    first = reconcile(None, sample(), now=NOW)
    changed = reconcile(first, sample(ts=NOW + 1, **change), now=NOW + 1)
    assert changed["reset_expiry_source"] != "api_cache"
    assert changed["reset_expiry_list"] == ([] if change.get("reset_credits_available") == 0 else None)


def test_verified_duplicate_dates_are_distinct_opportunities():
    dates = [NOW + 3600, NOW + 3600]
    item = reconcile(None, sample(reset_expiry_list=dates), now=NOW)
    assert item["reset_expiry_list"] == dates
    assert effective(item, now=NOW + 3601)["reset_expiry_list"] is None


def test_bogus_dates_never_appear():
    for value in ([NOW + 3600, True], [NOW + 3600, 4_102_444_801], [NOW + 3600]):
        item = reconcile(None, sample(reset_expiry_list=value), now=NOW)
        assert item["reset_expiry_list"] is None


def _probe():
    path = Path(__file__).resolve().parents[2] / "tools" / "codex_quota_probe.py"
    spec = importlib.util.spec_from_file_location("codex_probe_expiry_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_classifies_absent_null_zero_complete_and_partial(monkeypatch):
    probe = _probe()
    monkeypatch.setattr(probe.time, "time", lambda: NOW)
    base = {"rateLimits": {"secondary": {"windowDurationMins": 10080, "usedPercent": 25}}}
    def read(reset):
        return probe.normalize({**base, "rateLimitResetCredits": reset})
    assert read({"availableCount": 2})["reset_expiry_observation"] == "missing"
    assert read({"availableCount": 2, "credits": None})["reset_expiry_observation"] == "null"
    zero = read({"availableCount": 0})
    assert zero["reset_expiry_list"] == [] and zero["reset_expiry_observation"] == "zero"
    complete = read({"availableCount": 2, "credits": [
        {"id": "one", "status": "available", "expiresAt": NOW + 3600},
        {"id": "two", "status": "available", "expiresAt": NOW + 3600},
    ]})
    assert complete["reset_expiry_list"] == [NOW + 3600, NOW + 3600]
    assert len(complete["reset_expiry_identity"]) == 32
    partial = read({"availableCount": 2, "credits": [
        {"status": "available", "expiresAt": NOW + 3600},
    ]})
    assert partial["reset_expiry_list"] is None
    assert partial["reset_expiry_observation"] == "inconsistent"


@pytest.mark.asyncio
async def test_store_transaction_preserves_then_clears_and_rejects_old_sample():
    now = int(time.time())
    mac = "EXPIRY-QUALITY-TEST"
    first = sample(ts=now, reset_expiry_list=[now + 3600, now + 7200])
    await set_codex_usage(mac, "mac", first)
    missing = sample(ts=now + 1, reset_expiry_list=None)
    await set_codex_usage(mac, "mac", missing)
    current = await get_codex_usage(mac)
    assert current["reset_expiry_source"] == "api_cache"
    assert current["reset_expiry_verified_at"] == now
    await set_codex_usage(mac, "mac", sample(ts=now - 1, reset_credits_available=0))
    assert (await get_codex_usage(mac))["reset_credits_available"] == 2
    await set_codex_usage(mac, "mac", sample(ts=now + 2, reset_credits_available=0,
                                              reset_expiry_list=None))
    current = await get_codex_usage(mac)
    assert current["reset_expiry_list"] == []
    assert current["reset_expiry_status"] == "zero"
    await close_all()
