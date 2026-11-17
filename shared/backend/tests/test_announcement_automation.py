import copy
import json
from datetime import datetime,timezone
import pytest
from core import codex_announcements as c, codex_announcement_analysis as m, codex_announcement_schedule as policy
from core import codex_announcement_parser as parser, codex_banked_rules, state_store, desktop_service
from test_automation_schedule import at

IDENT='1953337039510003712'

def post(ident,text,stamp,parent=None):
    return dict(id=ident,text=text,published_at=stamp,url=f'https://x.com/thsottiaux/status/{ident}',
                author='thsottiaux',author_id=IDENT,parent_id=parent)

def payload(posts):
    return {'code':200,'results':[dict(type='status',id=p['id'],text=p['text'],created_timestamp=p['published_at'],url=p['url'],
        author=dict(screen_name=p['author'],id=p['author_id'])) for p in posts]}

@pytest.fixture
def env(tmp_path,monkeypatch):
    cfg=copy.deepcopy(policy.DEFAULTS);cfg.update(enabled=True,whenreset_enabled=False,nextreset_enabled=False)
    config={'codex_announcements':cfg,'panel_display':{},'device_policy':{}}
    monkeypatch.setattr(c,'STATE_FILE',tmp_path/'source.json');monkeypatch.setattr(m,'STATE_FILE',tmp_path/'model.json')
    monkeypatch.setattr(c,'_config',lambda:config)
    desktop_service.gate.resume()
    return cfg,config,dict(account_present=True,plan='plus',plan_valid_until=at('2026-12-01T00:00'),remaining=50)

def test_daily_success_no_new_data_counts_and_does_not_poll_by_quota(env):
    cfg,config,context=env;calls=[]
    requester=lambda *args:(calls.append(args) or (204,{}))
    assert c.tick(now=at('2026-11-04T13:00'),config=config,context=context,requester=requester)['ok']
    assert not c.tick(now=at('2026-11-04T22:00'),config=config,context=dict(context,remaining=0),requester=requester)['queried']
    assert len(calls)==1
    assert c.tick(now=at('2026-11-05T06:00'),config=config,context=context,requester=requester)['ok']

def test_auth_does_not_retry_or_use_auxiliary_as_official(env):
    cfg,config,context=env;calls=[]
    def requester(*args):calls.append(args);raise c.SourceFailure('http_error',status=403)
    assert not c.tick(now=at('2026-11-04T13:00'),config=config,context=context,requester=requester)['ok']
    assert not c.tick(now=at('2026-11-06T13:00'),config=config,context=context,requester=requester)['queried']
    assert len(calls)==1

def test_retry_after_can_be_stricter_than_default(env):
    cfg,config,context=env;t=at('2026-11-04T13:00')
    def requester(*args):raise c.SourceFailure('http_error',status=429,headers={'Retry-After':'7200'})
    c.tick(now=t,config=config,context=context,requester=requester)
    assert c._state(cfg)['next_allowed_at']==t+7200

def test_model_failure_retries_queue_not_same_source(env,monkeypatch):
    cfg,config,context=env;cfg['analysis_enabled']=True;t=at('2026-11-04T13:00')
    p=post('2118000000000000001','Codex will get a reset tomorrow before 10am PST',t-60)
    source_calls=[];model_calls=[];monkeypatch.setattr(m,'_key',lambda:'isolated-placeholder')
    def requester(*args):source_calls.append(args);return 200,payload([p])
    def model(*args):
        model_calls.append(args)
        return {'usage':{'prompt_tokens':100,'completion_tokens':5,'total_tokens':105},'choices':[{'finish_reason':'stop','message':{'content':'' if len(model_calls)==1 else '{"events":[]}'}}]}
    first=c.tick(now=t,config=config,context=context,requester=requester,model_caller=model)
    assert first['ok'] and not first['analysis']['ok'] and c._state(cfg)['last_success_at']==t
    assert not c.tick(now=t+899,config=config,context=context,requester=requester,model_caller=model)['queried']
    c.tick(now=t+900,config=config,context=context,requester=requester,model_caller=model)
    assert len(source_calls)==1 and len(model_calls)==2 and m.snapshot()['pending']==0

def test_context_hash_dedup_and_input_envelope(env):
    cfg,_,_=env;t=at('2026-11-04T13:00');p=post('2118000000000000001','Codex will get a banked reset tomorrow',t-60)
    m.enqueue({p['id']:p},cfg,t);m.enqueue({p['id']:p},cfg,t+1)
    assert len(m._state()['queue'])==1
    p=dict(p,text=p['text']+' Update: two resets.')
    m.enqueue({p['id']:p},cfg,t+2)
    assert len(m._state()['queue'])==1
    assert any(v['reason']=='superseded_public_context' for v in m._state()['dispositions'].values())

def test_relevant_cancellation_reply_is_not_filtered_before_model(env):
    cfg,_,_=env;t=at('2026-11-04T13:00')
    root=post('2118000000000000001','Codex will get a reset tomorrow',t-60)
    reply=post('2118000000000000002','Cancelled.',t,parent=root['id'])
    m.enqueue({root['id']:root,reply['id']:reply},cfg,t)
    assert any(any(p['id']==reply['id'] for p in q['posts']) for q in m._state()['queue'])

