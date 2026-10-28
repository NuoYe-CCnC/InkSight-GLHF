"""Offline public-announcement parser and independent cadence contracts."""
import copy
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from core import codex_announcement_parser as p
from core import codex_announcement_schedule as s
from core import codex_announcements as a
from core import desktop_service, operator_config

BJ = timezone(timedelta(hours=8))
NOW = int(datetime(2026, 10, 23, 12, tzinfo=BJ).timestamp())
AUTHOR = "thsottiaux"
AUTHOR_ID = s.DEFAULTS["target_user_id"]
IDENT = "1234567890000000001"


def at(hour, minute=0, day=23):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp())


def post(text=None, *, ident=IDENT, stamp=NOW-60, parent=None, author=AUTHOR, **extra):
    return {"type": "status", "id": ident, "text": text or "For all Codex Plus users, we will reset your weekly quota within two hours.",
            "created_timestamp": stamp, "url": f"https://x.com/{author}/status/{ident}",
            "author": {"screen_name": author, "id": AUTHOR_ID},
            "replying_to": {"screen_name": AUTHOR, "status": parent} if parent else None, **extra}


def parsed(rows, now=NOW):
    posts, rejected = p.normalize_posts({"code": 200, "results": rows}, AUTHOR, now, author_id=AUTHOR_ID)
    revisions, more = p.parse_posts(posts, now=now)
    return p.select_events(revisions, now=now, plan="plus"), rejected + more


def test_explicit_two_kinds_and_no_account_mutation():
    rows = [post(), post("All Codex Plus users will receive more reset credits within three hours.", ident="1234567890000000002")]
    before = copy.deepcopy(rows)
    events, _ = parsed(rows)
    assert set(events) == {"quota_reset", "reset_grant"}
    assert events["quota_reset"]["target_at"] == NOW-60+7200
    assert events["reset_grant"]["target_at"] == NOW-60+10800
    assert rows == before
    assert all("quantity" not in v for v in events.values())


@pytest.mark.parametrize("body,reason", [
    ("All Plus users will get reset credits within two hours.", "codex_scope_missing"),
    ("All Codex Plus users might get reset credits within two hours.", "conditional_or_uncertain"),
    ("If we reset Codex Plus limits within two hours, that would help.", "conditional_or_uncertain"),
    ("All Codex Plus users already received reset credits.", "negative_or_past_not_a_forecast"),
    ("All Codex Plus users will not receive reset credits tomorrow.", "negative_or_past_not_a_forecast"),
    ("All Codex Plus users will get reset credits tomorrow before 10am.", "timezone_missing_or_ambiguous"),
    ("All Codex Plus users will get reset credits at 10am PT.", "date_missing"),
    ("All Codex Plus users reset limits yesterday.", "future_intent_missing"),
])
def test_uncertain_negative_past_or_unscoped_posts_not_displayed(body, reason):
    events, rejected = parsed([post(body)])
    if reason in {'timezone_missing_or_ambiguous','date_missing'}:
        assert events and all(e['target_at'] is None and e['time_type']=='unknown' for e in events.values())
    else:assert not events
    assert reason in [r["reason"] for r in rejected]


def test_retweet_quote_and_wrong_author_id_cannot_supply_scope():
    foreign = post(author="somebody", text="All Codex Plus users will receive reset credits within two hours.")
    own_quote = post("Interesting", ident="1234567890000000002", quote=foreign)
    spoof = post(ident="1234567890000000003")
    spoof["author"]["id"] = "999999"
    events, rejected = parsed([foreign, own_quote, spoof])
    assert not events
    assert sum(r["reason"] == "not_target_author" for r in rejected) == 2


def test_explicit_product_event_without_plan_is_allowed_but_not_assumed_all():
    events,_=parsed([post('Codex will reset limits within two hours.')])
    assert events['quota_reset']['plans']==[]
    assert events['quota_reset']['plan_scope']=='unspecified'


