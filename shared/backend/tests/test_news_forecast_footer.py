"""Real MiSans/C++ framebuffer, cache expiry and visual-only regressions.

All examples are synthetic and remain in tests; no USB/network/model calls.
"""
import copy,importlib.util,json,os,subprocess
from pathlib import Path
import pytest
from core import activity_signals as signals,codex_announcement_view as view
from news_footer_fixtures import NOW,announcement,document,cases
from test_codex_forecast_native_v2 import binaries,run
from test_published_preview import isolated
ROOT=Path(__file__).resolve().parents[3]


def alter(snapshot,**values):
    event=snapshot['selected_event'];event.update(values)
    snapshot[event['kind']]=copy.deepcopy(event)


@pytest.fixture(scope='module')
def graphics(tmp_path_factory):
    font_dir=os.environ.get('INKSIGHT_TEST_FONT_DIR');include=os.environ.get('INKSIGHT_TEST_ARDUINOJSON_INCLUDE')
    if not font_dir or not include:pytest.skip('local licensed fonts and public ArduinoJson headers required')
    spec=importlib.util.spec_from_file_location('footer_test_renderer',ROOT/'shared/tools/misans_panel_render.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);p.FONT_DIR=Path(font_dir)
    binary=tmp_path_factory.mktemp('native-footer')/'panel'
    subprocess.run(['c++','-std=c++17','-DENABLE_STRUCTURED=1','-DINKSIGHT_CONFIG_H','-DINKSIGHT_DISPLAY_H','-I'+str(ROOT/'shared/backend/tests/footer_host'),'-I'+include,'-I'+str(ROOT/'shared/firmware/src'),'-I'+str(Path(font_dir).parent),str(ROOT/'shared/backend/tests/news_footer_panel_host.cpp'),str(ROOT/'shared/firmware/src/panel_ui.cpp'),str(ROOT/'shared/firmware/src/panel_pages.cpp'),'-o',str(binary)],capture_output=True,check=True)
    return p,p.load_fonts(),binary


def paint(graphics,doc,clock,page=1):
    p,fonts,binary=graphics;panel=p.Panel();(p.render_news_gold_panel if page else p.render_ai_panel)(panel,fonts,doc,clock)
    raw=subprocess.run([str(binary)],input=json.dumps({'document':doc,'now':clock,'page':page}).encode(),capture_output=True,check=True,env={**os.environ,'TZ':'Asia/Shanghai'}).stdout
    assert raw==panel.fb
    assert not panel.missing
    return panel


@pytest.mark.parametrize('name,doc,clock',cases(),ids=[c[0] for c in cases()])
def test_actual_firmware_pixels_match_preview_and_restore_brand(graphics,name,doc,clock):
    panel=paint(graphics,doc,clock);plain=copy.deepcopy(doc);plain['screen']['pages']['ai'].pop('announcements',None);baseline=paint(graphics,plain,clock)
    assert panel.fb[:42800]==baseline.fb[:42800]
    footer=[o for o in panel.ops if o.get('baseline')==graphics[0].PUB_FOOT_Y];left,right=footer[-2:]
    assert right['ink'][0]-left['ink'][1]>=16
    assert right['ink'][1]==778
    assert left['font']==right['font']=='reg21'
    expected=view.news_footer_text(doc['screen']['pages']['ai'].get('announcements'),clock)
    assert right['text']==(expected or 'INKSIGHT')
    if not expected:assert panel.fb==baseline.fb


@pytest.mark.parametrize('name,doc,clock',cases(),ids=[c[0] for c in cases()])
def test_both_pages_and_real_swift_menu_use_same_selected_event(binaries,name,doc,clock):
    snapshot=doc['screen']['pages']['ai'].get('announcements');event=view.nearest_event(snapshot,clock)
    menu=run(binaries[0],{'now':clock,'announcements':snapshot})
    firmware=run(binaries[1],{'now':clock,'ai':{'announcements':snapshot},'inspect_selected':True,'inspect_footer':True})
    assert menu['enabled']==bool(event)
    assert firmware['selected']==event
    assert firmware['footer']==(view.news_footer_text(snapshot,clock) or '')
    if event:assert menu['event_id']==event['event_id']


@pytest.mark.parametrize('kind',['quota_reset','reset_grant','reset_unspecified'])
def test_forecasts_only_change_visual_keys_not_activity_or_observation(kind):
    ai=document()['screen']['pages']['ai'];plain=copy.deepcopy(ai);ai['announcements']=announcement(kind)
    assert signals.activity_key(ai)==signals.activity_key(plain)
    assert signals.activity_observed_at(ai)==signals.activity_observed_at(plain)
    assert signals.news_gold_visual_key(ai,{},now=NOW)!=signals.news_gold_visual_key(plain,{},now=NOW)
    key=signals.news_gold_visual_key(ai,{},now=NOW)
    ai['announcements']['last_attempt_at']=NOW+123;ai['announcements']['auxiliary_evidence']=[1,2,3]
    alter(ai['announcements'],revision='b'*24)
    assert signals.news_gold_visual_key(ai,{},now=NOW+1)==key
    alter(ai['announcements'],target_at=NOW+15*3600,expires_at=NOW+15*3600,valid_until=NOW+15*3600)
    assert signals.news_gold_visual_key(ai,{},now=NOW+1)!=key
    ai['codex_7d_used']+=1
    assert signals.activity_key(ai)!=signals.activity_key(plain)


@pytest.mark.parametrize('typ',['exact','unknown','date_range','cross_range'])
def test_cached_same_payload_expires_once_without_network_or_page_change(binaries,typ):
    snap=announcement(typ=typ);e=snap['selected_event'];ai={'announcements':snap}
    active=run(binaries[1],{'now':NOW,'ai':ai,'inspect_footer':True});key=active['display_key'];meta=f'2|same-font|same-payload.{key}|2026-10-29|数据正常'
    duplicate=run(binaries[1],{'now':NOW+1,'ai':ai,'inspect_footer':True,'meta':meta})
    assert not duplicate['changed']
    expired=run(binaries[1],{'now':e['valid_until'],'ai':ai,'inspect_footer':True,'meta':meta})
    assert expired['changed'] and expired['footer']=='' and expired['display_key']=='f0000000000000000'
    cleared=meta.replace(key,expired['display_key'])
    assert not run(binaries[1],{'now':e['valid_until']+1,'ai':ai,'inspect_footer':True,'meta':cleared})['changed']
    assert signals.news_gold_visual_key(ai,{},now=e['valid_until'])==signals.news_gold_visual_key({}, {},now=e['valid_until'])
    # Cold restart or invalid clock must not resurrect the old visible forecast.
    assert run(binaries[1],{'now':0,'ai':ai,'inspect_footer':True,'meta':meta})['changed']
    assert run(binaries[1],{'now':0,'ai':ai,'inspect_footer':True,'meta':'old-layout-meta','legacy_visible':True})['changed']


@pytest.mark.parametrize('status',['数据正常','部分数据陈旧','部分数据缺失','数据陈旧','离线','时间待校准','科技人工智能特别晚报暂未更新'])
@pytest.mark.parametrize('stamp',[NOW,NOW-86400,0])
def test_long_status_update_dates_and_unknown_times_keep_readable_font(graphics,status,stamp):
    p,fonts,_=graphics;panel=p.Panel();right=view.news_footer_text(announcement(typ='date_range'),NOW)
    left,right=p.news_footer_parts(panel,fonts,status,stamp,NOW,right)
    assert panel.ink_width(fonts['reg21'],left)+panel.ink_width(fonts['reg21'],right)+16<=754
    assert '更新' in left and ('--:--' in left if stamp==0 else '12:00' in left)
    if status.endswith('暂未更新'):assert '暂未更新' in left
    elif status=='部分数据陈旧':assert status in left


@pytest.mark.parametrize('key,bad',[('schema_version',True),('schema_version',99),('selected_event',[]),('enabled','true')])
def test_malformed_optional_snapshot_does_not_break_real_page(graphics,key,bad):
    doc=document();snap=announcement();snap[key]=bad;doc['screen']['pages']['ai']['announcements']=snap
    panel=paint(graphics,doc,NOW)
    assert panel.ops[-1]['text']=='INKSIGHT'


def test_legacy_v1_and_hidden_grant_safe_downgrade(graphics):
    doc=document();snap={'schema_version':1,'enabled':True,'reset_grant':dict(kind='reset_grant',status='pending',confidence='explicit',target_at=NOW+3600,expires_at=NOW+3600,valid_until=NOW+3600,published_at=NOW-60)}
    doc['screen']['pages']['ai']['announcements']=snap
    assert '前发放' in paint(graphics,doc,NOW).ops[-1]['text']
    doc['screen']['display_preferences']['show_reset_opportunities']=False
    assert paint(graphics,doc,NOW).ops[-1]['text']=='INKSIGHT'


def test_historical_preview_uses_document_clock_without_reinterpreting_now(monkeypatch,graphics,isolated):
    from core import local_console_preview as preview
    from test_published_preview import receipt,run as async_run
    p,fonts,_=graphics
    doc=document();doc['screen']['publish_id']='footer-history';doc['screen']['pages']['ai']['announcements']=announcement()
    isolated({'AABBCCDDEE01':receipt(doc)})
    monkeypatch.setattr(preview,'_renderer',lambda:p)
    monkeypatch.setattr(preview,'_fonts',lambda renderer:fonts)
    monkeypatch.setattr(preview.time,'time',lambda:NOW+86400)
    image,meta=async_run(preview.render(3,page='news_gold'))
    expected=paint(graphics,doc,doc['ts'])
    assert image.tobytes()==expected.fb and meta['render_at']==doc['ts'] and meta['stale']
    assert '前重置' in expected.ops[-1]['text']
    assert paint(graphics,doc,NOW+86400).ops[-1]['text']=='INKSIGHT'