@pytest.mark.parametrize('response',[None,{}, {'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[]},
 {'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'finish_reason':'length','message':{'content':'{}'}}]},
 {'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'finish_reason':'stop','message':{'content':'not json'}}]}])
def test_invalid_or_truncated_model_response_is_durable_backoff(env,monkeypatch,response):
    cfg,_,_=env;cfg['analysis_enabled']=True;t=at('2026-11-04T13:00')
    p=post('2118000000000000001','Codex will get a reset tomorrow',t-60)
    m.enqueue({p['id']:p},cfg,t);monkeypatch.setattr(m,'_key',lambda:'isolated-placeholder')
    r=m.process_one(cfg,t,caller=lambda *args:response)
    assert r['analyzed'] and not r['ok'] and m._state()['next_allowed_at']==t+900
    assert m.snapshot()['pending']==1
    assert not m.process_one(cfg,t+1,caller=lambda *args:pytest.fail('no immediate retry'))['analyzed']

def test_request_contains_only_public_text_and_limits(env,monkeypatch):
    cfg,_,_=env;cfg['analysis_enabled']=True;t=at('2026-11-04T13:00')
    p=post('2118000000000000001','Codex will get a reset tomorrow',t-60)
    m.enqueue({p['id']:p},cfg,t);monkeypatch.setattr(m,'_key',lambda:'isolated-placeholder')
    def call(messages,key):
        assert key=='isolated-placeholder'
        assert len(json.dumps(messages,ensure_ascii=False).encode())<6000
        assert 'public_posts' in messages[1]['content'] and 'account_balance' not in messages[1]['content']
        assert m.MAX_OUTPUT_TOKENS==1000
        return {'usage':{'prompt_tokens':100,'completion_tokens':10},'choices':[{'finish_reason':'stop','message':{'content':'{"events":[]}'}}]}
    assert m.process_one(cfg,t,caller=call)['ok']

def model_event(p,root=None,status='future'):
    root=root or p
    quote=p['text']
    return dict(post_id=p['id'],root_post_id=root['id'],kind='reset_grant',status=status,time_text=quote,
        count=None,plans=[],related_post_ids=[],stages=[],evidence={k:{'post_id':p['id'],'quote':quote} for k in ['kind','status','time']})

@pytest.mark.parametrize('text',['I demand a banked reset','Fellow resetters, our new model is live'])
def test_nonplans_never_promoted_by_model(text):
    t=at('2026-11-04T13:00');p=post('2118000000000000001',text,t)
    assert m.validate_output({'events':[model_event(p)]},[p],t)==[]

@pytest.mark.parametrize('text',['Codex reset lands tomorrow before 10am PST','Codex reset landing tomorrow before 10am PST'])
def test_future_literal_beats_bad_completion_label(text):
    t=at('2026-11-04T13:00');p=post('2118000000000000001',text,t)
    rows=m.validate_output({'events':[model_event(p,status='fulfilled')]},[p],t)
    assert rows[0]['status']=='pending' and rows[0]['target_at']>t

def test_evidence_and_scope_are_literal_not_model_invention():
    t=at('2026-11-04T13:00');p=post('2118000000000000001','Codex will get two banked resets tomorrow',t)
    event=model_event(p);event['count']=99;event['evidence']['count']={'post_id':p['id'],'quote':p['text']}
    with pytest.raises(ValueError):m.validate_output({'events':[event]},[p],t)
    event=model_event(p);event['plans']=['plus'];event['evidence']['plans']={'post_id':p['id'],'quote':p['text']}
    with pytest.raises(ValueError):m.validate_output({'events':[event]},[p],t)

def test_edited_source_invalidates_old_model_evidence():
    t=at('2026-11-04T13:00');p=post('2118000000000000001','Codex will get a reset tomorrow',t)
    row=m.validate_output({'events':[model_event(p)]},[p],t)[0]
    cfg=policy.DEFAULTS
    assert m.current_revision(row,{p['id']:p},cfg)
    assert not m.current_revision(row,{p['id']:dict(p,text='Cancelled')},cfg)

def test_model_cannot_borrow_root_or_deadline_from_neighboring_event():
    t=at('2026-11-04T13:00')
    first=post('2118000000000000001','Codex will get a reset tomorrow before10am PST',t-3600)
    second=post('2118000000000000002','Codex will get extra reset credits soon',t)
    event=model_event(second,root=first)
    with pytest.raises(ValueError,match='root_relation'):
        m.validate_output({'events':[event]},[first,second],t)
    event=model_event(second);event['time_text']=first['text'];event['evidence']['time']={'post_id':first['id'],'quote':first['text']}
    row=m.validate_output({'events':[event]},[first,second],t)[0]
    assert row['time_type']=='unknown' and row['target_at'] is None

def test_official_currency_prices_and_cache_hits(monkeypatch):
    from core import news_calendar
    monkeypatch.setattr(news_calendar,'calendar_status',lambda:{'holidays':[],'degraded':False})
    bill=m._cost({'prompt_tokens':100,'prompt_cache_hit_tokens':50,'completion_tokens':100},at('2026-11-04T15:00'))
    assert bill['cost_cny']==pytest.approx(0.000902)
    off=m._cost({'prompt_tokens':100,'prompt_cache_hit_tokens':50,'completion_tokens':100},at('2026-11-04T20:00'))
    assert off['cost_cny']==pytest.approx(bill['cost_cny']/2)

def test_definitively_rejected_call_does_not_suppress_external_balance(tmp_path,monkeypatch):
    from core import deepseek_activity as activity
    monkeypatch.setattr(activity,'_STATE_FILE',tmp_path/'activity.json')
    t=int(at('2026-11-04T15:00'))
    activity.observe_balances({'CNY':10,'USD':0},now=t)
    activity.reserve_automatic_call('announcements|test','call',0.03,now=t+1)
    activity.observe_balances({'CNY':9.98,'USD':0},now=t+2)
    assert activity.cancel_automatic_call('call',now=t+3)
    state=activity.state_store.read_json(activity._STATE_FILE)[0]
    assert state['effective']['CNY']==9.98 and not state['pending']

def test_banked_plan_and_progress_exact_public_regression():
    root_stamp=datetime(2026,10,31,5,42,26,tzinfo=timezone.utc).timestamp()
    anchor=post('2116000000000000001','Day 25/ Codex reset today',root_stamp-86400)
    first=post('2116405414921384099','Day 26/ You can now easily switch between multiple accounts on mobile, and coming soon to desktop too. Next two days will be resets… banked… do with this what you may 👀.',root_stamp)
    half=post('2116692529970311254','Banked 1/2',datetime(2026,11,1,0,43,20,tzinfo=timezone.utc).timestamp())
    last=post('2117035794678546764','Day 28/ Banked 2/2',datetime(2026,11,1,23,27,20,tzinfo=timezone.utc).timestamp())
    records,_=parser.series_state([anchor,first,half,last])
    r=codex_banked_rules.revisions([first],root_stamp+60,records)
    assert r[-1]['status']=='pending' and r[-1]['grant_count']==2
    r=codex_banked_rules.revisions([first,half],half['published_at']+60,records)
    assert r[-1]['status']=='pending' and r[-1]['grant_count']==1 and r[-1]['stages'][0]['status']=='fulfilled'
    r=codex_banked_rules.revisions([first,half,last],last['published_at']+60,records)
    assert r[-1]['status']=='fulfilled' and r[-1]['grant_count']==0
    assert not parser.select_events(r,now=last['published_at']+60,plan='plus')
    cancelled=post('2116600000000000001','Cancelled.',root_stamp+3600,parent=first['id'])
    r=codex_banked_rules.revisions([first,cancelled],root_stamp+4000,records)
    assert r[-1]['status']=='retracted' and not parser.select_events(r,now=root_stamp+4000,plan='plus')


def test_prune_provably_old_and_irrelevant_without_key_or_model(env,monkeypatch):
    cfg,_,_=env;t=at('2026-11-04T13:00')
    values=[post('2118000000000000001','Codex reset tomorrow before 10am PST',t-10*86400),
            post('2118000000000000002','I demand a banked reset',t-60),
            post('2118000000000000003','Fellow resetters, new frontier model coming tomorrow.',t-60),
            post('2118000000000000004','Codex next two days will be resets… banked…',t-3*86400)]
    monkeypatch.setattr(m,'_key',lambda:pytest.fail('local pruning must not read key'))
    m.enqueue({p['id']:p for p in values},cfg,t)
    assert not m._state()['queue']
    assert len(m._state()['dispositions'])==4
    assert not m.process_one(dict(cfg,analysis_enabled=True),t,caller=lambda *a:pytest.fail('no paid pruning'))['analyzed']


def test_prune_preserves_live_unknown_and_related_cancellation(env):
    cfg,_,_=env;t=at('2026-11-04T13:00')
    root=post('2118000000000000001','Codex will have banked resets. Timing to be announced.',t-60)
    reply=post('2118000000000000002','Cancelled.',t,parent=root['id'])
    m.enqueue({root['id']:root,reply['id']:reply},cfg,t)
    assert len(m._state()['queue'])==2


def test_repeatable_invalid_output_cannot_block_queue_forever(env,monkeypatch):
    cfg,_,_=env;cfg['analysis_enabled']=True;t=at('2026-11-04T13:00')
    p=post('2118000000000000001','Codex will get a reset tomorrow',t-60)
    m.enqueue({p['id']:p},cfg,t);monkeypatch.setattr(m,'_key',lambda:'isolated-placeholder')
    calls=[]
    def bad(*a):
        calls.append(1)
        return {'usage':{'prompt_tokens':10,'completion_tokens':1},'choices':[{'finish_reason':'stop','message':{'content':'bad json'}}]}
    for advance in (0,900,2700):m.process_one(cfg,t+advance,caller=bad)
    assert len(calls)==3 and not m._state()['queue']
    assert next(iter(m._state()['dispositions'].values()))['status']=='needs_review'
    m.enqueue({p['id']:p},cfg,t+3600)
    assert not m._state()['queue']