def test_relative_deadline_uses_post_publication_not_collection():
    pub = at(23, 30, day=22)
    stamp, basis = p.explicit_deadline("tomorrow before 10am PST", pub)
    local_pub = datetime.fromtimestamp(pub, timezone(timedelta(hours=-8)))
    expected = datetime.combine(local_pub.date()+timedelta(days=1), datetime.min.time(), timezone(timedelta(hours=-8))).replace(hour=10)
    assert stamp == int(expected.timestamp())
    assert basis == "explicit_zone_and_publication_date"
    assert p.explicit_deadline("tomorrow before 10am PT", pub)[0] == stamp - 3600


@pytest.mark.parametrize("text", ["tomorrow at 25:00 UTC", "2026-02-30 at 10:00 UTC", "today 10am PT and 11am UTC", "next Monday 10am PT", "2026-10-24T12:00Z and 2026-10-24T13:00Z"])
def test_ambiguous_or_invalid_time_has_no_deadline(text):
    assert p.explicit_deadline(text, NOW)[0] is None


def test_explicit_day_end_and_iso_and_dst_rejection():
    t, _ = p.explicit_deadline("tomorrow before end of the day UTC", NOW)
    assert datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M") == "10-25 00:00"
    assert p.explicit_deadline("2026-10-24T10:00:00+08:00", NOW)[0] == at(10, day=24)
    pub = int(datetime(2026, 11, 1, 0, tzinfo=timezone.utc).timestamp())
    assert p.explicit_deadline("November 1 1:30am PT", pub)[1] == "ambiguous_dst_clock"


def test_author_reply_correction_cancel_and_expiry_never_revives_parent():
    root = post("All Codex Plus users will reset weekly quota within six hours.")
    reply = post("Correction: lands within two hours.", ident="1234567890000000002", parent=IDENT, stamp=NOW)
    events, _ = parsed([root, reply])
    assert events["quota_reset"]["post_id"] == reply["id"]
    assert parsed([root, reply], NOW+7200)[0] == {}
    cancel = post("Cancelled. Not happening.", ident="1234567890000000003", parent=reply["id"], stamp=NOW+1)
    assert parsed([root, reply, cancel], NOW+2)[0] == {}
    unknown = post("Update: we will share a new time tomorrow.", ident="1234567890000000003", parent=reply["id"], stamp=NOW+1)
    assert parsed([root, reply, unknown], NOW+2)[0] == {}


def test_explicit_fulfillment_only_for_same_author_event_chain():
    root = post()
    done = post("Done. Completed for all Codex Plus users.", ident="1234567890000000002", parent=IDENT, stamp=NOW)
    assert parsed([root, done])[0] == {}


def test_nearest_future_per_kind_and_latest_same_time_dedup():
    rows = [post(), post(ident="1234567890000000002", stamp=NOW-30),
            post("All Codex Plus users will reset weekly quota within one hour.", ident="1234567890000000003")]
    assert parsed(rows)[0]["quota_reset"]["post_id"] == rows[2]["id"]
    events, _ = parsed([post("All Codex Pro users will reset weekly quota within two hours.")])
    assert not events
    assert parsed([post("All Codex users will reset weekly quota within two hours.")])[0]


def test_plan_labels_not_adjectives_or_tool_names():
    assert p._plans('All Codex Plus users will receive free reset credits within two hours.')==['plus']
    assert p._plans('Codex users will receive free reset credits within two hours.')==[]
    assert p._plans('Codex users will get pro tools within two hours.')==[]
    assert p._plans('For Free, Plus and Pro users of Codex, limits will reset within two hours.')==['free','plus','pro']


