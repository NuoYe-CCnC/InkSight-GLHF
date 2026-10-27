"""Publication-source, no-side-effect and pixel contracts. No production writes."""
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import local_console_preview as preview


def run(coro):
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = asyncio.new_event_loop()
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(previous)


def document(stamp=1792746802):
    return {'payload_type': 'screen', 'ts': stamp, 'screen': {
        'publish_id': 'offline-contract', 'calendar': {'preserve': ['all', 'fields']},
        'display_preferences': {'show_codex_credits': True},
        'device_policy': {'page_switch': {'default_page': 'ai'}},
        'pages': {'ai': {'codex_7d_used': 34, 'announcements': {'enabled': False},
                         'codex_credit_balance': 12.3, 'deepseek_today_tokens': 59234},
                  'news_gold': {'news': {'schedule': {'preserve': True}},
                                'gold': {'data_state': {'preserve': True}}}}}}


def receipt(doc):
    return {'last_success_at': doc['ts'], 'publish_id': doc['screen']['publish_id'],
            'document': copy.deepcopy(doc), 'document_sha256': hashlib.sha256(
                json.dumps(doc, ensure_ascii=False).encode()).hexdigest()}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = tmp_path/'publication.json'
    monkeypatch.setattr(preview, 'STATE_FILE', path)
    async def devices(_user):
        return [{'mac':'AABBCCDDEE01','nickname':'面板'},
                {'mac':'AABBCCDDEE02','nickname':'test'}]
    monkeypatch.setattr(preview, '_operator_devices', devices)
    def save(rows):
        path.write_text(json.dumps({'_desktop_publications':rows},ensure_ascii=False))
    save({})
    return save


def test_default_is_published_device_not_first_binding_and_full_document_preserved(isolated):
    doc = document()
    isolated({'AABBCCDDEE02': receipt(doc)})
    rows = run(preview.list_devices(3))
    assert rows[0]['label'] == 'test' and rows[0]['has_snapshot']
    assert rows[1]['state'] == 'unpublished'
    assert 'AABBCC' not in json.dumps(rows)
    read, meta = run(preview.build_document(3))
    assert read == doc and meta['render_at'] == doc['ts']
    assert not meta['device_display_confirmed'] and meta['current_page'] is None
    read['screen']['calendar'].clear()
    assert run(preview.build_document(3))[0] == doc


def test_unknown_and_other_user_device_are_not_fallback(isolated):
    isolated({'AABBCCDDEE01': receipt(document())})
    with pytest.raises(preview.PreviewError, match='失效'):
        run(preview.build_document(3, preview._device_id(9,'AABBCCDDEE01')))
    with pytest.raises(preview.PreviewError, match='没有成功发布'):
        run(preview.build_document(3, preview._device_id(3,'AABBCCDDEE02')))


def test_unbound_and_legacy_receipt_have_accurate_empty_states(isolated, monkeypatch):
    isolated({'AABBCCDDEE01':{'last_success_at':1792746802,'publish_id':'legacy'}})
    with pytest.raises(preview.PreviewError) as error:
        run(preview.build_document(3))
    assert error.value.code == 'snapshot_missing'
    async def empty(_user): return []
    monkeypatch.setattr(preview,'_operator_devices',empty)
    with pytest.raises(preview.PreviewError) as error:
        run(preview.build_document(3))
    assert error.value.code == 'no_devices'


@pytest.mark.parametrize('change', ['hash','timestamp','publish_id','pages'])
def test_corrupt_receipt_does_not_become_success(isolated,change):
    row = receipt(document())
    if change == 'hash': row['document_sha256']='invalid'
    if change == 'timestamp': row['last_success_at']+=1
    if change == 'publish_id': row['publish_id']='other'
    if change == 'pages': row['document']['screen']['pages']={}
    isolated({'AABBCCDDEE01':row})
    with pytest.raises(preview.PreviewError) as error:
        run(preview.build_document(3))
    assert error.value.code == 'snapshot_invalid'


def test_font_failure_is_actionable_and_not_generic_500(tmp_path):
    with pytest.raises(preview.PreviewError) as error:
        preview._fonts(SimpleNamespace(FONT_DIR=tmp_path))
    assert error.value.code=='fonts_missing' and '不会自动下载' in str(error.value)


