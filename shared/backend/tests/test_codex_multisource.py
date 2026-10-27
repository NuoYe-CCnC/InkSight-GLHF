"""Public evidence excerpts, isolated transports and compact layout contracts."""
import copy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path

import pytest
from core import codex_announcement_parser as parser
from core import codex_announcement_sources as sources
from core import codex_announcements as collector
from core import codex_announcement_view as view
from core import codex_announcement_schedule as policy
from core import activity_signals

AUTHOR='thsottiaux';AUTHOR_ID=policy.DEFAULTS['target_user_id']
NOW=1792748400


def normalized(text,ident,published,parent=None):
    return {'id':ident,'author':AUTHOR,'author_id':AUTHOR_ID,'text':text,
        'published_at':published,'parent_id':parent,'url':f'https://x.com/{AUTHOR}/status/{ident}'}


def real_excerpt_chain():
    # Focused verbatim excerpts from the already persisted verified public
    # posts; ellipses are explicit. Never copied into production source state.
    return [
      normalized('Day 18/ … Uber is going all-in on codex & work. But yes… that’s a reset!',
                 '2113492187770175603',1792730779),
      normalized('Landing tomorrow before 10am PST together with a small ship.',
                 '2113492502355562875',1792730854,'2113492187770175603'),
      normalized('Day 17/ … it’s reset day today.', '2113162277688070445',1792652122),
      normalized('Lands tomorrow before 10am PST.', '2113162413008884099',1792652154,'2113162277688070445')]


def test_real_day17_expired_day18_future_literal_pst_source_date():
    events,rejections=parser.parse_posts(real_excerpt_chain(),now=NOW)
    selected=parser.select_events(events,now=NOW,plan='plus')
    assert list(selected)==['reset_unspecified']
    event=selected['reset_unspecified']
    assert event['post_id']=='2113492502355562875'
    assert datetime.fromtimestamp(event['target_at'],timezone(timedelta(hours=8))).isoformat()=='2026-10-24T02:00:00+08:00'
    assert event['plan_scope']=='unspecified' and event['source_time_zone']=='PST'
    assert event['time_relation']=='before'
    pdt=parser.explicit_deadline('Landing tomorrow before 10am PDT.',1792730854)[0]
    assert pdt==event['target_at']-3600
    assert any(row['post_id']=='2113162413008884099' and row['status']=='expired' for row in events)
    assert parser.select_events(events,now=event['target_at'],plan='plus')=={}


def test_explicit_unspecified_reset_without_series_and_paid_scope():
    post=normalized('Codex reset will land tomorrow before 10am PST.','2113492502355562801',1792730854)
    selected=parser.select_events(parser.parse_posts([post],now=NOW)[0],now=NOW,plan=None)
    assert selected['reset_unspecified']['plan_scope']=='unspecified'
    assert 'free' not in parser._plans('Paid Codex accounts')
    assert 'plus' in parser._plans('Paid Codex accounts')


def test_standalone_reset_joke_and_unrelated_shipping_do_not_supply_event():
    joke=normalized('Haha Codex, that’s a reset! Landing tomorrow before 10am PST.', '2113492502355562800',1792730854)
    assert not parser.select_events(parser.parse_posts([joke],now=NOW)[0],now=NOW,plan='plus')
    rows=real_excerpt_chain()[:2]
    rows[0]['text']='Day 18/ We shipped performance improvements for another product. Codex & work. But yes… that’s a reset!'
    assert parser.select_events(parser.parse_posts(rows,now=NOW)[0],now=NOW,plan='plus')


def test_explicit_scope_exclusion_and_same_event_latest_correction_retraction():
    rows=real_excerpt_chain()[:2]
    correction=normalized('Correction: Codex Pro users will reset weekly quota tomorrow before 11am PST.',
        '2113492502355562876',1792730900,rows[-1]['id'])
    revised=parser.parse_posts(rows+[correction],now=NOW)[0]
    assert not parser.select_events(revised,now=NOW,plan='plus')
    assert list(parser.select_events(revised,now=NOW,plan='pro'))==['quota_reset']
    cancel=normalized('Cancelled. Not happening.','2113492502355562877',1792731000,correction['id'])
    assert not parser.select_events(parser.parse_posts(rows+[correction,cancel],now=NOW)[0],now=NOW,plan='pro')


def test_third_party_only_yields_deduped_trusted_links_never_dates_or_forecasts():
    text='''<h2>Historical case studies, not a live feed</h2><p>Predicted reset today 1am PDT</p>
      <a href="https://x.com/thsottiaux/status/2113492502355562875">Source</a>
      <a href="https://x.com/thsottiaux/status/2113492502355562875">Duplicate</a>
      <a href="https://x.com/other/status/2113492502355562876">Other</a>
      <a href="https://127.0.0.1/thsottiaux/status/2113492502355562877">SSRF</a>
      <script><a href="https://x.com/thsottiaux/status/2113492502355562878">Hidden</a></script>'''
    found=sources.extract(text,AUTHOR,8)
    assert found['post_ids']==['2113492502355562875']
    assert found['historical_case_studies'] and not found['authoritative']
    assert 'target_at' not in found and not found['stable_machine_api']