@pytest.mark.parametrize("remaining,active,light", [(31,60,120),(30,30,60),(10,30,60),(0,30,60),(None,60,120),(-1,60,120),(True,60,120)])
def test_cadence_boundaries_without_extra_low10_tier(remaining, active, light):
    cfg = s.DEFAULTS
    assert s.interval_seconds(cfg,"active",remaining,{},NOW) == active*60
    assert s.interval_seconds(cfg,"light",remaining,{},NOW) == light*60
    assert s.interval_seconds(cfg,"night",remaining,{},NOW) is None


@pytest.mark.parametrize("minutes,expected", [(121,60),(120,15),(31,15),(30,10),(1,10)])
def test_near_event_acceleration_not_night(minutes,expected):
    events={"quota_reset":{"target_at":NOW+minutes*60}}
    assert s.interval_seconds(s.DEFAULTS,"active",80,events,NOW) == expected*60
    assert s.interval_seconds(s.DEFAULTS,"night",0,events,NOW) is None


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    old=a.STATE_FILE
    a.configure_state_file(tmp_path/'announcements.json')
    config=copy.deepcopy(operator_config.DEFAULT_CONFIG)
    config['codex_announcements']['enabled']=True
    monkeypatch.setattr(a,'_config',lambda:config)
    monkeypatch.setattr(a,'account_context',lambda cfg,now:{'account_present':True,'plan':'plus','plan_valid_until':now+86400,'remaining':80})
    monkeypatch.setattr(s,'mode_at',lambda config,now:'active' if 8<=datetime.fromtimestamp(now,BJ).hour<22 else 'night')
    yield config
    a.configure_state_file(old)


def requester(rows=None):
    return lambda path,query,timeout:(200,{'code':200,'results':rows or []})


def test_default_disabled_no_transport_or_account_read(monkeypatch):
    monkeypatch.setattr(a,'_config',lambda:copy.deepcopy(operator_config.DEFAULT_CONFIG))
    monkeypatch.setattr(a,'account_context',lambda *args:pytest.fail('must not inspect account'))
    assert a.tick(requester=lambda *args:pytest.fail('network'))['reason']=='disabled'
    assert a.snapshot()['enabled'] is False


def test_success_then_no_change_does_not_refresh_again(isolated):
    assert a.tick(now=NOW,requester=requester([post()]))['ok']
    first=a.snapshot(now=NOW)
    assert first['quota_reset'] and not first['reset_grant']
    assert a.tick(now=NOW+60,requester=lambda *args:pytest.fail('not due'))['queried'] is False
    assert a.snapshot(now=NOW+1)==first
    assert a.snapshot(now=NOW+7200)['quota_reset'] is None


def test_night_catchup_success_persistent_then06_still_runs(isolated):
    assert a.tick(now=at(2),requester=requester())['ok']
    assert a.tick(now=at(3),requester=lambda *args:pytest.fail('duplicate wake'))['queried'] is False
    assert a.tick(now=at(6),requester=requester())['ok']
    assert a.tick(now=at(6)+30,requester=lambda *args:pytest.fail('duplicate six'))['queried'] is False
    data=json.loads(a.STATE_FILE.read_text())
    assert set(data['slot_success'])=={'2026-10-23@00:00','2026-10-23@06:00'}


def test_failed_night_not_counted_success_and_respects_backoff(isolated):
    def fail(*args): raise a.SourceFailure('network_or_timeout')
    assert not a.tick(now=at(1),requester=fail)['ok']
    assert not json.loads(a.STATE_FILE.read_text())['slot_success']
    assert not a.tick(now=at(1)+60,requester=lambda *args:pytest.fail('backoff'))['queried']
    assert a.tick(now=at(1)+300,requester=requester())['ok']


def test_midnight_trigger_and_catchup_coalesce(isolated):
    assert a.tick(now=at(0),requester=requester())['ok']
    assert not a.tick(now=at(0),requester=lambda *args:pytest.fail('duplicate'))['queried']
    assert a.tick(now=at(5,59)+50,requester=lambda *args:pytest.fail('already covered'))['queried'] is False