@pytest.mark.parametrize('succeed,dry_run', [(True,False),(False,False),(True,True)])
def test_publisher_receipt_advances_only_verified_real_success(tmp_path,monkeypatch,succeed,dry_run):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
    import cloud_publish as publisher
    old=receipt(document(1792746000)); state={'_desktop_publications':{'AABBCCDDEE01':old}}
    initial=copy.deepcopy(state)
    doc=document();doc['screen']['modules']=[];doc['screen']['api_key']='never persist'
    monkeypatch.setattr(publisher,'load_state',lambda:state)
    saved=[]
    monkeypatch.setattr(publisher,'save_state',lambda value:saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(publisher,'fetch_payload',lambda *args,**kw:{})
    monkeypatch.setattr(publisher,'combine_dashboard',lambda rows:doc)
    monkeypatch.setattr(publisher,'publish_one',lambda *args,**kw:succeed)
    monkeypatch.setattr(publisher.time,'time',lambda:1792746802)
    publisher._run_unlocked({'backend':'http://127.0.0.1:18181','webdav':'https://example.invalid',
                            'devices':[{'mac':'AABBCCDDEE01','modes':['AI_USAGE']}]},dry_run=dry_run)
    if succeed and not dry_run:
        row=state['_desktop_publications']['AABBCCDDEE01']
        assert row['last_success_at']==1792746802
        assert 'api_key' not in row['document']['screen']
        assert preview._document(row)==row['document']
    else:
        assert state==initial
        if dry_run: assert not saved


@pytest.mark.parametrize('page',['ai','news_gold'])
@pytest.mark.parametrize('forecasts',[False,True])
def test_real_persisted_snapshot_pixel_equal_to_installed_baseline(isolated,monkeypatch,page,forecasts):
    snapshot=os.environ.get('INKSIGHT_PREVIEW_TEST_DOCUMENT')
    baseline=os.environ.get('INKSIGHT_PREVIEW_BASELINE_RENDERER')
    font_dir=os.environ.get('INKSIGHT_TEST_FONT_DIR')
    if not all([snapshot,baseline,font_dir]):
        pytest.skip('private production snapshot/fonts are deliberately not distributed')
    doc=json.loads(Path(snapshot).read_text())
    # Optional fixture variants stay only in the isolated temporary test state.
    if forecasts:
        doc['screen']['pages']['ai']['announcements']={'schema_version':1,'enabled':True}
        for kind, seconds in [('quota_reset',3600),('reset_grant',1800)]:
            doc['screen']['pages']['ai']['announcements'][kind]={
                'kind':kind,'status':'pending','confidence':'explicit',
                'target_at':doc['ts']+seconds,'expires_at':doc['ts']+seconds,
                'valid_until':doc['ts']+seconds,'published_at':doc['ts']-60}
    isolated({'AABBCCDDEE01':receipt(doc)})
    # Active forecast geometry was explicitly revised in test.12; only the
    # no-forecast state must retain the previous installed pixels unchanged.
    baseline_path=Path(preview.__file__).resolve().parents[2]/'tools/misans_panel_render.py' if forecasts else Path(baseline)
    spec=importlib.util.spec_from_file_location('baseline_preview_renderer',baseline_path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.FONT_DIR=Path(font_dir)
    if forecasts:
        assert module.announcement_target(doc['screen']['pages']['ai'],'quota_reset',doc['ts'])>0
    renderer=preview._renderer()
    monkeypatch.setattr(renderer,'FONT_DIR',Path(font_dir))
    monkeypatch.setattr(preview.time,'time',lambda:doc['ts']+172800)
    image,meta=run(preview.render(3,page=page))
    panel=module.Panel();fonts=module.load_fonts()
    (module.render_ai_panel if page=='ai' else module.render_news_gold_panel)(panel,fonts,doc,doc['ts'])
    assert image.size==(800,480) and image.tobytes()==bytes(panel.fb)
    assert meta['render_at']==doc['ts'] and meta['stale']
    assert not panel.missing


def test_preview_does_not_import_or_call_upstream_composers():
    source=Path(preview.__file__).read_text()
    for forbidden in ['get_codex_usage(', 'gold_feed.', 'news_brief.', 'reliable_sources.',
                      'openai_costs.', 'urlopen(', 'publish_one(', 'load_effective(']:
        assert forbidden not in source


def test_real_route_requires_session_and_returns_structured_errors(isolated,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from api.routes.desktop import router as desktop
    from api.routes.local_console import router as console
    from core import desktop_browser as browser
    monkeypatch.setenv('INKSIGHT_DESKTOP_MODE','1')
    monkeypatch.setenv('INKSIGHT_DESKTOP_PORT','18181')
    monkeypatch.setenv('INKSIGHT_DESKTOP_TOKEN','isolated-preview-test')
    app=FastAPI();app.include_router(desktop,prefix='/api');app.include_router(console,prefix='/api')
    app.add_middleware(browser.DesktopBoundaryMiddleware)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',54321)),base_url='http://127.0.0.1:18181') as client:
            assert (await client.get('/api/local-console/preview')).status_code==401
            ticket=(await client.post('/api/desktop/browser-ticket',headers={'X-InkSight-Desktop-Token':'isolated-preview-test'})).json()['ticket']
            assert (await client.post('/api/desktop/browser-session',json={'ticket':ticket},headers={'Origin':'http://127.0.0.1:18181'})).status_code==200
            response=await client.get('/api/local-console/preview')
            assert response.status_code==404 and response.json()['error']=='unpublished'
            assert (await client.get('/api/local-console/preview?source=arbitrary')).status_code==422
            browser._sessions.clear()
            assert (await client.get('/api/local-console/preview/devices')).status_code==401
    run(scenario())
