"""Offline USD/FX v2 baseline, precision, compatibility and persistence tests."""
import copy
import importlib.util
import json
import math
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import data_cache as dc
from core import gold_baseline as b
from core import gold_feed as g
from core import state_store

BJ = timezone(timedelta(hours=8))
DAY = "2026-10-01"
ZERO = int(datetime(2026, 10, 1, tzinfo=BJ).timestamp())
NOW = ZERO + 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def spot(t=NOW, p=4200.123456, r=6.731234, **extra):
    return {"xau": {"price": 1.23, "currency": "CNY", "unit": "gram"},
            "spot_usd_oz": p, "fx_rate": r, "source": "xaus.com",
            "updated_at": iso(t), "price_as_of": iso(t),
            "data_state": {"status": "fresh", "source": "upstream", "as_of": iso(t)}, **extra}


def history(points=None, now=NOW):
    points = points if points is not None else [
        {"t": ZERO - 60, "p": 4190}, {"t": ZERO + 120, "p": 4195.123456},
        {"t": now - 60, "p": 4200}]
    return {"symbol": "xau", "currency": "USD", "unit": "troy_oz",
            "hours": 48, "source": "xaus-sampler (gold-api.com)", "interval_seconds": 120,
            "points": points, "count": len(points),
            "coverage_seconds": max(x["t"] for x in points) - min(x["t"] for x in points),
            "data_state": {"status": "fresh", "source": "sampler", "as_of": iso(max(x["t"] for x in points))}}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    old = (g.STATE_FILE, b.STATE_FILE, dc.DEFAULT_CACHE_FILE)
    g.configure_state_file(tmp_path / "gold.json")
    b.configure_state_file(tmp_path / "baseline.json")
    dc.configure_cache_file(tmp_path / "cache.json")
    monkeypatch.setattr(g, "_freshness_limits", lambda: (300, 7200, 2700))
    monkeypatch.setattr(b, "_config", lambda: (120, 1800, 2))
    yield
    g.configure_state_file(old[0]); b.configure_state_file(old[1]); dc.configure_cache_file(old[2])


def seed(points=None, now=NOW):
    return b.maybe_recover_usd(now=now, requester=lambda: (200, history(points, now=now)))


def test_formula_full_precision_ignores_reported_cny():
    assert seed()["status"] == "recovered"
    v = b.attach_changes(g._parse(spot(), NOW), now=NOW)
    p, r, baseline = 4200.123456, 6.731234, 4195.123456
    assert v["price_gram_cny"] == p * r / 31.1034768
    assert v["change_usd_oz_since_reference"] == p - baseline
    assert v["change_cny_g_usd_reference"] == (p - baseline) * r / 31.1034768
    assert v["reported_price_gram_cny"] == 1.23
    assert v["fx_as_of"] is None
    assert v["baseline_reference_kind"] == "first_available"


def test_prior_midnight_point_excluded_and_exact_midnight_preferred():
    points = [{"t": ZERO - 1, "p": 1}, {"t": ZERO, "p": 2}, {"t": NOW, "p": 3}]
    v = b.parse_intraday(history(points), now=NOW, target_day=DAY)
    assert v["usd"]["value"] == 2 and v["reference_kind"] == "midnight"
    assert v["price_as_of"] == ZERO


def test_late_first_available_partial_coverage_is_honest():
    points = [{"t": ZERO + 1800, "p": 10}, {"t": NOW, "p": 11}]
    v = b.parse_intraday(history(points), now=NOW, target_day=DAY)
    assert v["price_as_of"] == ZERO + 1800
    assert v["history_quality"]["covers_midnight"] is False
    assert v["reference_kind"] == "first_available"


def test_unsorted_identical_duplicates_deduped_conflicts_rejected():
    h = history()
    h["points"] = [h["points"][2], h["points"][1], h["points"][0], h["points"][1]]
    h["count"] = 4
    assert b.parse_intraday(h, now=NOW, target_day=DAY)["price_as_of"] == ZERO + 120
    h["points"][-1] = {"t": ZERO + 120, "p": 999}
    with pytest.raises(ValueError, match="conflicting"):
        b.parse_intraday(h, now=NOW, target_day=DAY)


