"""Offline acceptance for conservative remote-source release defaults."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from core import news_brief as brief
from core import news_schedule as schedule
from core import operator_config

ROOT = Path(__file__).resolve().parents[3]


def _enabled_config(source_id: str = "openai") -> dict:
    cfg = copy.deepcopy(operator_config.DEFAULT_CONFIG["news_digest"])
    cfg["sources"] = [source_id]
    cfg["source_enabled"][source_id] = True
    return cfg


def _isolated_state(tmp_path, monkeypatch) -> None:
    schedule.configure_state_file(tmp_path / "schedule.json")
    brief.configure_state_file(tmp_path / "brief.json")
    monkeypatch.setattr(brief, "_CORPUS_FILE", tmp_path / "corpus.json")
    monkeypatch.setattr(schedule, "AUTHORIZED_FILE", tmp_path / "authorized.json")


def test_new_install_has_no_remote_sources_enabled():
    news = operator_config.DEFAULT_CONFIG["news_digest"]
    assert news["sources"] == []
    assert news["source_enabled"] == {source_id: False for source_id in brief.SOURCE_IDS}


def test_legacy_explicit_sources_are_preserved_in_memory():
    upgraded = operator_config._upgrade_public_document({
        "schema_version": 2,
        "news_digest": {"sources": ["ithome", "verge"]},
    })
    assert upgraded["news_digest"]["source_enabled"] == {
        source_id: source_id in {"ithome", "verge"} for source_id in brief.SOURCE_IDS
    }


def test_explicit_switches_are_not_overridden_by_legacy_sources():
    raw = {"schema_version": 2, "news_digest": {
        "sources": ["verge"],
        "source_enabled": {source_id: source_id == "openai" for source_id in brief.SOURCE_IDS},
    }}
    assert operator_config._upgrade_public_document(raw)["news_digest"]["source_enabled"]["openai"]
    assert not operator_config._upgrade_public_document(raw)["news_digest"]["source_enabled"]["verge"]


def test_disabled_collection_performs_zero_network_requests(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    monkeypatch.setattr(brief, "_fetch_feed", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network should not be reached")))
    result = brief.collect_all([])
    assert all(row.get("disabled") is True for row in result.values())


def test_old_cache_cannot_leak_from_disabled_source(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    now = 2_000_000_000
    corpus = {"seen": {
        "allowed": {"source": "openai", "article_id": "allowed", "title": "Allowed",
                    "url": "https://example.test/a", "published": now - 30, "collected": now - 20},
        "blocked": {"source": "verge", "article_id": "blocked", "title": "Blocked",
                    "url": "https://example.test/b", "published": now - 30, "collected": now - 20},
    }}
    (tmp_path / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    rows = brief.build_candidates(now=now, source_ids=["openai"])
    assert [row["source"] for row in rows] == ["openai"]


def test_all_sources_disabled_publishes_local_message_with_zero_calls(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    cfg = copy.deepcopy(operator_config.DEFAULT_CONFIG["news_digest"])
    monkeypatch.setattr(schedule, "credential_state", lambda: {"state": "active"})
    monkeypatch.setattr(schedule, "_collect_if_due", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("collection should not run")))
    monkeypatch.setattr(schedule, "_generate", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("model should not run")))
    outcome = schedule._run_one(schedule._load(), None, "manual",
                                datetime(2026, 9, 14, 9, 0),
                                datetime(2026, 9, 14, 9, 0), cfg=cfg)
    current = brief._load_state()["current"]
    assert outcome == "published:daily-message"
    assert current["origin"] == "local-daily-message"
    assert "未启用远程新闻源" in current["note"]


def test_disabled_sources_take_precedence_over_unready_credential(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    cfg = copy.deepcopy(operator_config.DEFAULT_CONFIG["news_digest"])
    monkeypatch.setattr(schedule, "credential_state", lambda: {"state": "pending_validation"})
    outcome = schedule._run_one(schedule._load(), None, "manual",
                                datetime(2026, 9, 14, 9, 0),
                                datetime(2026, 9, 14, 9, 0), cfg=cfg)
    assert outcome == "published:daily-message"
    assert "未启用远程新闻源" in brief._load_state()["current"]["note"]


def test_missing_key_skips_collection_and_model(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    cfg = _enabled_config()
    monkeypatch.setattr(schedule, "credential_state", lambda: {"state": "missing"})
    monkeypatch.setattr(schedule, "_collect_if_due", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("collection should not run")))
    monkeypatch.setattr(schedule, "_generate", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("model should not run")))
    outcome = schedule._run_one(schedule._load(), None, "manual",
                                datetime(2026, 9, 14, 9, 0),
                                datetime(2026, 9, 14, 9, 0), cfg=cfg)
    assert outcome == "published:daily-message"


def test_supplied_candidates_are_filtered_before_prompt(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch)
    cfg = _enabled_config("openai")
    monkeypatch.setattr(schedule, "credential_state", lambda: {"state": "active"})
    monkeypatch.setattr(schedule, "_collect_if_due", lambda *_a, **_k: None)
    seen = []
    monkeypatch.setattr(schedule, "_generate", lambda rows, *_a, **_k: (
        seen.extend(rows) or {"ok": True, "lines": ["来源过滤测试内容已通过。"],
                              "events": [], "origin": "test"}, None))
    candidates = [
        {"source": "verge", "article_id": "blocked"},
        {"source": "openai", "article_id": "allowed"},
    ]
    outcome = schedule._run_one(schedule._load(), candidates, "manual",
                                datetime(2026, 9, 14, 9, 0),
                                datetime(2026, 9, 14, 9, 0), cfg=cfg)
    assert outcome == "published"
    assert [row["article_id"] for row in seen] == ["allowed"]


def test_manager_exposes_source_switches_and_transparency_metadata():
    html = (ROOT / "shared/backend/static/manager/index.html").read_text(encoding="utf-8")
    script = (ROOT / "shared/backend/static/manager/manager.js").read_text(encoding="utf-8")
    route = (ROOT / "shared/backend/api/routes/local_console.py").read_text(encoding="utf-8")
    for source_id in brief.SOURCE_IDS:
        assert f'data-news-source="{source_id}"' in html
    assert "AI 摘要" in script and "safeUrl" in script
    assert '"source", "url", "title", "published_at", "event_at"' in route
