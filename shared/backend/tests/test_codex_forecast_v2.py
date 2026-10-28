"""Frozen public cases and adversarial contracts. Never seed live state."""
import copy
from datetime import datetime,timezone,timedelta
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import subprocess
import urllib.error

import pytest
from core import codex_announcement_parser as p, codex_announcement_time as t
from core import codex_announcement_view as v, codex_announcements as a
from core import codex_announcement_sources as s, codex_announcement_schedule as policy
from core import codex_announcement_network as network, activity_signals

AUTHOR='thsottiaux';ID=policy.DEFAULTS['target_user_id'];PUB=1793155373;NOW=PUB+120
ROOT=Path(__file__).resolve().parents[3]


def row(text,number=1,at=PUB,parent=None):
    ident=str(2115273064481218828+number)
    return dict(id=ident,author=AUTHOR,author_id=ID,text=text,published_at=at,parent_id=parent,url=f'https://x.com/{AUTHOR}/status/{ident}')


def real_case():
    anchor=row('Day 18/ Codex & work. But yes… that’s a reset!',11,PUB-5*86400)
    anchor.update(id='2113492187770175603',url='https://x.com/thsottiaux/status/2113492187770175603',published_at=1792730779)
    main=row('Day 23/ Much requested iCloud Main & Calendar plugins. Just kidding, today is a reset.')
    reply=row('Lands by midnight PST.',2,PUB+45,main['id'])
    reply.update(id='2115273253711409208',url='https://x.com/thsottiaux/status/2115273253711409208')
    return [anchor,main,reply]


def select(rows,now=NOW,records=None,plan='plus'):
    events,rejected=p.parse_posts(rows,now=now,series_records=records)
    return p.select_events(events,now=now,plan=plan),events,rejected


def snap(rows,now=NOW,**kw):
    state=a._default(policy.DEFAULTS);state['posts']={x['id']:x for x in rows}
    return a._snapshot(state,policy.DEFAULTS,now,dict(account_present=True,plan='plus',plan_valid_until=now+86400),**kw)[0]


def test_real_day23_chain_midnight_and_canonical_identity():
    selected,events,rejected=select(real_case())
    e=selected['reset_unspecified']
    expected=datetime(2026,10,28,16,tzinfo=t.BJ).timestamp()
    assert e['target_at']==expected
    assert e['plan_scope']=='unspecified' and not e['plans']
    assert e['source_time_zone']=='PST' and e['time_relation']=='before'
    assert e['post_id']=='2115273253711409208'
    snapshot=snap(real_case());assert v.nearest_event(snapshot,NOW)==snapshot['selected_event']
    assert snapshot['canonical_source_url'].endswith(e['post_id'])
    assert v.display_lines(snapshot['selected_event'])==('预计','10-28 16:00 前重置')
    assert snap(real_case(),now=expected)['selected_event'] is None
    assert select(real_case(),now=expected)[0]=={}


@pytest.mark.parametrize('text,accepted',[
    ('Day 23/ Other plugins. Just kidding, today is a reset.',True),
    ('Day 23/ Today is a reset. Just kidding!',False),
    ('Day 23/ Haha, maybe a reset tomorrow.',False),
    ('Please reset Codex limits within two hours.',False),
    ('When will Codex reset weekly quota within two hours?',False),
    ('If we reset Codex limits within two hours, that could help.',False),
    ('Codex will reset weekly quota within two hours.',True),
    ('Codex already reset weekly quota.',False),
    ('Codex reset has been completed.',False),
])
def test_semantics(text,accepted):
    posts=real_case()[:1]+[row(text)]
    assert bool(select(posts)[0])==accepted


def test_series_only_product_no_time_plan_transfer_and_topic_stop():
    posts=real_case()[:2]
    selected,_,_=select(posts)
    assert selected['reset_unspecified']['target_at'] is None
    unrelated=row('Reset my router tomorrow before 10am PST.',3)
    assert not select(posts[:1]+[unrelated])[0]
    end=row('Day 22/ This series ends.',5,PUB-86400)
    assert not select([posts[0],end,posts[1]])[0]
    topic=row('Day 22/ Database reset scheduled.',6,PUB-86400)
    assert not select([posts[0],topic,posts[1]])[0]
    expired=row('Day 18/ Codex reset day today.',11,PUB-15*86400)
    assert not select([expired,posts[1]])[0]


