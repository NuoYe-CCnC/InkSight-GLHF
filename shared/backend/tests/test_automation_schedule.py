from datetime import datetime
from zoneinfo import ZoneInfo
import copy
import pytest
from core import automation_schedule as a

def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

def record(raw='11月30日', action='续期'):
    return {'product':'ChatGPT Plus','display_date':raw,'action_word':action,'renewal_or_expiry_wording':raw+action}

def seeded(raw='11月30日', observed='2026-11-04T15:00', action='续期'):
    s={};a.accept_apple(s,record(raw,action),at(observed));return s

def test_current_schedule_exact_nodes():
    s=seeded()
    assert a.apple_due(s,at('2026-11-27T08:59'))['due'] is False
    assert a.apple_due(s,at('2026-11-27T09:00'))['due']
    a.accept_apple(s,record(),at('2026-11-27T09:01'))
    assert not a.apple_due(s,at('2026-11-30T15:00'))['due']
    assert a.apple_due(s,at('2026-12-01T09:00'))['due']

def test_manual_inside_window_satisfies_before_not_weekly_timer():
    s=seeded();a.accept_apple(s,record(),at('2026-11-28T16:00'))
    due=a.apple_due(s,at('2026-11-29T16:00'))
    assert not due['due'] and due['next_at']==at('2026-12-01T09:00')

def test_old_value_six_hours_and_72_hours_survive_restart():
    s=seeded();t=at('2026-12-01T09:01');a.accept_apple(s,record(),t);s['last_attempt_at']=t
    assert not a.apple_due(s,t+6*3600-1)['due']
    assert a.apple_due(s,t+6*3600)['due']
    s=copy.deepcopy(s);a.accept_apple(s,record(),t+6*3600);s['last_attempt_at']=t+6*3600
    assert s['stale_since']==t
    due=a.apple_due(s,t+72*3600)
    assert due['reason']=='old_date_weekly_pending' and not due['due']
    assert s['stale_since']==t

def test_changed_date_ends_old_cycle():
    s=seeded();a.accept_apple(s,record(),at('2026-12-01T09:01'))
    a.accept_apple(s,record('12月30日'),at('2026-12-02T09:01'))
    assert 'stale_since' not in s
    assert a.apple_due(s,at('2026-12-02T10:00'))['next_at']==at('2026-12-27T09:00')

def test_cancellation_disables_dense_waiting_and_does_not_infer_expired():
    s=seeded();a.accept_apple(s,record(action='到期'),at('2026-12-01T09:01'))
    assert 'stale_since' not in s and s['record']['action_word']=='到期'
    assert not a.apple_due(s,at('2026-12-03T09:01'))['due']

def test_explicit_expired_stops():
    s=seeded();s['record']['displayed_state']='expired'
    assert not a.apple_due(s,at('2026-12-01T09:00'))['due']

def test_month_day_is_not_official_year_or_silently_rolled():
    s=seeded();assert s['record']['precision']=='month_day'
    assert s['record']['schedule_anchor']['official_year'] is False
    a.accept_apple(s,record(),at('2027-01-01T09:00'))
    assert s['record']['schedule_anchor']['date']=='2026-11-30'
    assert '2026' not in s['record']['display_date']

def test_changed_cross_year_anchor_is_separate_and_explained():
    s=seeded('12月30日','2026-12-01T15:00')
    a.accept_apple(s,record('1月30日'),at('2026-12-31T10:00'))
    assert s['record']['schedule_anchor']['date']=='2027-01-30'
    assert s['record']['display_date']=='1月30日'

def test_english_source_preserves_given_year_precision():
    s=seeded('November 30, 2026')
    assert s['record']['precision']=='year_month_day'
    assert s['record']['schedule_anchor']['official_year']
    assert a.apple_due(s,at('2026-11-04T16:00'))['next_at']==at('2026-11-27T09:00')
    assert a.date_parts('February 30, 2026') is None

def test_ambiguous_old_date_is_pending_not_next_year():
    s=seeded('1月2日','2026-11-04T15:00')
    assert s['record']['schedule_anchor'] is None
    assert a.apple_due(s,at('2026-11-04T16:00'))['reason']=='date_needs_confirmation'
    s=seeded('11月30日','2027-02-01T15:00')
    assert s['record']['schedule_anchor'] is None  # Not silently moved eleven months forward.

def test_missing_before_check_does_not_create_renewal_day_work():
    s=seeded()
    assert not a.apple_due(s,at('2026-11-30T09:00'))['due']

def test_failures_do_not_extend_dense_window_beyond72h():
    s=seeded();t=at('2026-12-01T09:00');a.accept_apple(s,record(),t)
    s.update(last_attempt_at=t+72*3600,retry_pending=True,next_allowed_at=t+78*3600)
    assert not a.apple_due(s,t+79*3600)['due']
    assert a.apple_due(s,t+72*3600+7*86400)['due']

def test_failed_read_retries_not_success_and_backoff_applies_to_manual():
    s=seeded();s.update(retry_pending=True,next_allowed_at=at('2026-11-05T10:00'))
    assert not a.apple_due(s,at('2026-11-05T09:59'),manual=True)['due']
    assert a.apple_due(s,at('2026-11-05T10:00'))['due']

@pytest.mark.parametrize('stamp,slot',[('2026-11-04T05:59','2026-11-03@06:00'),('2026-11-04T06:00','2026-11-04@06:00'),('2026-11-09T13:00','2026-11-09@06:00')])
def test_latest_due_slot_coalesces_days_and_does_not_advance_early(stamp,slot):
    assert a.daily_slot(at(stamp))[0]==slot
    due=a.announcement_due({},at(stamp));assert due['due'] and due['slots']==[slot]

def test_manual_before06_does_not_cover_future_slot():
    s={'slot_success':{a.daily_slot(at('2026-11-04T05:00'))[0]:at('2026-11-04T05:00')}}
    assert not a.announcement_due(s,at('2026-11-04T05:30'))['due']
    assert a.announcement_due(s,at('2026-11-04T06:00'))['due']

def test_after06_manual_covers_today_without24h_drift():
    s={'daily_success_slot':'2026-11-04@06:00'}
    assert not a.announcement_due(s,at('2026-11-04T18:00'))['due']
    assert a.announcement_due(s,at('2026-11-04T18:00'))['next_query_at']==at('2026-11-05T06:00')

@pytest.mark.parametrize('failures,seconds',[(1,900),(2,1800),(3,3600),(4,10800),(8,10800)])
def test_retry_sequence(failures,seconds):
    assert a.retry_wait(failures)==seconds

def test_persisted_auth_and_backoff_deny_queries():
    assert not a.announcement_due({'auth_blocked':True},at('2026-11-04T13:00'),True)['due']
    s={'next_allowed_at':at('2026-11-05T13:00')}
    assert not a.announcement_due(copy.deepcopy(s),at('2026-11-04T13:00'),True)['due']
