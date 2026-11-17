"""Typed subscription flow, semantic activity and real firmware framebuffer parity."""
import copy
import pytest
from core import membership as m, activity_signals as sig, reliable_sources as rs
from test_news_forecast_footer import graphics, paint
from news_footer_fixtures import NOW, document


def state(**changes):
    value=dict(record=dict(product='ChatGPT Plus', display_date='11月30日', action_word='续期',
        displayed_state='renewing', source='Apple 官方订阅页', state_wording='11月30日续期',
        schedule_anchor={'date':'2026-11-30','official_year':False}),
        last_success_at=NOW, last_result='success')
    value.update(changes)
    return value


def ai(value=None, **changes):
    member=m.effective_member({'plan':'PLUS','valid_until_date':'2026-10-28'},enabled=True,state=value or state(),now=NOW)
    result=document()['screen']['pages']['ai']
    result.update(plan=member['plan'],valid_until_date=member['valid_until_date'],member=member,membership=member['subscription'])
    result.update(changes)
    return result


def test_authoritative_projection_preserves_source_precision_and_manual_plan():
    value=m.effective_member({'plan':'PRO','valid_until_date':'2026-10-28'},enabled=True,state=state(),now=NOW)
    assert value['plan']=='PRO' and value['valid_until_date'] is None
    info=value['subscription']
    assert info['date']=='11-30' and info['precision']=='month_day'
    assert '2026' not in info['date'] and 'schedule_anchor' not in info
    assert m.line(ai())=='PLUS · 续期 11-30'


def test_disabled_keeps_legacy_and_failed_read_keeps_last_apple():
    old={'plan':'PLUS','valid_until_date':'2026-10-28'}
    assert m.effective_member(old,enabled=False,state=state())==old
    value=ai(state(last_result='failed',retry_pending=True))
    assert value['valid_until_date'] is None and m.line(value)=='PLUS · 续期 11-30'
    assert value['membership']['read_status']=='failed' and m.problem(value)
    assert m.line(ai({'last_result':'failed'}))=='PLUS · 待确认'


def test_cancelled_valid_until_and_explicit_expired():
    s=state();s['record'].update(action_word='到期',displayed_state='cancelled')
    assert m.line(ai(s))=='PLUS · 有效至 11-30'
    s['record']['displayed_state']='expired'
    assert m.line(ai(s))=='PLUS · 已到期 11-30'
    s['record']['display_date']='2026年11月30日'
    member=m.effective_member({},enabled=True,state=s,now=NOW)
    assert member['valid_until_date']=='2026-11-30'


def test_visible_version_excludes_read_times_and_activity_excludes_dates():
    a=ai();b=copy.deepcopy(a);b['membership']['last_success_at']=NOW+60
    b['member']['subscription']=b['membership']
    assert sig.ai_visual_key(a)==sig.ai_visual_key(b)
    assert m.visual_member(a['member'])==m.visual_member(b['member'])
    b['membership']['date']='12-30';b['membership']['raw_date_text']='12月30日'
    b['valid_until_date']='2026-12-30';b['member']['valid_until_date']='2026-12-30'
    assert sig.ai_visual_key(a)!=sig.ai_visual_key(b)
    assert sig.activity_key(a)==sig.activity_key(b) and sig.activity_key(a)
    b['codex_7d_used']+=1
    assert sig.activity_key(a)!=sig.activity_key(b)


def test_cached_member_reprojects_live_apple_without_manual_cache_reset(monkeypatch):
    monkeypatch.setattr(rs,'_member_cache',{'plan':'PLUS','valid_until_date':'2026-10-28'})
    monkeypatch.setattr('core.operator_config.load_effective',lambda:type('C',(),{'config':{'apple_subscription':{'enabled':True}}})())
    s=state();monkeypatch.setattr('core.apple_subscription._load',lambda:s)
    assert rs.member_config()['subscription']['date']=='11-30'
    s['record']['display_date']='12月30日'
    assert rs.member_config()['subscription']['date']=='12-30'


@pytest.mark.parametrize('kind',['renewal','valid_until','expired','unknown','failed','old','cross_year','invalid','long_plan'])
def test_actual_firmware_and_preview_date_line_stay_in_original_bounds(graphics,kind):
    value=ai()
    if kind=='valid_until':value['membership'].update(date_type='valid_until',state='valid_until')
    if kind=='expired':value['membership']['state']='expired'
    if kind=='unknown':value['membership'].update(date=None,state='unknown',date_type='unknown')
    if kind=='failed':value['membership'].update(needs_confirmation=True,read_status='failed')
    if kind=='old':value.pop('membership');value['valid_until_date']='2026-10-28'
    if kind=='cross_year':value['membership'].update(date='01-02',raw_date_text='1月2日')
    if kind=='invalid':value['membership']['date']='02-31'
    if kind=='long_plan':value['plan']='ENTERPRISE BUSINESS PREMIUM CUSTOM VERY LONG PLAN'
    doc=document();doc['screen']['pages']['ai']=value
    p=paint(graphics,doc,NOW,page=0)
    row=next(o for o in p.ops if ' · ' in o['text'] and o.get('baseline')==93)
    assert row['text']==m.line(value)
    codex=next(o for o in p.ops if o['text']=='CODEX')
    assert row['ink'][1]==461 and row['ink'][0]>=codex['ink'][1]+16
    assert row['font'] in {'reg21','reg16'}
    # Membership changes must not add a row to the news page or move its content.
    before=paint(graphics,document(),NOW,page=1)
    after=paint(graphics,doc,NOW,page=1)
    assert before.fb[:42800]==after.fb[:42800]