@pytest.mark.parametrize("path,value", [
    (("currency",), "CNY"), (("unit",), "gram"), (("symbol",), "xag"),
    (("source",), "GC=F"), (("hours",), 24), (("count",), 0),
    (("coverage_seconds",), 1), (("interval_seconds",), True),
    (("data_state", "status"), "stale"), (("data_state", "as_of"), iso(NOW + 1)),
    (("data_state", "age_seconds"), 900), (("updated_at",), iso(NOW - 900)),
])
def test_bad_metadata_not_accepted(path, value):
    h = history()
    target = h if len(path) == 1 else h[path[0]]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        b.parse_intraday(h, now=NOW, target_day=DAY)


def test_real_intraday_contract_needs_no_updated_at():
    h = history()
    assert "updated_at" not in h
    assert b.parse_intraday(h, now=NOW, target_day=DAY)["history_quality"]["response_updated_at"] is None
    with pytest.raises(ValueError):
        b.parse_intraday(h, now=NOW + 900, target_day=DAY)


def test_future_points_rejected_even_one_second():
    h = history()
    h["points"][-1]["t"] = NOW + 1
    with pytest.raises(ValueError, match="future"):
        b.parse_intraday(h, now=NOW, target_day=DAY)


def test_invalid_price_skipped_not_false_zero():
    h = history()
    h["points"][1]["p"] = float("nan")
    row = b.parse_intraday(h, now=NOW, target_day=DAY)
    assert row["price_as_of"] == NOW - 60
    assert row["history_quality"]["invalid_price_count"] == 1


def test_no_same_day_or_weekend_does_not_invent_baseline():
    t = ZERO + 60
    h = history([{"t": ZERO - 180, "p": 1}, {"t": ZERO - 60, "p": 2}], now=t)
    with pytest.raises(LookupError):
        b.parse_intraday(h, now=t, target_day=DAY)
    v = b.attach_changes(g._parse(spot(t=t), t), now=t)
    assert v["price_gram_cny"] is not None
    assert v["change_usd_oz_since_reference"] is None


def test_reference_fixed_on_restart_fx_changes_and_next_day_empty():
    assert seed()["status"] == "recovered"
    first = b.status(now=NOW)["baseline"]
    assert b.maybe_recover_usd(now=NOW + 1801, requester=lambda: pytest.fail("no repeat"))["reason"] == "usd_baseline_exists"
    for r in (6.5, 7.0):
        v = b.attach_changes(g._parse(spot(r=r), NOW), now=NOW)
        assert v["baseline_usd_oz"] == first["usd"]["value"]
        assert v["change_cny_g_usd_reference"] == v["change_usd_oz_since_reference"] * r / 31.1034768
    assert b.status(now=NOW + 86400)["baseline"] is None


def test_exact_spot_only_even_if_fx_unavailable():
    prior = g._parse(spot(t=ZERO - 1), ZERO)
    assert not b.consider_spot(prior, now=ZERO)
    later = g._parse(spot(t=ZERO + 1), ZERO + 1)
    assert not b.consider_spot(later, now=ZERO + 1)
    exact = g._parse(spot(t=ZERO, r=None), ZERO)
    assert b.consider_spot(exact, now=ZERO)
    assert b.status(now=ZERO)["baseline"]["price_as_of"] == ZERO


def test_legacy_schema_not_converted_and_budget_preserved():
    state_store.write_json(b.STATE_FILE, {"schema": 1, "records": {DAY: {"cny": {"value": 900}}},
        "recoveries": {DAY: {"attempts": 2, "last_attempt_at": NOW}}})
    assert b.status(now=NOW)["baseline"] is None
    assert not b.recovery_due(now=NOW + 2000)