def test_auxiliary_independent_cooldown_failure_preserves_evidence_and_rate_limit():
    cfg={**policy.DEFAULTS,'whenreset_enabled':True,'nextreset_enabled':True}
    calls=[]
    def request(name,path,timeout):
        calls.append((name,path))
        if path=='/robots.txt':return 'User-agent: *\nAllow: /'
        return '<a href="https://x.com/thsottiaux/status/2113492502355562875">Source</a>'
    first=sources.collect({},cfg,NOW,requester=request)
    assert len(calls)==4 and all(row['state']=='current' for row in first.values())
    assert sources.collect(first,cfg,NOW+3600,requester=lambda *a:pytest.fail('too frequent'))==first
    def limited(*args):raise sources.DiscoveryError('http_error',429,14400)
    failure=sources.collect(first,cfg,NOW+7200,requester=limited)
    assert all(row['post_ids']==first[name]['post_ids'] for name,row in failure.items())
    assert all(row['next_allowed_at']>=NOW+21600 for row in failure.values())


def test_robots_failure_or_disallow_never_reads_html():
    calls=[]
    def deny(name,path,timeout):
        calls.append(path);return 'User-agent: *\nDisallow: /'
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    result=sources.collect({},cfg,NOW,requester=deny)
    assert calls==['/robots.txt'] and result['whenreset']['state']=='robots_disallowed'
    def failed(*args):raise sources.DiscoveryError('network_or_decode')
    assert sources.collect({},cfg,NOW,requester=failed)['whenreset']['state']=='network_or_decode'


def test_nearest_event_zero_fallback_and_timestamps_do_not_change_activity():
    def event(kind,target):return dict(kind=kind,target_at=target,expires_at=target,valid_until=target,
        published_at=NOW-60,status='pending',confidence='explicit',time_relation='before')
    snapshot={'schema_version':1,'enabled':True,'quota_reset':event('quota_reset',NOW+7200),
              'reset_grant':event('reset_grant',NOW+3600)}
    assert view.nearest_event(snapshot,NOW)['kind']=='reset_grant'
    assert view.nearest_event(snapshot,NOW,show_reset=False)['kind']=='quota_reset'
    ai={'announcements':snapshot,'reset_credits_available':0,'reset_credits_confirmed':True}
    old=copy.deepcopy(ai);key=activity_signals.ai_visual_key(ai,now=NOW)
    ai['announcements']['last_polled_at']=NOW+100
    assert activity_signals.ai_visual_key(ai,now=NOW)==key
    assert activity_signals.activity_key(ai)==activity_signals.activity_key(old)


def test_auxiliary_discovery_returns_to_primary_verification_and_no_call_amplification():
    cfg={**policy.DEFAULTS,'whenreset_enabled':True}
    rows=real_excerpt_chain()[:2]
    raw=[]
    for post in rows:
        raw.append({'type':'status','id':post['id'],'text':post['text'],
          'created_timestamp':post['published_at'],'url':post['url'],
          'author':{'screen_name':AUTHOR,'id':AUTHOR_ID},
          'replying_to':{'screen_name':AUTHOR,'status':post['parent_id']} if post['parent_id'] else None})
    def auxiliary(name,path,timeout):
        return 'User-agent: *\nAllow: /' if path=='/robots.txt' else f'<a href="{rows[1]["url"]}">Original</a>'
    calls=[]
    def primary(path,query,timeout):
        calls.append(path)
        if '/profile/' in path:return 200,{'code':200,'results':[]}
        ident=path.rsplit('/',1)[1]
        return 200,{'code':200,'status':next(post for post in raw if post['id']==ident)}
    state=collector._default(cfg)
    collected=collector._collect(state,cfg,NOW,primary,{'plan':'plus'},auxiliary)
    assert collected['requests']<=4 and len(calls)==3
    revisions,_=parser.parse_posts(list(collected['posts'].values()),now=NOW)
    assert parser.select_events(revisions,now=NOW,plan='plus')['reset_unspecified']['post_id']==rows[1]['id']
    state.update(collected)
    again=collector._collect(state,cfg,NOW+60,primary,{'plan':'plus'},lambda *a:pytest.fail('auxiliary queried every primary poll'))
    assert again['requests']<=4


def test_verified_terminal_revision_survives_other_source_cached_success():
    cfg=policy.DEFAULTS
    posts=real_excerpt_chain()[:2]
    done=normalized('Done. Completed.', '2113492502355562876',1792731000,posts[1]['id'])
    revisions,_=parser.parse_posts(posts+[done],now=NOW)
    terminal=collector._retirements({},revisions,NOW,cfg)
    state=collector._default(cfg);state.update(posts={row['id']:row for row in posts},terminal_revisions=terminal)
    context={'account_present':True,'plan':'plus'}
    assert collector._events(state,cfg,NOW,context)[0]=={}


def test_native_nearest_unknown_and_expiry(tmp_path):
    import os,subprocess
    include=os.environ.get('INKSIGHT_TEST_ARDUINOJSON_INCLUDE')
    if not include:pytest.skip('private local ArduinoJson required')
    root=Path(__file__).resolve().parents[3]
    target=tmp_path/'native-nearest'
    subprocess.run(['c++','-std=c++17','-I'+include,'-I'+str(root/'shared/firmware/src'),
        str(root/'shared/backend/tests/codex_announcement_host.cpp'),'-o',str(target)],check=True,capture_output=True)
    event={'kind':'reset_unspecified','status':'pending','confidence':'explicit','published_at':NOW-60,
           'target_at':NOW+3600,'valid_until':NOW+3600,'expires_at':NOW+3600}
    snap={'schema_version':1,'enabled':True,'reset_unspecified':event}
    for clock in [NOW,NOW+3600]:
        result=json.loads(subprocess.run([str(target)],input=json.dumps({'now':clock,'inspect_nearest':True,'ai':{'announcements':snap}}),
            text=True,capture_output=True,check=True).stdout)
        assert result['nearest']==(NOW+3600 if clock==NOW else 0)
        assert result['kind']==('reset_unspecified' if clock==NOW else '')
