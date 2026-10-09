"""Offline contract tests for one XAUS request budget and durable wake retries."""
from __future__ import annotations

import importlib.util
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import data_cache as dc
from core import gold_baseline as baseline
from core import gold_feed as gold
from core import host_recovery

BJ = timezone(timedelta(hours=8))
ZERO = int(datetime(2026, 9, 29, 0, tzinfo=BJ).timestamp())


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def payload(stamp, *, updated=None, state="fresh"):
    return {"xau": {"price": 950.0, "currency": "CNY", "unit": "gram"},
            "spot_usd_oz": 4400.0, "fx_rate": 6.72, "source": "xaus.com",
            "price_as_of": iso(stamp), "updated_at": iso(updated or stamp),
            "data_state": {"status": state, "as_of": iso(updated or stamp),
                           "source": "upstream"}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    previous = (gold.STATE_FILE, baseline.STATE_FILE, dc.DEFAULT_CACHE_FILE,
                host_recovery.STATE_FILE)
    gold.configure_state_file(tmp_path / "gold_state.json")
    baseline.configure_state_file(tmp_path / "baselines.json")
    dc.configure_cache_file(tmp_path / "cache.json")
    host_recovery.configure_state_file(tmp_path / "host.json")
    monkeypatch.setattr(gold, "_freshness_limits", lambda: (300, 7200, 2700))
    yield
    gold.configure_state_file(previous[0])
    baseline.configure_state_file(previous[1])
    dc.configure_cache_file(previous[2])
    host_recovery.configure_state_file(previous[3])


def test_host_wake_bypasses_completed_slot_and_disabled_night(monkeypatch):
    t = ZERO + 30 * 60
    calls = []
    monkeypatch.setattr(gold, "_mode_enabled", lambda _at=None: False)
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (calls.append(1) or 200, payload(t)))
    state = gold._default_state()
    state["scheduled_slots"][gold._scheduled_slot(datetime.fromtimestamp(t, BJ))] = {
        "at": t, "status": "done"}
    gold._save_state(state)
    assert gold.refresh(now=t, reason="scheduled")["disabled"]
    wake = gold.refresh(now=t, force=True, reason="host-wake", request_id="host-wake-1")
    assert wake["ok"] and len(calls) == 1


def test_failed_fetch_is_pending_and_deduped_without_immediate_retry(monkeypatch):
    t = ZERO + 3600
    calls = []
    def request(timeout_sec=12):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("offline")
        return 200, payload(t + 60)
    monkeypatch.setattr(gold, "_request", request)
    first = gold.refresh(force=True, reason="device_wake", request_id="wake-first", now=t)
    assert first["pending"] and first["attempted"] and len(calls) == 1
    second = gold.refresh(force=True, reason="device_wake", request_id="wake-first", now=t + 1)
    assert second["pending"] and not second["attempted"] and len(calls) == 1
    assert gold.retry_pending(now=t + 29)["action"] == "none"
    third = gold.retry_pending(now=t + 60)
    assert third["ok"] and gold.request_state("wake-first") == "complete"
    assert len(calls) == 2


def test_failure_does_not_coalesce_a_distinct_wake_as_success(monkeypatch):
    t = ZERO + 3600
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (_ for _ in ()).throw(
        urllib.error.URLError("offline")))
    gold.refresh(force=True, reason="host-wake", request_id="wake-first", now=t)
    second = gold.refresh(force=True, reason="device_wake", request_id="wake-second", now=t + 1)
    assert second["pending"] and not second["ok"]
    assert gold.request_state("wake-second") == "pending"


def test_retry_after_is_shared_and_prevents_second_network_call(monkeypatch):
    t = ZERO + 3600
    calls = []
    def request(timeout_sec=12):
        calls.append(1)
        raise urllib.error.HTTPError(gold.ENDPOINT, 429, "limited",
                                     {"Retry-After": "120"}, None)
    monkeypatch.setattr(gold, "_request", request)
    first = gold.refresh(force=True, request_id="rate-first", now=t)
    assert first["pending"] and first["retry_after_s"] >= 120
    second = gold.refresh(force=True, request_id="rate-second", now=t + 30)
    assert second["pending"] and len(calls) == 1
    assert gold.retry_pending(now=t + 119)["action"] == "none"


