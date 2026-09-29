from __future__ import annotations

from core import codex_collector_health as health


def test_health_failure_backoff_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "STATE_FILE", tmp_path / "health.json")
    assert health.snapshot(now=1000)["status"] == "not_started"
    first = health.record(False, "cli_missing", now=1000)
    assert first["next_retry_at"] == 1060
    assert not health.should_attempt(now=1059)
    assert health.should_attempt(now=1060)
    second = health.record(False, "missing_7d", now=1060)
    assert second["next_retry_at"] == 1180
    assert health.snapshot(now=1060)["status"] == "stale"
    health.record(True, now=1180)
    assert health.snapshot(now=1180)["status"] == "current"
    assert health.should_attempt(now=1180)
    health.record(False, "rpc_error", now=1240)
    assert health.snapshot(now=1240)["status"] == "retrying"
    assert health.snapshot(now=8381)["status"] == "stale"
    assert health.retry_seconds(100) == 600


def test_health_does_not_persist_arbitrary_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "STATE_FILE", tmp_path / "health.json")
    state = health.record(False, "secret-bearing error", now=100)
    assert state["reason"] == "unexpected"
    assert "secret-bearing error" not in (tmp_path / "health.json").read_text()


def test_corrupt_health_is_not_mislabeled_as_not_started(tmp_path, monkeypatch):
    target = tmp_path / "health.json"
    target.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(health, "STATE_FILE", target)
    assert health.snapshot(now=100)["status"] == "unavailable"