def test_series_persistence_rolling_and_hard_cap():
    anchor=row('Day 1/ Codex reset day today.',11,PUB)
    records,_=p.series_state([anchor])
    for number in range(2,100):
        post=row(f'Day {number}/ Today is a reset.',20+number,PUB+(number-1)*86400)
        records,contexts=p.series_state([post],records)
        assert records[0]['expires_at']<=PUB+90*86400
        if number<91:assert post['id'] in contexts
        else:assert post['id'] not in contexts
    ended=row('Day 100/ Series over.',150,PUB+89*86400)
    records,_=p.series_state([ended],records)
    assert records[0]['termination_reason']=='explicit_end'


@pytest.mark.parametrize('mutation',[
    lambda r:r[2].update(parent_id=r[2]['id']),
    lambda r:r[1].update(parent_id=r[2]['id']),
    lambda r:r[1].update(author='other'),
    lambda r:r[1].update(author_id='0000000000000000000'),
    lambda r:r[1].update(published_at=PUB+60),
])
def test_reply_identity_order_cycles(mutation):
    rows=real_case();mutation(rows)
    selected,_,reject=select(rows)
    assert all(e['post_id']!=rows[2]['id'] for e in selected.values())
    assert any(e['reason'].startswith(('invalid_reply','reply_cycle')) for e in reject)


def test_cross_day_does_not_apply_parent_today():
    root=row('Codex will reset weekly quota today.',at=datetime(2026,10,27,23,59,tzinfo=t.ZONES['PST']).timestamp())
    reply=row('Lands by midnight PST.',2,root['published_at']+120,root['id'])
    e=select([root,reply],now=reply['published_at']+1)[0]['quota_reset']
    assert e['time_type']=='unknown' and e['target_at'] is None
    assert e['time_basis']=='parent_date_crosses_day'


@pytest.mark.parametrize('phrase,zone,hours',[
    ('tomorrow before 10am PST','PST',16),('tomorrow before 10am PDT','PDT',15),
    ('tomorrow before 10am PT','PT',15),('today by midnight PST','PST',30),
    ('today at noon PST','PST',18),('今天结束前 北京时间','北京时间',8),
])
def test_literal_zones_and_midnight(phrase,zone,hours):
    base=int(datetime(2026,10,27,18,tzinfo=t.ZONES['PST']).timestamp())
    result=t.parse_time(phrase,base)
    # Derive expected wall date using its own literal source timezone.
    day=datetime.fromtimestamp(base,t.ZONES[zone]).date()
    if 'tomorrow' in phrase:day+=timedelta(days=1)
    if 'midnight' in phrase or '结束前' in phrase:
        expected=datetime.combine(day+timedelta(days=1),datetime.min.time(),t.ZONES[zone]).timestamp()
    else:expected=datetime(day.year,day.month,day.day,12 if 'noon' in phrase else 10,tzinfo=t.ZONES[zone]).timestamp()
    assert result['target_at']==expected


@pytest.mark.parametrize('phrase,seconds',[('within two hours',7200),('in two hours',7200),('over next two hours',7200),('over the next two hours',7200)])
def test_durations(phrase,seconds):assert t.parse_time(phrase,PUB)['target_at']==PUB+seconds


def test_date_range_beijing_boundary_and_unknown_ttl():
    us=t.parse_time('today PT',PUB);bj=t.parse_time('今天 北京时间',PUB)
    assert us['time_type']=='date_range' and us['display_date'] is None
    assert bj['time_type']=='date_range' and bj['display_date']=='2026-10-28'
    unknown=select([row('Codex will reset weekly quota.')])[0]['quota_reset']
    assert unknown['expires_at']==PUB+86400 and unknown['target_at'] is None
    repeated=row('Codex will reset weekly quota.');repeated.update(collected_at=PUB+80000,last_seen_at=PUB+80000)
    assert select([repeated],now=PUB+86400)[0]=={}
    assert select([repeated],now=PUB+80000)[0]['quota_reset']['expires_at']==PUB+86400