@pytest.mark.parametrize('status,headers,minimum', [(429,{'Retry-After':'1800'},1800),(429,{'x-ratelimit-reset':str(NOW+2400)},2400),(403,{},86400),(401,{},86400),(500,{},300)])
def test_provider_errors_backoff_not_slot_success(isolated,status,headers,minimum):
    def fail(*args): raise a.SourceFailure('http_error',status=status,headers=headers)
    assert not a.tick(now=NOW,requester=fail)['ok']
    state=json.loads(a.STATE_FILE.read_text())
    assert state['next_allowed_at']>=NOW+minimum
    assert state['last_success_at']==0 and not state['slot_success']


def test_global_pause_blocks_new_forecast_work(isolated):
    desktop_service.gate.pause()
    assert a.tick(now=NOW,requester=lambda *args:pytest.fail('paused'))['reason']=='paused'


def test_204_is_success_not_retraction_and_expiry_local_on_failure(isolated):
    assert a.tick(now=NOW,requester=requester([post()]))['ok']
    def unchanged(path,query,timeout):
        return (200,{'code':200,'status':post()}) if '/status/' in path else (204,None)
    assert a.tick(now=NOW+900,requester=unchanged)['ok']
    assert a.snapshot(now=NOW+901)['quota_reset']
    assert a.snapshot(now=NOW+7200)['quota_reset'] is None


def test_deleted_post_never_revives_from_cached_parent(isolated):
    assert a.tick(now=NOW,requester=requester([post()]))['ok']
    def deleted(path,query,timeout):
        if '/status/' in path:return 200,{'code':200,'status':{'type':'tombstone','reason':'deleted'}}
        return 204,None
    assert a.tick(now=NOW+900,requester=deleted)['ok']
    assert a.snapshot(now=NOW+901)['quota_reset'] is None


def test_concurrent_trigger_is_coalesced_without_second_transport(isolated):
    entered,release=threading.Event(),threading.Event()
    def slow(*args): entered.set(); assert release.wait(2); return 204,None
    worker=threading.Thread(target=lambda:a.tick(now=NOW,requester=slow))
    worker.start(); assert entered.wait(2)
    assert a.tick(now=NOW,requester=lambda *args:pytest.fail('duplicate'))['reason']=='concurrent_coalesced'
    release.set();worker.join(2);assert not worker.is_alive()


def test_corrupt_state_no_reset_or_network(isolated):
    a.STATE_FILE.write_text('{broken')
    assert a.tick(now=NOW,requester=lambda *args:pytest.fail('corrupt'))['reason']=='state_or_configuration_unavailable'
    assert a.STATE_FILE.read_text()=='{broken'


def test_forecast_config_validated_and_does_not_change_other_defaults():
    cfg=copy.deepcopy(operator_config.DEFAULT_CONFIG)
    assert not operator_config.validate_config(cfg)
    cfg['codex_announcements']['target_handle']='https://evil.example/'
    assert any('codex_announcements.target_handle' in e for e in operator_config.validate_config(cfg))
    cfg=copy.deepcopy(operator_config.DEFAULT_CONFIG)
    cfg['codex_announcements']['urgent_interval_minutes']=1
    assert any('urgent_interval_minutes' in e for e in operator_config.validate_config(cfg))


@pytest.mark.parametrize('body', [
    'All Codex Plus users will reset weekly quota within two hours or within three hours.',
    'All Codex Plus users will reset weekly quota within two hours, tomorrow before 10am PT.',
    'All Codex Plus users will reset weekly quota today or tomorrow before 10am PT.',
])
def test_multiple_deadlines_never_pick_first(body):
    events=parsed([post(body)])[0]
    assert events and all(e['time_type']=='unknown' and e['target_at'] is None for e in events.values())


