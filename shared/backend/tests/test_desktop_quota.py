"""Menu snapshots are local, read-only, identity bound, and explicitly 7D."""
import hashlib
import json

import pytest

from core import desktop_quota as quota, desktop_service


@pytest.fixture
def sample(monkeypatch,tmp_path):
    monkeypatch.setattr(quota,"GUARD_FILE",tmp_path/"quota-guard.json")
    group = {"status": "fresh", "last_success": 1000,
             "value": {"duration_minutes": 10080, "used_percent": 64,
                       "account_key": "test-identity", "source": "mac"}}
    health = {"status": "current", "last_success_at": 1000}
    monkeypatch.setattr(quota.data_cache, "get_group", lambda _key: group)
    monkeypatch.setattr(quota.codex_collector_health, "snapshot", lambda **kwargs: health)
    monkeypatch.setattr(quota, "ai_source_policy", lambda: {"codex": {"stale_after_s": 1800}})
    monkeypatch.setattr(quota, "account_key", lambda: "test-identity")
    return group, health


@pytest.mark.parametrize("used,remaining", [(0,100), (100,0), (91,9), (64,36), (63.5,37)])
def test_valid_percentage(sample, used, remaining):
    sample[0]["value"]["used_percent"] = used
    before = json.dumps(sample[0])
    for now in [1000, 1100, 2799]:
        result = quota.snapshot(now=now)
        assert result["remaining_percent"] == remaining
        assert result["last_success_at"] == 1000
        assert result["expires_at"] == 2800
        assert result["reason"] is None
        assert "account_key" not in result
    assert json.dumps(sample[0]) == before  # Polling cannot refresh cache stamps.


@pytest.mark.parametrize("used", [None, True, False, "64", -1, 101, float("nan"), float("inf"), {}, []])
def test_illegal_percentage_never_clamped(sample, used):
    sample[0]["value"]["used_percent"] = used
    assert quota.snapshot(now=1100)["remaining_percent"] is None


@pytest.mark.parametrize("duration", [300, 43200, None, True, "10080", 10080.1, {}, []])
def test_only_exact_seven_day(sample, duration):
    sample[0]["value"]["duration_minutes"] = duration
    assert quota.snapshot(now=1100)["reason"] == "missing_7d"


@pytest.mark.parametrize("stamp", [None, True, "1000", 0, -1, float("nan"), float("inf"), 1200])
def test_invalid_or_future_time(sample, stamp):
    sample[0]["last_success"] = stamp
    assert quota.snapshot(now=1100)["remaining_percent"] is None


def test_expiry_pause_and_recovery(sample):
    desktop_service.gate.pause()
    assert quota.snapshot(now=2799)["remaining_percent"] == 36
    assert quota.snapshot(now=2800)["reason"] == "stale"
    desktop_service.gate.resume()
    assert quota.snapshot(now=2801)["remaining_percent"] is None
    sample[0]["last_success"] = sample[1]["last_success_at"] = 2802
    assert quota.snapshot(now=2802)["remaining_percent"] == 36


def test_identity_switch_and_restore(sample, monkeypatch):
    monkeypatch.setattr(quota, "account_key", lambda: "new-identity")
    assert quota.snapshot(now=1100)["reason"] == "account_mismatch"
    sample[0]["value"]["account_key"] = "new-identity"
    sample[0]["last_success"] = 1101
    assert quota.snapshot(now=1101)["remaining_percent"] == 36
    monkeypatch.setattr(quota, "account_key", lambda: None)
    assert quota.snapshot(now=1100)["remaining_percent"] is None


@pytest.mark.parametrize("status", ["stale", "retrying", "unavailable", "not_started"])
def test_temporary_collector_failure_retains_until_source_boundary(sample, status):
    sample[1]["status"] = status
    assert quota.snapshot(now=1100)["remaining_percent"] == 36
    assert quota.snapshot(now=1100)["retrying"]


def test_cache_and_collector_must_correspond(sample):
    sample[1]["last_success_at"] = 999
    assert quota.snapshot(now=1100)["remaining_percent"] == 36
    sample[1]["last_success_at"] = 1200
    assert quota.snapshot(now=1100)["remaining_percent"] is None
    sample[0]["value"]["source"] = "windows"
    sample[1]["last_success_at"] = 1000
    assert quota.snapshot(now=1100)["reason"] == "wrong_source"
    sample[0]["status"] = "stale"
    assert quota.snapshot(now=1100)["remaining_percent"] is None


def test_empty_cache(sample):
    sample[0].clear()
    assert quota.snapshot(now=1100)["remaining_percent"] is None


def test_local_auth_identity_without_secret_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert quota.account_key() is None
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"account_id": "unit-account", "access_token": "private"}}))
    assert quota.account_key() == hashlib.sha256(b"unit-account").hexdigest()[:20]
    path.write_text("bad-json")
    assert quota.account_key() is None


def test_reset_and_source_expiry_are_hard_boundaries(sample):
    sample[0]['value'].update(resets_at=1500,expires_at=1400)
    assert quota.snapshot(now=1399)['remaining_percent']==36
    assert quota.snapshot(now=1399)['expires_at']==1400
    assert quota.snapshot(now=1400)['remaining_percent'] is None
    sample[0]['value'].pop('expires_at')
    assert quota.snapshot(now=1500)['reason']=='reset_boundary'


def test_no_poll_retry_or_health_write_renews_source_time(sample):
    sample[0]['status']='stale';sample[1].update(status='retrying',reason='rpc_error',last_attempt_at=2600)
    for t in (1100,1500,2000,2799):
        v=quota.snapshot(now=t);assert v['remaining_percent']==36 and v['last_success_at']==1000 and v['expires_at']==2800
    assert quota.snapshot(now=2800)['remaining_percent'] is None


def test_missing_window_invalidates_across_restart_and_old_replay(sample):
    sample[1].update(status='retrying',reason='missing_7d',last_attempt_at=1100)
    assert quota.snapshot(now=1100)['reason']=='missing_7d'
    sample[1].update(status='current',reason=None)
    assert quota.snapshot(now=1101)['remaining_percent'] is None
    sample[0]['last_success']=1102
    assert quota.snapshot(now=1102)['remaining_percent']==36


def test_identity_round_trip_cannot_restore_old_sample(sample,monkeypatch):
    assert quota.snapshot(now=1001)['remaining_percent']==36
    monkeypatch.setattr(quota,'account_key',lambda:'different-account')
    assert quota.snapshot(now=1100)['remaining_percent'] is None
    monkeypatch.setattr(quota,'account_key',lambda:'test-identity')
    assert quota.snapshot(now=1101)['remaining_percent'] is None
    sample[0]['last_success']=1102
    assert quota.snapshot(now=1102)['remaining_percent']==36


def test_transition_diagnostics_do_not_contain_identity_and_only_log_changes(sample):
    quota.snapshot(now=1001);quota.snapshot(now=1002)
    sample[1]['status']='retrying';quota.snapshot(now=1003);quota.snapshot(now=1004)
    sample[1]['status']='current';quota.snapshot(now=1005)
    guard=json.loads(quota.GUARD_FILE.read_text());assert len(guard['transitions'])==3
    assert 'test-identity' not in json.dumps(guard['transitions'])