def test_cancel_complete_delay_and_corrected_negation():
    root=row('Codex will reset weekly quota within two hours.')
    for text in ('Cancelled. Not happening.','Done. Completed.'):
        revision=row(text,2,PUB+30,root['id'])
        assert not select([root,revision])[0]
    delayed=row('Delayed: Codex will reset weekly quota. Time to be confirmed.',2,PUB+30,root['id'])
    event=select([root,delayed])[0]['quota_reset']
    assert event['target_at'] is None and event['expires_at']==PUB+30+86400
    correction=row('Correction: not today but tomorrow before 10am PST.',2,PUB+30,root['id'])
    assert select([root,correction])[0]['quota_reset']['target_at'] is not None


def test_exclusion_not_all_and_selection_stability():
    excluded=row('Codex will reset weekly quota within two hours, except Plus users.')
    assert not select([excluded])[0]
    assert select([excluded],plan='pro')[0]['quota_reset']['plan_scope']=='unspecified'
    unknown=row('Codex will reset weekly quota.',3,PUB+10)
    exact=row('Codex will reset weekly quota within three hours.')
    for rows in ([unknown,exact],[exact,unknown]):
        snapshot=snap(rows);assert snapshot['selected_event']['post_id']==exact['id']
        assert snapshot['quota_reset']==snapshot['selected_event']


@pytest.mark.parametrize('url',[
 'http://x.com/thsottiaux/status/2115273253711409208',
 'https://x.com:443/thsottiaux/status/2115273253711409208',
 'https://evil.com/thsottiaux/status/2115273253711409208',
 'https://x.com@evil.com/thsottiaux/status/2115273253711409208',
 'https://x.com/other/status/2115273253711409208',
 'https://x.com/thsottiaux/status/2115273253711409208?redirect=evil',
 'https://x.com/thsottiaux/status/2115273253711409208#fragment',
 ' https://x.com/thsottiaux/status/2115273253711409208',
 'https://x.com/thsottiaux/status/2115273253711409208/',
 'javascript:alert(1)',
])
def test_malicious_canonical_source(url):
    snapshot=snap(real_case());event=snapshot['selected_event'];event['canonical_source_url']=url;snapshot['canonical_source_url']=url
    assert v.nearest_event(snapshot,NOW) is None
    assert p.canonical_source_url(url,AUTHOR,'2115273253711409208') is None


def test_unknown_schema_invalidity_quota_activity_and_stable_visual_key():
    snapshot=snap([row('Codex will reset weekly quota.')]);event=snapshot['selected_event']
    assert v.nearest_event(snapshot,NOW)==event
    ai={'announcements':snapshot,'codex_7d_used':37};plain={'codex_7d_used':37}
    assert activity_signals.activity_key(ai)==activity_signals.activity_key(plain)
    before=activity_signals.ai_visual_key(ai,now=NOW)
    snapshot['last_polled_at']=NOW+30
    assert activity_signals.ai_visual_key(ai,now=NOW+30)==before
    assert activity_signals.ai_visual_key(ai,now=PUB+86400)==activity_signals.ai_visual_key(plain,now=PUB+86400)
    for key,value in [('target_at',0),('expires_at',PUB+86401),('time_type','guess'),('valid_until',NOW)]:
        bad=copy.deepcopy(snapshot);bad['selected_event'][key]=value
        assert not v.visible_events(bad,NOW)


def test_auxiliary_retry_persistence_no_new_cycle_each_tick():
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    calls=[]
    def fail(*args):calls.append(args);raise s.DiscoveryError('read_timeout')
    state=s.collect({},cfg,NOW,requester=fail)
    assert state['whenreset']['next_allowed_at']==NOW+600
    state=s.collect(json.loads(json.dumps(state)),cfg,NOW+599,requester=fail);assert len(calls)==1
    state=s.collect(state,cfg,NOW+600,requester=fail);assert len(calls)==2
    assert state['whenreset']['next_allowed_at']==NOW+1800
    state=s.collect(state,cfg,NOW+1800,requester=fail);assert len(calls)==3
    assert state['whenreset']['retry_cycle'] is None
    assert state['whenreset']['next_allowed_at']==NOW+1800+7200