def test_uncertain_parent_and_conditional_correction_do_not_create_or_revive():
    root=post('All Codex Plus users might get reset credits within two hours.')
    reply=post('Lands within three hours.',ident='1234567890000000002',parent=IDENT,stamp=NOW)
    assert not parsed([root,reply])[0]
    root=post()
    reply['text']='Update: we might reset within three hours.'
    assert not parsed([root,reply])[0]


def test_06_failure_retries_after_trigger_grace_and_restart(isolated):
    def fail(*args): raise a.SourceFailure('network_or_timeout')
    assert not a.tick(now=at(6),requester=fail)['ok']
    assert json.loads(a.STATE_FILE.read_text())['pending_slots']==['2026-10-23@06:00']
    assert a.tick(now=at(6,5),requester=requester())['ok']
    state=json.loads(a.STATE_FILE.read_text())
    assert state['slot_success']['2026-10-23@06:00']==at(6,5)
    assert state['pending_slots']==[]


def test_success_before06_coalesces_boundary_without_query(isolated):
    # A midnight catch-up finishing just before 06 also satisfies that boundary.
    assert a.tick(now=at(5,59)+40,requester=requester())['ok']
    assert a.tick(now=at(6),requester=lambda *args:pytest.fail('second request'))['reason']=='successful_boundary_coalesced'


def test_total_budget_cursor_and_overlap_bounded(isolated):
    rows=[post(),post('All Codex Plus users will receive reset credits within three hours.',ident='1234567890000000002')]
    assert a.tick(now=NOW,requester=requester(rows))['ok']
    calls=[]
    def transport(path,query,timeout):
        calls.append((path,query))
        if '/status/' in path:
            row=next(row for row in rows if path.endswith(row['id']))
            return 200,{'code':200,'status':row}
        return 204,None
    result=a.tick(now=NOW+3600,requester=transport)
    assert result['ok'] and result['requests']<=4
    assert len([path for path,_ in calls if '/status/' in path])==2
    assert calls[0][1]['since']==NOW-61
    assert a.snapshot(now=NOW+3600)['schema_version']==2
    assert a.snapshot(now=NOW+3600)['selected_event']['kind']=='quota_reset'


def test_http_date_retry_header():
    from email.utils import formatdate
    error=a.SourceFailure('http_error',status=429,headers={'Retry-After':formatdate(NOW+4000,usegmt=True)})
    assert a._retry_at(error,NOW,s.DEFAULTS,1)==NOW+4000


@pytest.mark.parametrize('status,body,content_type,reason', [
    (204,b'',None,None),(200,b'{bad','application/json','invalid_json'),
    (200,b'{}','text/html','non_json_response'),
    (200,b'{"code":429}','application/json','provider_error'),
    (200,b'x'*(a.MAX_BYTES+1),'application/json','response_too_large'),
])
def test_fixed_host_transport_no_credentials_and_bounds(monkeypatch,status,body,content_type,reason):
    from email.message import Message
    headers=Message()
    if content_type: headers['Content-Type']=content_type
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self,count):assert count==a.MAX_BYTES+1;return body
    response=Response();response.status=status;response.headers=headers
    class Opener:
        def open(self,request,timeout):
            assert request.full_url.startswith('https://api.fxtwitter.com/2/profile/thsottiaux/statuses?')
            assert request.get_method()=='GET' and timeout==8
            assert not any(k.lower() in {'authorization','cookie'} for k in request.headers)
            return response
    monkeypatch.setattr(a.urllib.request,'build_opener',lambda *args:Opener())
    if reason:
        with pytest.raises(a.SourceFailure,match=reason):a._request('/2/profile/thsottiaux/statuses',{'count':20},8)
    else:assert a._request('/2/profile/thsottiaux/statuses',{'count':20},8)==(204,None)


@pytest.mark.parametrize('url',['http://api.fxtwitter.com/2/status/12345','https://evil.example/','https://name@api.fxtwitter.com/','https://api.fxtwitter.com:444/'])
def test_redirect_rejects_cross_host_downgrade_credentials_port(url):
    with pytest.raises(a.SourceFailure,match='redirect_rejected'):
        a._SameHostRedirect().redirect_request(None,None,302,'',{},url)