def test_corrupt_record_recovered_but_corrupt_file_does_not_reset_budget():
    state_store.write_json(b.STATE_FILE, {"schema": 2, "timezone": "Asia/Shanghai", "records": {DAY: {"bad": 1}}})
    assert seed()["status"] == "recovered"
    b.STATE_FILE.write_text("{")
    assert not b.recovery_due(now=NOW + 1900)
    assert b.status(now=NOW)["state_error"]


def test_recovery_cooldown_limit_and_429_durable():
    def fail():
        raise urllib.error.HTTPError(b.INTRADAY_ENDPOINT, 429, "limited", {"Retry-After": "4000"}, None)
    assert b.maybe_recover_usd(now=NOW, requester=fail)["retry_after_s"] == 4000
    assert not b.recovery_due(now=NOW + 2000)
    assert b.maybe_recover_usd(now=NOW + 4000, requester=fail)["attempted"]
    assert not b.recovery_due(now=NOW + 9000)


@pytest.mark.parametrize("fx", [None, 0, -1, float("nan"), float("inf"), True])
def test_invalid_fx_keeps_usd_independent(fx):
    v = g._parse(spot(r=fx), NOW)
    assert v["usd_status"] == "fresh" and v["price_gram_cny"] is None
    assert v["cny_status"] == "unavailable"
    assert g.visual_seed(v)["price_gram_cny"] is None


def test_degraded_fx_retains_complete_snapshot_not_new_price_times_old_fx(monkeypatch):
    seed()
    replies = iter([spot(), spot(t=NOW + 61, p=4300, r=7.5, fx_stale=True)])
    monkeypatch.setattr(g, "_request", lambda: (200, next(replies)))
    first = g.refresh(force=True, request_id="one", now=NOW)["item"]
    second = g.refresh(force=True, request_id="two", now=NOW + 61)["item"]
    assert second["spot_usd_oz"] == 4300
    assert second["usd_status"] == "fresh" and second["cny_status"] == "stale"
    for key in ("price_gram_cny", "cny_spot_usd_oz", "cny_fx_rate", "cny_price_as_of", "cny_fetched_at"):
        assert second[key] == first[key]
    assert second["change_cny_g_usd_reference"] == first["change_cny_g_usd_reference"]
    assert second["price_gram_cny"] != 4300 * first["fx_rate"] / 31.1034768


def test_raw_tiny_delta_preserved_until_formatting():
    seed([{ "t": ZERO, "p": 4200}, {"t": NOW, "p": 4200}])
    v = b.attach_changes(g._parse(spot(p=4200.004, r=100), NOW), now=NOW)
    assert v["change_usd_oz_since_reference"] == pytest.approx(0.004)
    assert v["change_cny_g_usd_reference"] > 0.01


def test_old_firmware_cannot_label_new_delta_first_quote():
    seed()
    v = b.attach_changes(g._parse(spot(), NOW), now=NOW)
    assert v["change_cny_g_since_reference"] is None
    assert v["baseline_cny_kind"] is None
    assert v["change_usd_oz_since_bj_midnight"] is None
    assert v["change_cny_g_usd_reference"] is not None


def test_all_request_entrypoints_enforce_60_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(g, "_request", lambda: (calls.append(1) or 200, spot()))
    assert g.refresh(force=True, reason="host-wake", request_id="first", now=NOW)["ok"]
    g.refresh(force=True, reason="device_wake", request_id="second", now=NOW + 59)
    assert calls == [1]
    assert g._minimum_interval() >= 60


def test_preview_zero_and_missing_delta():
    path = Path(__file__).resolve().parents[2] / "tools/misans_panel_render.py"
    spec = importlib.util.spec_from_file_location("gold_preview_test", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.fmt_signed_delta(-0.004) == "0.00"
    assert m.fmt_signed_delta(0.0) == "0.00"
    assert m.fmt_signed_delta(math.nan) == "—"
    assert m.fmt_signed_delta(-1.239) == "-1.24"
    assert m.fmt_signed_delta(1.239) == "+1.24"