@pytest.mark.parametrize('reason,status',[('decode',None),('tls_certificate',None),('robots_disallowed',403),('http_error',401),('http_error',403),('http_error',429)])
def test_auxiliary_no_fast_retry_permanent_failures(reason,status):
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    def fail(*args):raise s.DiscoveryError(reason,status,14400 if status==429 else 0)
    result=s.collect({},cfg,NOW,requester=fail)['whenreset']
    assert result['retry_cycle'] is None and result['next_allowed_at']>=NOW+7200
    if status==429:assert result['next_allowed_at']>=NOW+14400


def test_retry_source_success_and_night_only_legal_opportunity():
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    def fail(*args):raise s.DiscoveryError('connection')
    state=s.collect({},cfg,NOW,requester=fail)
    assert not s.due(state,cfg,NOW+600,allow_retry=False)
    assert s.collect(state,cfg,NOW+600,requester=lambda *args:pytest.fail('night retry'),allow_retry=False)==state
    def good(name,path,timeout):return 'User-agent: *\nAllow: /' if path=='/robots.txt' else '<a href="https://x.com/thsottiaux/status/2115273253711409208">Source</a>'
    success=s.collect(state,cfg,NOW+600,requester=good)['whenreset']
    assert success['retry_cycle'] is None and success['next_allowed_at']==NOW+600+7200


@pytest.mark.parametrize('exc,reason',[(socket.gaierror(-2,'private detail'),'dns'),(ConnectionRefusedError(),'connection'),(TimeoutError(),'timeout'),(ssl.SSLCertVerificationError(),'tls_certificate'),(ssl.SSLError(),'tls_handshake'),(Exception('unknown'),'unknown_network')])
def test_failure_classification_no_secret_error_text(exc,reason):
    assert network.failure_reason(urllib.error.URLError(exc))==reason
    assert 'private detail' not in network.summary(exc)


def test_first_seen_migration_repeat_and_no_ack(tmp_path,monkeypatch):
    cfg={**policy.DEFAULTS,'enabled':True}
    post=row('Codex will reset weekly quota within two hours.')
    raw=dict(type='status',id=post['id'],text=post['text'],created_timestamp=PUB,url=post['url'],author={'screen_name':AUTHOR,'id':ID})
    def request(*args):return 200,{'code':200,'results':[raw]}
    old=a._default(cfg);old['posts']={post['id']:post}
    update=a._collect(old,cfg,NOW,request,{'plan':'plus'})
    assert update['posts'][post['id']]['first_seen_at'] is None
    assert update['posts'][post['id']]['last_seen_at']==NOW
    fresh=a._collect(a._default(cfg),cfg,NOW,request,{'plan':'plus'})
    assert fresh['posts'][post['id']]['first_seen_at']==NOW
    state={**a._default(cfg),**fresh};a._update_event_history(state,cfg,NOW,dict(account_present=True,plan='plus',plan_valid_until=NOW+86400),{})
    history=copy.deepcopy(state['event_history']);a._update_event_history(state,cfg,NOW+60,dict(account_present=True,plan='plus',plan_valid_until=NOW+86400),{})
    assert state['event_history']==history
    assert a._publication_diagnostics(snap([post]))['device_ack'] is False