def test_actual_calendar_policy_manual_day_and_custom_windows():
    cfg=copy.deepcopy(operator_config.DEFAULT_CONFIG)
    cfg['device_policy']['activity_windows']['restday']=[{'id':'custom','start':'10:00','end':'11:00','mode':'active'}]
    assert s.mode_at(cfg,at(10),is_workday=lambda d:False)=='active'
    assert s.mode_at(cfg,at(12),is_workday=lambda d:False)=='night'
    assert s.mode_at(cfg,at(12),is_workday=lambda d:True)=='active'


def test_plan_identity_ttl_independent_of_quota(monkeypatch):
    from core import data_cache,desktop_quota
    group={'last_success':NOW-1801,'value':{'plan':'PLUS','source':'mac','account_key':'synthetic'}}
    monkeypatch.setattr(data_cache,'get_group',lambda key:group)
    monkeypatch.setattr(desktop_quota,'account_key',lambda:'synthetic')
    monkeypatch.setattr(desktop_quota,'quota_snapshot_base',lambda **kw:{'remaining_percent':None})
    context=a.account_context(s.DEFAULTS,NOW)
    assert context['plan']=='plus' and context['remaining'] is None
    group['value']['account_key']='different'
    assert a.account_context(s.DEFAULTS,NOW)['plan'] is None
    group['value']['account_key']='synthetic';group['last_success']=NOW-86400
    assert a.account_context(s.DEFAULTS,NOW)['plan'] is None


def test_scope_ttl_and_account_unknown_expire_without_network(isolated):
    assert a.tick(now=NOW,requester=requester([post()]))['ok']
    context={'account_present':True,'plan':'plus','plan_valid_until':NOW+60,'remaining':None}
    assert a.snapshot(now=NOW,context=context)['quota_reset']['valid_until']==NOW+60
    assert not a.snapshot(now=NOW+60,context=context)['quota_reset']
    context['account_present']=False
    assert not a.snapshot(now=NOW,context=context)['quota_reset']


def test_edit_provenance_survives_unchanged_recheck(isolated):
    assert a.tick(now=NOW,requester=requester([post()]))['ok']
    revised=post('All Codex Plus users will reset weekly quota within three hours.')
    assert a.tick(now=NOW+900,requester=requester([revised]))['ok']
    # The correction moves the event out of the near window: ordinary cadence
    # resumes instead of forcing another request at the old 15-minute interval.
    assert not a.tick(now=NOW+1800,requester=lambda *args:pytest.fail('not due'))['queried']
    assert a.tick(now=NOW+4500,requester=requester([revised]))['ok']
    saved=json.loads(a.STATE_FILE.read_text())['posts'][IDENT]
    assert saved['previous_text']==post()['text'] and saved['edited_observed_at']==NOW+900


def test_verified_cancellation_survives_later_independent_source_failure(isolated):
    grant=post('All Codex Plus users will receive reset credits within three hours.',ident='1234567890000000002')
    assert a.tick(now=NOW,requester=requester([post(),grant]))['ok']
    cancel=post('Cancelled. Not happening.',parent=IDENT,ident='1234567890000000003',stamp=NOW+1)
    def partial(path,query,timeout):
        if '/status/' in path:raise a.SourceFailure('http_error',status=503)
        return 200,{'code':200,'results':[cancel]}
    assert not a.tick(now=NOW+900,requester=partial)['ok']
    snapshot=a.snapshot(now=NOW+901)
    assert snapshot['quota_reset'] is None and snapshot['reset_grant']
    state=json.loads(a.STATE_FILE.read_text())
    assert state['last_success_at']==NOW and state['next_allowed_at']>=NOW+1200
    assert cancel['id'] in state['posts']
