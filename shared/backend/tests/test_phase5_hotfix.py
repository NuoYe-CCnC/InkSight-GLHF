"""Regression tests for Chinese publication gating and macOS recovery."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import data_cache as dc
from core import gold_baseline
from core import gold_feed
from core import host_recovery
from core import news_brief as brief
from core import news_digest as digest
from core import news_schedule as schedule
from core import news_due_request

BJ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 10, 13, 5, tzinfo=BJ).timestamp()


@pytest.mark.parametrize("text", [
    "OpenAI发布GPT-5 API，并支持iPhone端应用。",
    "苹果发布Apple Vision Pro Developer Kit，面向开发者测试。",
    "量子位：京东推出JoyAI世界模型；OpenAI：Paul Christiano加入基金会董事会。",
])
def test_chinese_gate_allows_brand_model_fragments(text):
    assert digest.validate_digest_text(text)["ok"]


@pytest.mark.parametrize("text,reason", [
    ("Paul Christiano joins OpenAI Foundation Board.", "english-clause"),
    ("OpenAI消息：Paul Christiano joins OpenAI Foundation Board。", "english-clause"),
    ("分析：这是模型的内部分析，不应上屏。", "internal-analysis-leak"),
    ('{"body":"一段中文新闻","events":[]}', "json-or-prompt-leak"),
    ("```json\n一段中文新闻\n```", "json-or-prompt-leak"),
])
def test_chinese_gate_rejects_english_prose_and_internal_leaks(text, reason):
    result = digest.validate_digest_text(text)
    assert not result["ok"] and result["reason"] == reason


def test_local_fallback_never_copies_an_english_source_title():
    rows = [
        {"source": "qbitai", "article_id": "cn1", "title": "国产算力集群发布世界模型",
         "summary": "完成新的模型建设", "digest_category": "模型与研究",
         "event_type": "launch", "published": int(NOW)},
        {"source": "openai", "article_id": "en1",
         "title": "Paul Christiano joins OpenAI Foundation Board",
         "summary": "", "digest_category": "政策与社会影响", "event_type": "research",
         "published": int(NOW) - 10},
        {"source": "ithome", "article_id": "cn2", "title": "新款芯片完成量产验证",
         "summary": "面向推理设备", "digest_category": "芯片与基础设施",
         "event_type": "launch", "published": int(NOW) - 20},
    ]
    result = digest.build_local_fallback(rows)
    assert result and result["ok"]
    assert "joins" not in result["body"] and "量产验证" in result["body"]


def test_final_gate_keeps_previous_issue_and_its_original_date(tmp_path):
    old_brief = brief._STATE_FILE
    brief.configure_state_file(tmp_path / "brief.json")
    previous = {
        "mode": "digest", "period": "evening", "schedule_id": "evening",
        "issue_title": "科技 / AI 晚报", "date": "2026-09-09",
        "generated_at": int(NOW) - 7200, "text": "上一期合格中文简报保持原日期。",
        "events": [], "freshness": "fresh", "origin": "deepseek", "version": "old",
    }
    brief._save_state({"current": copy.deepcopy(previous),
                       "last_valid_digest": copy.deepcopy(previous)})
    try:
        state = {"log": []}
        outcome = schedule._publish(
            state,
            {"ok": True, "lines": ["OpenAI消息：Paul Christiano joins OpenAI Foundation Board。"]},
            "noon", datetime(2026, 9, 10, 12, 55), datetime(2026, 9, 10, 13, 0),
            "科技 / AI 中报",
        )
        current = brief._load_state()["current"]
        assert outcome == "skip:keep-old:final-validation"
        assert current["text"] == previous["text"] and current["date"] == "2026-09-09"
        assert current["freshness"] == "stale" and "未更新正文" in current["note"]
    finally:
        brief.configure_state_file(old_brief)


def test_publish_output_gate_blocks_legacy_bad_state(tmp_path):
    old_brief = brief._STATE_FILE
    brief.configure_state_file(tmp_path / "brief.json")
    brief._save_state({"current": {"mode": "digest", "period": "noon",
                                    "text": "Paul Christiano joins OpenAI Foundation Board.",
                                    "generated_at": int(NOW), "date": "2026-09-10"}})
    try:
        result = brief.fetch_news_for_publish()
        assert result["mode"] == "status" and result["freshness"] == "error"
        assert "joins" not in result["text"]
    finally:
        brief.configure_state_file(old_brief)


def test_host_recovery_is_reserved_before_calls_and_dedupes_short_repeats(
        tmp_path, monkeypatch):
    old_state = host_recovery.STATE_FILE
    host_recovery.configure_state_file(tmp_path / "host_recovery.json")
    calls = {"gold": 0, "news": 0, "feed": 0}

    def fake_gold(**kwargs):
        calls["gold"] += 1
        assert kwargs["force"] is True and kwargs["reason"] in {"service-start", "host-wake"}
        return {"ok": True, "status": "fresh"}

    def fake_news(*_args, **_kwargs):
        calls["news"] += 1
        return {"done": "published", "schedule_id": "noon", "action": "already-current"}

    monkeypatch.setattr(gold_feed, "refresh", fake_gold)
    monkeypatch.setattr(schedule, "current_due_status", lambda *_a, **_k: {
        "due": True, "schedule_id": "noon"})
    monkeypatch.setattr(news_due_request, "check", fake_news)
    monkeypatch.setattr("core.feed_document.build_feed_document",
                        lambda **_kwargs: calls.__setitem__("feed", calls["feed"] + 1))
    try:
        first = host_recovery.recover("service-start", now=NOW)
        duplicate = host_recovery.recover("host-wake", now=NOW + 10)
        later = host_recovery.recover("host-wake", now=NOW + 901)
        assert first["gold"]["action"] == "fetched" and not first["deduped"]
        assert duplicate["deduped"] and later["gold"]["action"] == "fetched"
        assert calls == {"gold": 2, "news": 2, "feed": 2}
        saved = json.loads((tmp_path / "host_recovery.json").read_text())
        assert len(saved["events"]) == 2 and all(row["status"] == "done" for row in saved["events"])
    finally:
        host_recovery.configure_state_file(old_state)


def test_corrupt_recovery_state_fails_closed_without_network(tmp_path, monkeypatch):
    old_state = host_recovery.STATE_FILE
    path = tmp_path / "host_recovery.json"
    path.write_text("not-json", encoding="utf-8")
    host_recovery.configure_state_file(path)
    monkeypatch.setattr(gold_feed, "refresh", lambda **_kwargs: pytest.fail("external call"))
    monkeypatch.setattr(schedule, "tick", lambda *_a, **_k: pytest.fail("news tick"))
    try:
        result = host_recovery.recover("service-start", now=NOW)
        assert not result["ok"] and result["reason"] == "state-unavailable"
    finally:
        host_recovery.configure_state_file(old_state)


def _xaus_payload():
    return {
        "xau": {"price": 952.03, "currency": "CNY", "unit": "gram"},
        "spot_usd_oz": 4401.10, "fx_rate": 6.72,
        "fx_source": "open.er-api.com", "fx_stale": False,
        "updated_at": "2026-09-10T05:05:00.000Z",
        "data_state": {"status": "fresh", "as_of": "2026-09-10T05:05:00.000Z",
                       "source": "upstream"},
        "stale": False, "price_as_of": "2026-09-10T05:05:00.000Z",
        "source": "xaus.com", "price_source": "gold-api.com",
    }


def test_recovery_and_same_moment_cron_make_one_provider_call(tmp_path, monkeypatch):
    old = (host_recovery.STATE_FILE, gold_feed.STATE_FILE, gold_baseline.STATE_FILE,
           dc._file_path)
    host_recovery.configure_state_file(tmp_path / "host.json")
    gold_feed.configure_state_file(tmp_path / "gold.json")
    gold_baseline.configure_state_file(tmp_path / "baseline.json")
    dc.configure_cache_file(tmp_path / "cache.json")
    calls = {"n": 0}

    def request(timeout_sec=12):
        calls["n"] += 1
        return 200, _xaus_payload()

    monkeypatch.setattr(gold_feed, "_request", request)
    monkeypatch.setattr(gold_feed, "_mode_enabled", lambda *_a: True)
    monkeypatch.setattr(gold_feed, "_scheduled_slot", lambda *_a: "2026-09-10T13:00+0800")
    monkeypatch.setattr(schedule, "tick", lambda *_a, **_k: {"latest": None})
    monkeypatch.setattr("core.feed_document.build_feed_document", lambda **_kwargs: {})
    try:
        recovered = host_recovery.recover("host-wake", now=NOW)
        scheduled = gold_feed.refresh(force=False, now=NOW + 1)
        assert recovered["gold"]["action"] == "fetched"
        assert scheduled["ok"] and scheduled["coalesced"] and calls["n"] == 1
    finally:
        host_recovery.configure_state_file(old[0])
        gold_feed.configure_state_file(old[1])
        gold_baseline.configure_state_file(old[2])
        dc.configure_cache_file(old[3])


def test_host_recovery_refetches_completed_half_hour_slot_after_minimum_gap(tmp_path, monkeypatch):
    old = (host_recovery.STATE_FILE, gold_feed.STATE_FILE, gold_baseline.STATE_FILE,
           dc._file_path)
    host_recovery.configure_state_file(tmp_path / "host.json")
    gold_feed.configure_state_file(tmp_path / "gold.json")
    gold_baseline.configure_state_file(tmp_path / "baseline.json")
    dc.configure_cache_file(tmp_path / "cache.json")
    monkeypatch.setattr(gold_feed, "_mode_enabled", lambda *_a: True)
    monkeypatch.setattr(gold_feed, "_scheduled_slot", lambda *_a: "2026-09-10T13:00+0800")
    calls = {"n": 0}
    def request(*_a, **_k):
        calls["n"] += 1
        return 200, _xaus_payload()
    monkeypatch.setattr(gold_feed, "_request", request)
    monkeypatch.setattr(schedule, "current_due_status", lambda *_a, **_k: {"due": False})
    monkeypatch.setattr("core.feed_document.build_feed_document", lambda **_kwargs: {})
    try:
        scheduled = gold_feed.refresh(force=False, reason="scheduled", now=NOW)
        assert scheduled["ok"]
        recovered = host_recovery.recover("host-wake", now=NOW + 60)
        assert recovered["gold"]["action"] == "fetched"
        assert gold_feed._load_state()["stats"]["requests"] == 2
        assert calls["n"] == 2
    finally:
        host_recovery.configure_state_file(old[0])
        gold_feed.configure_state_file(old[1])
        gold_baseline.configure_state_file(old[2])
        dc.configure_cache_file(old[3])