def test_auxiliary_due_is_independent_of_primary_and_restart_lease(tmp_path,monkeypatch):
    cfg={**policy.DEFAULTS,'enabled':True,'whenreset_enabled':True}
    configuration={'codex_announcements':cfg}
    monkeypatch.setattr(a,'STATE_FILE',tmp_path/'announcements.json')
    monkeypatch.setattr(policy,'mode_at',lambda *args:'active')
    state=a._default(cfg);state.update(last_success_at=NOW,last_attempt_at=NOW,next_allowed_at=NOW+60)
    state['auxiliary_sources']=s.collect({},cfg,NOW,requester=lambda *args:(_ for _ in ()).throw(s.DiscoveryError('connection')))
    a.state_store.write_json(a.STATE_FILE,state)
    seen=[]
    def auxiliary(name,path,timeout):
        reserved=a.state_store.read_json(a.STATE_FILE)[0]
        assert reserved['auxiliary_sources'][name]['next_allowed_at']>NOW+600
        assert reserved['auxiliary_sources'][name]['retry_cycle']['attempts']==1
        seen.append(path)
        return 'User-agent: *\nAllow: /' if path=='/robots.txt' else '<a href="https://x.com/thsottiaux/status/2115273253711409208">Source</a>'
    result=a.tick(now=NOW+600,config=configuration,context={'account_present':False,'remaining':80},
                  requester=lambda *args:pytest.fail('auxiliary retry pulled main source'),auxiliary_requester=auxiliary)
    assert result['reason']=='auxiliary_only' and result['requests']==0 and seen
    saved=a.state_store.read_json(a.STATE_FILE)[0]
    assert saved['last_success_at']==NOW and saved['last_attempt_at']==NOW
    assert not saved['auxiliary_sources']['whenreset']['retry_cycle']
    assert not a.tick(now=NOW+601,config=configuration,context={'account_present':False,'remaining':80},requester=lambda *args:pytest.fail('unexpected primary'))['queried']


def test_primary_failure_keeps_verified_plan_and_auxiliary_success():
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    state=a._default(cfg);state['posts']={r['id']:r for r in real_case()}
    def failed(*args):raise a.SourceFailure('read_timeout')
    def auxiliary(name,path,timeout):return 'User-agent: *\nAllow: /' if path=='/robots.txt' else '<a href="https://x.com/thsottiaux/status/2115273253711409208">Source</a>'
    with pytest.raises(a.SourceFailure) as error:a._collect(state,cfg,NOW,failed,{'plan':'plus'},auxiliary)
    state.update(error.value.partial)
    assert state['auxiliary_sources']['whenreset']['state']=='current'
    assert a._snapshot(state,cfg,NOW,dict(account_present=True,plan='plus'))[0]['selected_event']['post_id']=='2115273253711409208'


