"""Phase-3 isolated policy, semantic-key and calendar acceptance tests."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import activity_signals as sig  # noqa: E402
from core import date_table  # noqa: E402
from core import news_calendar  # noqa: E402


NOW = 1_788_759_358


def _ai():
    return {
        "codex_7d_used": 3.004,
        "codex_resets_at": NOW + 3600,
        "reset_credits_available": 2,
        "reset_expiry_list": [NOW + 9000, NOW + 8000, NOW + 9000],
        "plan": "PLUS",
        "valid_until_date": "2025-01-15",
        "deepseek": {"CNY": 9.034, "USD": 0.0},
        "member": {"plan": "plus", "valid_until_date": "2025-01-15",
                   "renewal_status": "cancelled", "precision": "date",
                   "source": "manual"},
        "fresh": {"codex": NOW - 30, "ds_cny": NOW - 10, "ds_usd": NOW - 20},
        "freshness_policy": {"codex": {"stale_after_s": 1800}},
    }


def test_activity_key_normalizes_noise_and_list_order():
    a = _ai()
    b = copy.deepcopy(a)
    b["codex_7d_used"] = 3.0039
    b["deepseek"]["CNY"] = 9.0301
    b["reset_expiry_list"] = list(reversed(a["reset_expiry_list"]))
    b["fresh"] = {"codex": NOW, "ds_cny": NOW, "ds_usd": NOW}
    b["freshness_policy"] = {"codex": {"stale_after_s": 1}}
    b["status"] = "stale"
    assert sig.activity_key(a) == sig.activity_key(b)
    assert sig.activity_observed_at(a) == NOW - 30


def test_reset_timestamp_second_jitter_is_not_a_new_activity_or_visual_change():
    a = _ai()
    b = copy.deepcopy(a)
    a["codex_resets_at"] = 1_790_487_573
    b["codex_resets_at"] = 1_790_487_574
    ng = {"news": {"mode": "digest", "text": "正文", "generated_at": NOW},
          "gold": {"price_gram_cny": 951.8, "change_gram_cny": -4.4,
                   "change_percent": -0.46, "quote_time": NOW}}
    assert sig.activity_key(a) == sig.activity_key(b)
    assert sig.ai_visual_key(a) == sig.ai_visual_key(b)
    assert sig.news_gold_visual_key(a, ng) == sig.news_gold_visual_key(b, ng)

    b["codex_resets_at"] = a["codex_resets_at"] + 61
    assert sig.activity_key(a) != sig.activity_key(b)


def test_usd_independent_change_is_activity():
    a = _ai()
    b = copy.deepcopy(a)
    b["deepseek"]["USD"] = 0.01
    assert sig.activity_key(a) != sig.activity_key(b)


def test_activity_unknown_is_not_comparable():
    for field in ("codex_7d_used", "codex_resets_at", "reset_expiry_list"):
        a = _ai()
        a[field] = None
        assert sig.activity_snapshot(a) is None
        assert sig.activity_key(a) is None
    a = _ai()
    a["fresh"]["ds_usd"] = 0
    assert sig.activity_key(a)
    assert sig.activity_observed_at(a) is None


def test_visual_keys_ignore_freshness_but_track_visible_fields():
    a = _ai()
    ng = {"news": {"mode": "digest", "text": "正文", "generated_at": NOW},
          "gold": {"price_gram_cny": 951.8, "change_gram_cny": -4.4,
                   "change_percent": -0.46, "quote_time": NOW}}
    b = copy.deepcopy(a)
    b["fresh"] = {"codex": NOW + 60, "ds_cny": NOW + 60, "ds_usd": NOW + 60}
    assert sig.ai_visual_key(a) == sig.ai_visual_key(b)
    assert sig.news_gold_visual_key(a, ng) == sig.news_gold_visual_key(b, ng)
    b["deepseek"]["USD"] = 1.0
    assert sig.ai_visual_key(a) != sig.ai_visual_key(b)
    assert sig.news_gold_visual_key(a, ng) != sig.news_gold_visual_key(b, ng)

    c = copy.deepcopy(ng)
    c["news"]["update_state"] = "due"
    assert sig.news_gold_visual_key(a, ng) != sig.news_gold_visual_key(a, c)


def test_official_workday_and_out_of_range_degradation(tmp_path):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps({
        "holidays": ["2026-10-01"], "workdays": ["2026-10-10"],
        "years": [2026], "source": "isolated official fixture"}), encoding="utf-8")
    old = news_calendar._CAL_FILE
    try:
        news_calendar.configure_calendar_file(cal)
        assert news_calendar.workday_info(dt.date(2026, 10, 1)) == {
            "workday": False, "degraded": False,
            "source": "isolated official fixture", "covered_years": ["2026"]}
        assert news_calendar.workday_info(dt.date(2026, 10, 10))["workday"] is True
        future = news_calendar.workday_info(dt.date(2027, 1, 4))
        assert future["workday"] is True and future["degraded"] is True
    finally:
        news_calendar.configure_calendar_file(old)


def test_date_payload_carries_same_workday_decision(monkeypatch):
    real = Path(news_calendar.__file__).resolve().parent.parent / "data" / "news_calendar.json"
    old = news_calendar._CAL_FILE
    try:
        news_calendar.configure_calendar_file(real)
        monkeypatch.setattr(date_table, "_bj_today", lambda: dt.date(2026, 10, 1))
        table = date_table.build_table_payload(days_past=0, days_future=9)
        by_date = {r["d"]: r for r in table["days"]}
        assert by_date["2026-10-01"]["workday"] is False
        assert by_date["2026-10-10"]["workday"] is True
        assert table["workday_calendar"]["degraded"] is False
    finally:
        news_calendar.configure_calendar_file(old)


def test_transport_version_changes_without_visual_refresh():
    tools = Path(__file__).resolve().parents[2] / "tools"
    sys.path.insert(0, str(tools))
    import cloud_publish

    base = {
        "screen": {
            "modules": [{"widgets": [{"type": "text", "text": "same"}]}],
            "versions": {"ai_visual_key": "a", "news_gold_visual_key": "n",
                         "calendar_key": "c", "activity_observed_at": 100},
            "pages": {"ai": {"fresh": {"codex": 100}}, "news_gold": {}},
            "layout_version": 2, "font_version": "f",
        }
    }
    newer = copy.deepcopy(base)
    newer["screen"]["versions"]["activity_observed_at"] = 160
    newer["screen"]["pages"]["ai"]["fresh"]["codex"] = 160
    a = cloud_publish.combine_dashboard([base])
    b = cloud_publish.combine_dashboard([newer])
    assert cloud_publish.payload_id(a) == cloud_publish.payload_id(b)
    assert cloud_publish.publication_id(a) != cloud_publish.publication_id(b)


def test_cloud_publish_preserves_device_policy_and_display_switches():
    tools = Path(__file__).resolve().parents[2] / "tools"
    sys.path.insert(0, str(tools))
    import cloud_publish

    base = {
        "screen": {
            "modules": [{"widgets": [{"type": "text", "text": "same"}]}],
            "display_preferences": {
                "show_codex_credits": False,
                "show_openai_api_balance": False,
            },
            "device_policy": {
                "news_check": {
                    "enabled": True,
                    "followup_seconds": 300,
                    "poll_seconds": 60,
                    "schedules": [{"hour": 8, "minute": 55, "issue": "morning"}],
                }
            },
        }
    }
    combined = cloud_publish.combine_dashboard([base])
    assert combined["screen"]["display_preferences"] == base["screen"]["display_preferences"]
    assert combined["screen"]["device_policy"] == base["screen"]["device_policy"]

    changed = copy.deepcopy(base)
    changed["screen"]["display_preferences"]["show_codex_credits"] = True
    assert cloud_publish.payload_id(cloud_publish.combine_dashboard([changed])) != \
        cloud_publish.payload_id(combined)


def test_firmware_policy_header_host_compiles(tmp_path):
    root = Path(__file__).resolve().parents[2]
    header_dir = root / "firmware" / "src"
    src = tmp_path / "policy.cpp"
    src.write_text(r'''
#include "phase3_policy.h"
#include <cassert>
int main() {
  assert(p3ModeForMinute(true, 8*60+29) == P3Mode::Night);
  assert(p3ModeForMinute(true, 8*60+30) == P3Mode::Active);
  assert(p3ModeForMinute(true, 18*60+30) == P3Mode::Light);
  assert(p3ModeForMinute(true, 22*60) == P3Mode::Night);
  assert(p3ModeForMinute(false, 8*60) == P3Mode::Light);
  assert(p3ModeForMinute(false, 20*60) == P3Mode::Night);
  assert(p3AdaptiveInterval(P3Mode::Active, true, true, 9999, 0) == 60);
  assert(p3AdaptiveInterval(P3Mode::Light, true, true, 299, 0) == 60);
  assert(p3AdaptiveInterval(P3Mode::Light, true, true, 300, 0) == 180);
  assert(p3AdaptiveInterval(P3Mode::Light, true, true, 900, 0) == 300);
  assert(p3AdaptiveInterval(P3Mode::Night, true, true, 299, 0) == 60);
  assert(p3AdaptiveInterval(P3Mode::Night, true, true, 300, 0) == 300);
  assert(p3AdaptiveInterval(P3Mode::Night, true, true, 900, 0) == 600);
  assert(p3AdaptiveInterval(P3Mode::Night, true, true, 1800, 0) == 900);
  assert(p3AdaptiveInterval(P3Mode::Night, true, true, 3600, 0) == 1800);
  assert(p3AdaptiveInterval(P3Mode::Night, true, false, 9999, 0) == 300);
  assert(p3DesiredPage(P3Mode::Active, 0, true, true, false, 1600, 1000, 1300) == 1);
  // Unknown activity (including first boot) cannot pin AI indefinitely.
  assert(p3DesiredPage(P3Mode::Active, 0, true, false, false, 1600, 1000, 1300) == 1);
  assert(p3DesiredPage(P3Mode::Active, 1, true, false, false, 1600, 1000, 1300) == 1);
  assert(p3DesiredPage(P3Mode::Active, 1, true, true, true, 1600, 1000, 1300) == 0);
  assert(p3DesiredPage(P3Mode::Light, 0, true, true, false, 1299, 1000, 1200) == 0);
  assert(p3DesiredPage(P3Mode::Light, 0, true, true, false, 1300, 1000, 1200) == 1);
  assert(p3DesiredPage(P3Mode::Night, 0, true, true, false, 9999, 1000, 0) == 0);
  assert(p3DesiredPage(P3Mode::Night, 1, true, true, true, 9999, 1000, 0) == 0);
  assert(p3DesiredPage(P3Mode::Active, 0, false, true, false, 9999, 1000, 0) == 0);
  assert(p3NextWakeSeconds(true, 60, 300, 1000, 0) == 300);
  assert(p3NextWakeSeconds(true, 60, 300, 1000, 50) == 50);
  assert(p3NextWakeSeconds(false, 60, 0, 1000, 500) == 60);
  assert(p3SecondsToModeBoundary(true, true, 8*60+29, 0) == 60);
  assert(p3SecondsToModeBoundary(true, true, 8*60+30, 0) == 10*60*60);
  assert(p3AlignedNext(1000, 1005, 60) == 1060);
  assert(p3AlignedNext(1000, 1060, 60) == 1120);
  assert(p3AlignedNext(1000, 1181, 60) == 1240);
  return 0;
}
''', encoding="utf-8")
    exe = tmp_path / "policy"
    subprocess.run(["c++", "-std=c++11", "-I", str(header_dir), str(src), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_firmware_has_no_minute_clock_or_empty_retry_wake():
    source = (Path(__file__).resolve().parents[2] / "firmware" / "src" / "v4_mode.cpp").read_text()
    assert "_drawClockMinute" not in source
    assert "min(ACTIVE_POLL_SECONDS, retryWait)" not in source
    assert "p3NextWakeSeconds" in source
    assert "structuredLoadCachedDocument" in source


def test_timezone_is_restored_on_every_deep_sleep_boot():
    source = (Path(__file__).resolve().parents[2] / "firmware" / "src" / "main.cpp").read_text()
    assert 'setenv("TZ", "CST-8", 1)' in source
    # main.cpp has separate voice-only and display setup functions; both must
    # restore timezone because either image can wake from deep sleep.
    assert source.count("configureRuntimeTimezone();") == 2