def test_old_fresh_response_and_cache_age_never_look_current(monkeypatch):
    t = ZERO + 3600
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (200, payload(t - 900)))
    old = gold.refresh(force=True, request_id="aged-response", now=t)
    assert old["pending"] and old["accepted"]
    assert old["item"]["freshness_reason"] == "response_aged"
    assert old["item"]["updated_at"] == t - 900
    assert old["item"]["fetched_at"] == t
    assert old["item"]["price_as_of"] == t - 900
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (200, payload(t + 61)))
    assert gold.retry_pending(now=t + 61)["ok"]
    assert gold.cached(now=t + 61 + 2701)["freshness_reason"] in {"cache_aged", "quote_aged"}
    assert gold.effective_cache_status(now=t + 61 + 2701) == "stale"


def test_clock_rollback_does_not_make_extra_request(monkeypatch):
    t = ZERO + 3600
    calls = []
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (calls.append(1) or 200, payload(t)))
    assert gold.refresh(force=True, request_id="first-time", now=t)["ok"]
    rolled = gold.refresh(force=True, request_id="rolled-time", now=t - 60)
    assert rolled["pending"] and rolled["error_class"] == "clock_rollback"
    assert len(calls) == 1


def test_intraday_recovery_uses_same_budget_after_spot(monkeypatch):
    t = ZERO + 3600
    calls = []
    monkeypatch.setattr(gold, "_request", lambda timeout_sec=12: (calls.append("spot") or 200, payload(t)))
    monkeypatch.setattr(baseline, "recovery_due", lambda now=None: True)
    monkeypatch.setattr(baseline, "maybe_recover_usd", lambda now=None: (calls.append("intraday") or {
        "attempted": True, "status": "no_point_within_tolerance"}))
    assert gold.refresh(force=True, request_id="spot", now=t)["ok"]
    assert gold.recover_baseline_if_due(now=t + 1)["reason"] == "minimum_interval"
    assert calls == ["spot"]
    assert gold.recover_baseline_if_due(now=t + 59)["reason"] == "minimum_interval"
    assert gold.recover_baseline_if_due(now=t + 60)["attempted"]
    assert calls == ["spot", "intraday"]


def test_weekend_stale_is_not_labeled_market_closed():
    saturday = int(datetime(2026, 10, 3, 12, tzinfo=BJ).timestamp())
    item = gold._parse(payload(saturday - 900), saturday)
    assert item["stale"] and item["market_state"] == "unknown"


def test_queue_retains_pending_and_deletes_only_completed(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "tools" / "gold_requests.py"
    spec = importlib.util.spec_from_file_location("gold_requests_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"backend": "http://127.0.0.1:8080",
                                  "webdav": "https://example.invalid/dav",
                                  "devices": [{"mac": "AABBCCDDEEFF"}]}))
    monkeypatch.setattr(module, "_admin_token", lambda _path: "dummy")
    monkeypatch.setattr(module, "_ensure_req_dir", lambda *_args: True)
    monkeypatch.setattr(module, "_webdav_get", lambda *_args: (
        200, json.dumps({"type": "gold_refresh", "request_id": "wake-12345678"}).encode(), "tag"))
    deleted = []
    monkeypatch.setattr(module, "_webdav_delete", lambda *_args: deleted.append(True) or True)
    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps(self.value).encode()
    replies = iter([{"ok": False, "pending": True}, {"ok": True, "action": "fetched"}])
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(next(replies)))
    monkeypatch.setattr(module.sys, "argv", ["gold_requests", "--config", str(config)])
    assert module.main() == 1 and not deleted
    assert module.main() == 0 and len(deleted) == 1