def test_publication_times_are_not_delivery_ack():
    spec=importlib.util.spec_from_file_location('v2_cloud_publish',ROOT/'shared/tools/cloud_publish.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    document={'screen':{'pages':{'ai':{'announcements':snap(real_case())}}}}
    state={};module.record_announcement_snapshot(state,document,now=NOW)
    first=next(iter(state['_announcement_history'].values()))
    assert first['first_snapshot_at']==NOW and first['first_published_at'] is None
    module.record_announcement_snapshot(state,document,now=NOW+1,success=True)
    module.record_announcement_snapshot(state,document,now=NOW+2,success=True)
    assert first['first_snapshot_at']==NOW and first['first_published_at']==NOW+1 and first['last_published_at']==NOW+2
    assert 'device_ack' not in first


def test_correction_scope_replaces_old_plan_instead_of_union():
    root=row('Codex Plus users will reset weekly quota within two hours.')
    update=row('Correction: Codex Pro users will reset weekly quota within three hours.',2,PUB+30,root['id'])
    assert not select([root,update])[0]
    assert select([root,update],plan='pro')[0]['quota_reset']['plans']==['pro']


@pytest.fixture(scope='module')
def panel_assets():
    import os
    path=os.environ.get('INKSIGHT_TEST_FONT_DIR')
    if not path:pytest.skip('local MiSans glyphs required')
    spec=importlib.util.spec_from_file_location('v2_panel',ROOT/'shared/tools/misans_panel_render.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.FONT_DIR=Path(path)
    return module,module.load_fonts()


@pytest.mark.parametrize('variant',['exact','unknown','date_range','expired'])
def test_real_font_bounds_and_restore_layout(panel_assets,variant):
    from test_codex_announcement_views import document
    module,fonts=panel_assets;doc=document();doc['ts']=NOW
    prefs=doc['screen']['display_preferences'];prefs.update(show_codex_credits=True,show_openai_api_info=True)
    posts=real_case() if variant=='exact' else [row('Codex will reset weekly quota today 北京时间.' if variant=='date_range' else 'Codex will reset weekly quota.')]
    ai=doc['screen']['pages']['ai'];ai['announcements']=snap(posts)
    clock=PUB+86400 if variant=='expired' else NOW
    panel=module.Panel();module.render_ai_panel(panel,fonts,doc,clock)
    assert not panel.missing
    assert len(panel.fb)==48000
    if variant=='expired':
        ai.pop('announcements');plain=module.Panel();module.render_ai_panel(plain,fonts,doc,clock)
        assert panel.fb==plain.fb
    else:
        lines=v.display_lines(v.nearest_event(ai['announcements'],clock))
        upper=[op for op in panel.ops if op['text'] in lines]
        assert [op['baseline'] for op in upper]==[225,256]
        assert all(op['ink'][1]==461 for op in upper)
        assert not any(op['text'] in {'API 本月消费','点数余额'} for op in panel.ops)
        for op,left_label in zip(upper,['剩余','7 日窗口']):
            left=next(x for x in panel.ops if x['text']==left_label)
            assert left['ink'][1]+16<=op['ink'][0]


def test_uncertain_series_anchor_and_author_revised_anchor_do_not_authorize_posts():
    root=row('Day 18/ Not about Codex reset.',11,PUB-86400)
    plan=row('Day 23/ Today is a reset.')
    assert not select([root,plan])[0]
    anchor=row('Day 18/ Codex reset day today.',11,PUB-86400)
    record,_=p.series_state([anchor])
    anchor['text']='Day 18/ Not about Codex reset.'
    state,contexts=p.series_state([anchor,plan],record)
    assert state[0]['termination_reason']=='anchor_revised' and plan['id'] not in contexts


def test_reply_timezone_conflict_retains_unknown_and_explicit_correction_resolves():
    root=row('Codex will reset weekly quota today before 10pm PST.')
    reply=row('Lands today before 11pm PDT.',2,PUB+45,root['id'])
    e=select([root,reply])[0]['quota_reset']
    assert e['target_at'] is None and e['time_basis']=='timezone_conflicting_parent'
    reply['text']='Correction: lands today before 11pm PDT.'
    assert select([root,reply])[0]['quota_reset']['target_at'] is not None


@pytest.mark.parametrize('module,stage,expected',[(a,'connect','connect_timeout'),(a,'read','read_timeout'),(s,'connect','connect_timeout'),(s,'read','read_timeout')])
def test_transport_distinguishes_connect_and_read_timeout(monkeypatch,module,stage,expected):
    class Response:
        status=200
        headers=type('Headers',(dict,),{'get_content_type':lambda self:'application/json' if module is a else 'text/html'})()
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self,*args):raise TimeoutError()
    class Opener:
        def open(self,*args,**kwargs):
            if stage=='connect':raise urllib.error.URLError(TimeoutError())
            return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *args:Opener())
    with pytest.raises(a.SourceFailure if module is a else s.DiscoveryError) as failure:
        a._request('/2/status/2115273253711409208',{},2) if module is a else s.request('whenreset','/codex',2)
    assert failure.value.reason==expected


def test_diagnostics_reparse_old_rejections_and_estimate_schedule(monkeypatch):
    state=a._default(policy.DEFAULTS);state['posts']={r['id']:r for r in real_case()}
    state['diagnostics']=[dict(post_id=real_case()[-1]['id'],reason='codex_scope_missing')]
    snapshot=snap(real_case());rows=a._candidate_diagnostics(state,snapshot,NOW)
    chosen=next(r for r in rows if r['post_id']==real_case()[-1]['id'])
    assert chosen['selected'] and chosen['outcome']=='selected'
    assert chosen['display_label']=='预计 / 10-28 16:00 前重置'
    monkeypatch.setattr(policy,'mode_at',lambda *args:'active')
    state.update(last_success_at=NOW,last_attempt_at=NOW,next_allowed_at=NOW+60)
    assert a._next_query_time({},state,policy.DEFAULTS,NOW,{'remaining':80},{})==NOW+3600
    monkeypatch.setattr(policy,'mode_at',lambda *args:'night')
    next_time=a._next_query_time({},state,policy.DEFAULTS,NOW,{'remaining':80},{})
    assert datetime.fromtimestamp(next_time,t.BJ).strftime('%H:%M')=='00:00'
