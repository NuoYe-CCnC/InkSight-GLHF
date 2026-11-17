"""Offline real send-boundary and publication-gate replay; no external calls."""
import copy,datetime as dt,json,urllib.error
from pathlib import Path
import pytest
from core import news_digest as d,news_validate as v,news_schedule as s,news_brief as b,news_evidence as e,operator_config as c

T=int(dt.datetime(2026,11,5,8,55,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
GOOD='OpenAI 发布 GPT-6 编程模型；NVIDIA 推出新款 GPU 芯片，支持本地计算。'
BAD='OpenAI announced a new model，支持编程；NVIDIA 推出新款 GPU 芯片，支持本地计算。'

@pytest.fixture
def env(tmp_path,monkeypatch):
    s.configure_state_file(tmp_path/'schedule.json')
    monkeypatch.setattr(b,'_STATE_FILE',tmp_path/'brief.json')
    # The module's configured path is bound by its real state helper.
    b.configure_state_file(tmp_path/'brief.json')
    monkeypatch.setattr(d.time,'time',lambda:T)
    monkeypatch.setattr(s,'credential_state',lambda:{'state':'active'})
    monkeypatch.setattr(s,'_collect_if_due',lambda *a:None)
    monkeypatch.setattr(b,'_env_key',lambda:'offline-test-key')
    monkeypatch.setattr(b,'reserve_usage_activity',lambda *a:None)
    monkeypatch.setattr(b,'settle_usage',lambda *a:None)
    monkeypatch.setattr(s.budget,'reserve',lambda *a,**k:None)
    monkeypatch.setattr(s.budget,'settle',lambda *a,**k:None)
    fonts=Path.home()/'Library/Application Support/InkSight-GLHF/versions/v0.1.0-test.21/source/shared/firmware/src/fonts_gen'
    monkeypatch.setattr(v,'_FONT21',fonts/'misans_reg_21.h');monkeypatch.setattr(v,'_font',None)
    rows=[dict(article_id='a',source='ars',title='OpenAI released GPT-6 coding model',summary='OpenAI released GPT-6 coding model.',published_at=T-1800,url='https://example.org/a'),dict(article_id='b',source='ithome',title='NVIDIA 推出新款 GPU 芯片，支持本地计算',summary='NVIDIA 推出新款 GPU 芯片，支持本地计算。',published_at=T-1200,url='https://example.org/b')]
    monkeypatch.setattr(b,'filter_candidates',lambda x,*a:x)
    monkeypatch.setattr(b,'build_candidates',lambda **k:copy.deepcopy(rows))
    cfg=copy.deepcopy(c.DEFAULT_CONFIG['news_digest']);cfg.update(max_calls_per_issue=3,sources=['ars','ithome'],source_enabled={'ars':True,'ithome':True})
    now=dt.datetime.fromtimestamp(T,dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    return rows,cfg,now


def response(body=GOOD,quote=None):
    return {'body':body,'events':[dict(candidate_id='1',evidence_ids=['E1T'],claim=body.split('；')[0]),dict(candidate_id='2',evidence_ids=['E2T'],claim=body.split('；')[1].rstrip('。'))]}


def transport(monkeypatch,outputs):
    calls=[]
    class R:
        def __init__(self,data):self.data=data
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):return json.dumps(self.data,ensure_ascii=False).encode()
    def send(req,**kwargs):
        calls.append(json.loads(req.data));value=outputs[min(len(calls)-1,len(outputs)-1)]
        if isinstance(value,Exception):raise value
        if isinstance(value,str):content=value;finish='stop'
        elif value.get('_finish'):content='';finish=value['_finish']
        else:content=json.dumps(value,ensure_ascii=False);finish='stop'
        return R({'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150},'choices':[{'finish_reason':finish,'message':{'content':content}}]})
    monkeypatch.setattr(d.urllib.request,'urlopen',send)
    return calls


def run(env):
    rows,cfg,now=env;state=s._load()
    with s.state_store.file_lock(s._STATE,operation='schedule'):
        return s._run_one(state,rows,'morning',now,now,dict(label='早报'),cfg,'2026-11-05|morning')


@pytest.mark.parametrize('text',['AI for Science 推动科研工具升级','DeepSeek 发布模型，支持 API 和 GPU 计算','OceanBase 推出新数据库功能','Apple Vision Pro 支持空间应用','API v2.0 支持 GPU 数据处理','University of the Arts 推出科研课程','New York Times 发布科技报道'])
def test_technical_names_are_not_prose(text):assert v.validate_visible_chinese(text)['ok']

@pytest.mark.parametrize('text',[BAD,'OpenAI Announces a New Model，支持科研工作','The Future of AI 发布了新内容','OpenAI released a new model today'])
def test_english_narrative_and_headlines_are_not_entity_whitelisted(text):assert not v.validate_visible_chinese(text)['ok']


def test_scoped_language_repair_and_original_language_evidence(env,monkeypatch):
    calls=transport(monkeypatch,[response(BAD),response(GOOD.replace('GPT-6 编程模型','新模型，支持编程'))])
    assert run(env)=='published'
    assert len(calls)==2
    prompt=calls[1]['messages'][1]['content'];assert 'announced a new model' in prompt and 'previous_body' in prompt
    state=s._load();assert state['model_calls']['2026-11-05|morning']==2 and state['rejections'][0]['fragments']
    current=b._load_state()['current'];assert 'announced' not in current['text'] and 'OpenAI released' in current['events'][0]['evidence']
    assert 'offline-test-key' not in json.dumps(state)


def test_third_failure_restart_and_repeated_request_share_send_count(env,monkeypatch):
    calls=transport(monkeypatch,['bad json',{'_finish':'length'},'not json'])
    old=dict(mode='digest',text='科技行业持续发展，产品功能更新。',date='2026-11-04',period='evening',issue_title='晚报')
    b._save_state({'current':old})
    run(env);run(env)
    assert len(calls)==3 and s._load()['model_calls']['2026-11-05|morning']==3
    assert b._load_state()['current']['date']=='2026-11-04'
    assert any(r.get('fallback') for r in s._load()['rejections'])


def test_invalid_reference_is_separate_from_body_language(env,monkeypatch):
    bad=response();bad['events'][0]['evidence_ids']=['E2T']
    calls=transport(monkeypatch,[bad,response()]);assert run(env)=='published' and len(calls)==2
    rejection=s._load()['rejections'][0];assert 'evidence-id-invalid' in rejection['rules'] and not rejection['fragments']


def test_english_evidence_normalization_and_translated_quote_rejection():
    candidate={'title':'OpenAI’s GPU model is released.','summary':''}
    ev={'evidence':"OpenAI's GPU model is released.",'claim':'OpenAI 发布 GPU 模型'}
    assert e.check(ev['claim'],candidate,ev,{},1)[1] is None
    ev['evidence']='OpenAI 发布 GPU 模型'
    assert e.check(ev['claim'],candidate,ev,{},1)[1]=='evidence-not-in-source'
    assert e.check('OpenAI 发布 5 款模型',candidate,dict(evidence=candidate['title']),{},1)[1]=='claim-number-not-supported'


def test_fallback_filters_chinese_material_before_ranking(env,monkeypatch):
    rows,_,_=env
    chinese=copy.deepcopy(rows[0]);chinese.update(article_id='c',source='qbitai',title='OpenAI 发布 GPT-6 编程模型，支持代码生成')
    more=[dict(rows[0],article_id=str(i)) for i in range(4)]+[rows[1],chinese]
    detail={};value=d.build_local_fallback(more,diagnostics=detail)
    assert value and value['origin']=='local-fallback' and detail['ok']
    assert not d.build_local_fallback([dict(r,published_at=T-10*86400) for r in more])


def test_manual_same_operation_does_not_generate_again(env,monkeypatch):
    rows,cfg,now=env;calls=transport(monkeypatch,[response()])
    assert s.run_manual_issue('manual|same-click',cfg,now_dt=now)=='published'
    assert s.run_manual_issue('manual|same-click',cfg,now_dt=now)=='published'
    assert len(calls)==1


def test_caps_defaults_legacy_and_explicit_lower_limits():
    assert s._max_model_calls({})==3
    assert s._max_model_calls({'max_calls_per_issue':5})==3
    assert s._max_model_calls({'max_calls_per_issue':2})==2
    assert c.DEFAULT_CONFIG['news_digest']['max_calls_per_issue']==3
    assert c.DEFAULT_CONFIG['reserved']['news']['max_calls_per_issue']==3


def test_timeout_consumes_send_budget_and_no_network_preflight_does_not(env,monkeypatch):
    calls=transport(monkeypatch,[TimeoutError('offline timeout')]);run(env)
    assert len(calls)==1 and s._load()['model_calls']['2026-11-05|morning']==1
    env[1]['max_input_tokens']=1
    with pytest.raises(ValueError):run(env)
    assert len(calls)==1 and s._load()['model_calls']['2026-11-05|morning']==1


def test_local_fallback_is_validated_and_published_with_no_send(env,monkeypatch):
    rows,cfg,now=env;rows[0]['title']='OpenAI 发布 GPT-6 编程模型，支持代码生成'
    state=s._load();state['model_calls']={'2026-11-05|morning':3};s._save(state)
    calls=transport(monkeypatch,[AssertionError('no model permitted')])
    assert run(env)=='published' and not calls
    assert b._load_state()['current']['origin']=='local-fallback'
    assert s._load()['model_calls']['2026-11-05|morning']==3


def test_original_quote_must_support_entity_numbers_and_future_qualification():
    cnd={'title':'OpenAI plans to release GPT-6 with 5 tools.','summary':''}
    for claim in ['OpenAI 已发布 GPT-6，提供5个工具','OpenAI 计划发布 GPT-9，提供5个工具','OpenAI 计划发布 GPT-6，提供8个工具']:
        assert e.check(claim,cnd,dict(evidence=cnd['title'],claim=claim),{},1)[1]
    assert e.check('OpenAI 计划发布 GPT-6，提供5个工具',cnd,dict(evidence=cnd['title']),{},1)[1] is None


def test_concurrent_manual_delivery_uses_one_durable_operation(env,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _,cfg,now=env;calls=transport(monkeypatch,[response()])
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:s.run_manual_issue('manual|concurrent-click',cfg,now_dt=now),range(2)))
    assert outcomes==['published','published'] and len(calls)==1


def test_reference_claim_cannot_cover_only_subject_and_leave_facts_unchecked(env,monkeypatch):
    invalid=response();invalid['events'][0]['claim']='OpenAI'
    calls=transport(monkeypatch,[invalid,response()])
    assert run(env)=='published' and len(calls)==2
    assert 'claim-coverage-incomplete' in s._load()['rejections'][0]['rules']


def test_duplicate_failed_manual_post_requires_explicit_resume(monkeypatch):
    import asyncio
    from api.routes import local_console
    task={'id':'issue-offline','status':'failed'}
    monkeypatch.setattr(local_console.manual_issue,'create',lambda *a,**k:(task,False))
    monkeypatch.setattr(local_console.manual_issue,'start',lambda *a:pytest.fail('duplicate POST must not resume failed operation'))
    result=asyncio.run(local_console.manual_issue_create({'idempotency_key':'same-click'},_user_id=1))
    assert result.status_code==200 and json.loads(result.body)['created'] is False
