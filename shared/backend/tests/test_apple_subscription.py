from core import apple_subscription as s, state_store, desktop_service
from test_automation_schedule import at
import pytest

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'STATE_FILE',tmp_path/'apple.json')
    monkeypatch.setattr(s,'_config',lambda:{'enabled':True})
    desktop_service.gate.resume()

def result(**overrides):
    r=dict(outcome='confirmed_ui_row_read',official_url_count=1,normal_cleanup_complete=True,
           force_termination_used=False,probe_bundle='cc.nuoye.inksight',
           extracted=dict(product='ChatGPT Plus',display_date='11月30日',action_word='续期',renewal_or_expiry_wording='11月30日续期'))
    return dict(r,**overrides)

def claim(now):
    job=s.claim(now=now);assert job;return job

def test_initialize_only_from_formal_native_success():
    t=at('2026-11-04T15:00');job=claim(t)
    assert s.claim(now=t+1) is None
    assert s.complete(job['id'],result(),now=t+12)['ok']
    row=s.status(now=t+13)
    assert row['label']=='续订日 11月30日' and row['next_check_at']==at('2026-11-27T09:00')

def test_test_app_identity_cannot_seed_formal_state():
    t=at('2026-11-04T15:00');job=claim(t)
    assert not s.complete(job['id'],result(probe_bundle='cc.nuoye.inksight.plus-safety-candidate-v5'),now=t+10)['ok']
    assert s._load()['record'] is None

@pytest.mark.parametrize('outcome,attempt,cleanup',[
 ('timeout_45_seconds',1,True),('foreground_changed_preserved_no_cleanup',1,False),
 ('blocked_user_target_app_open',0,False),('skipped_permission_missing_no_prompt',0,False),
 ('authentication_or_transaction_prompt_preserved',1,False)])
def test_failures_and_skips_do_not_overwrite_last_confirmed(outcome,attempt,cleanup):
    t=at('2026-11-04T15:00');job=claim(t);s.complete(job['id'],result(),now=t+10)
    old=s._load()['record']
    assert s.request(manual=True,now=t+20)['accepted']
    job=claim(t+21)
    r=result(outcome=outcome,official_url_count=attempt,normal_cleanup_complete=cleanup)
    assert not s.complete(job['id'],r,now=t+30)['ok']
    current=s._load();assert current['record']==old and current['last_success_at']==t+10
    if 'permission' in outcome or 'authentication' in outcome:
        assert current['blocked']

def test_permission_report_is_final_runtime_state_not_old_grant():
    s.report_permission(False);assert s.claim(now=at('2026-11-04T15:00')) is None
    s.report_permission(True);assert s.claim(now=at('2026-11-04T15:00'))

def test_pause_retains_queued_and_does_not_start_native_job():
    t=at('2026-11-04T15:00');assert s.request(manual=True,now=t)['accepted']
    desktop_service.gate.pause()
    assert s.claim(now=t+1) is None
    assert s._load()['job']['status']=='queued'
    desktop_service.gate.resume()

def test_stale_result_and_crashed_reader_do_not_reopen_immediately():
    t=at('2026-11-04T15:00');job=claim(t)
    assert not s.complete('different',result(),now=t+10)['accepted']
    assert s.claim(now=t+101) is None
    assert s._load()['record'] is None and s._load()['retry_pending']
    assert not s.request(manual=True,now=t+102)['accepted']
