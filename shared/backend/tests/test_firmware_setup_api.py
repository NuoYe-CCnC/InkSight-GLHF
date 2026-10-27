"""Real desktop cookie + CSRF checks around formal prerequisite endpoints."""
import asyncio
import httpx
import pytest
from fastapi import FastAPI
from api.routes.desktop import router as desktop_router
from api.routes.local_console import router as console_router
from core import desktop_browser, firmware_setup, firmware_tasks, state_store
from core.local_console_security import issue_csrf_token


@pytest.fixture
def app(monkeypatch,tmp_path):
    monkeypatch.setenv('INKSIGHT_DESKTOP_MODE','1');monkeypatch.setenv('INKSIGHT_DESKTOP_PORT','18181')
    monkeypatch.setenv('INKSIGHT_DESKTOP_TOKEN','test-entry-only')
    monkeypatch.setenv('INKSIGHT_FIRMWARE_HOME',str(tmp_path/'private'))
    monkeypatch.setattr(firmware_tasks,'_STATE',tmp_path/'tasks.json')
    application=FastAPI();application.include_router(desktop_router,prefix='/api');application.include_router(console_router,prefix='/api')
    application.add_middleware(desktop_browser.DesktopBoundaryMiddleware)
    desktop_browser._sessions.clear();desktop_browser._tickets.clear()
    return application


async def enter(client):
    token=(await client.post('/api/desktop/browser-ticket',headers={'X-InkSight-Desktop-Token':'test-entry-only'})).json()['ticket']
    assert (await client.post('/api/desktop/browser-session',json={'ticket':token},headers={'Origin':'http://127.0.0.1:18181'})).status_code==200
    return {'X-CSRF-Token':issue_csrf_token(0),'Origin':'http://127.0.0.1:18181'}


def run(app,scenario):
    async def wrapped():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',12345)),base_url='http://127.0.0.1:18181') as client:
            return await scenario(client)
    return asyncio.run(wrapped())


def test_prerequisites_need_session_and_mutations_need_csrf(app,monkeypatch):
    calls=[]
    monkeypatch.setattr(firmware_setup,'start',lambda kind,uploads=None:calls.append((kind,uploads)) or {'id':'task-unit'})
    async def scenario(client):
        assert (await client.get('/api/local-console/firmware/preparation')).status_code==401
        headers=await enter(client)
        assert (await client.post('/api/local-console/firmware/preparation/tools')).status_code==403
        assert not calls
        assert (await client.post('/api/local-console/firmware/preparation/tools',headers=headers)).status_code==202
        assert calls==[('tools',None)]
        bad={**headers,'Origin':'http://127.0.0.1:18182'}
        assert (await client.post('/api/local-console/firmware/preparation/tools',headers=bad)).status_code==403
        assert len(calls)==1
    run(app,scenario)


def test_font_upload_is_bounded_and_original_paths_not_executed(app,monkeypatch):
    calls=[];monkeypatch.setattr(firmware_setup,'FONT_MAX',8)
    monkeypatch.setattr(firmware_setup,'start',lambda kind,uploads=None:calls.append((kind,uploads)) or {'id':'fonts-unit'})
    async def scenario(client):
        headers=await enter(client)
        headers.update({'Content-Type':'application/octet-stream','X-InkSight-Regular-Bytes':'4',
            'X-InkSight-Regular-Type':'.ttf','X-InkSight-Bold-Type':'.otf'})
        assert (await client.post('/api/local-console/firmware/preparation/fonts',content=b'abcdefgh',headers=headers)).status_code==202
        assert calls[0][0]=='fonts'
        assert calls[0][1]=={'regular':('regular.ttf',b'abcd'),'bold':('bold.otf',b'efgh')}
        response=await client.post('/api/local-console/firmware/preparation/fonts',content=b'a'*13,headers=headers)
        assert response.status_code==413 and len(calls)==1
        headers['X-InkSight-Regular-Type']='../../x.ttf'
        assert (await client.post('/api/local-console/firmware/preparation/fonts',content=b'abcdefgh',headers=headers)).status_code==422
    run(app,scenario)


def test_missing_font_bytes_or_unsupported_transport_refused(app,monkeypatch):
    monkeypatch.setattr(firmware_setup,'start',lambda *a,**k:pytest.fail('invalid upload must not start'))
    async def scenario(client):
        headers=await enter(client)
        assert (await client.post('/api/local-console/firmware/preparation/fonts',json={},headers=headers)).status_code==415
        headers.update({'Content-Type':'application/octet-stream','X-InkSight-Regular-Bytes':'4',
            'X-InkSight-Regular-Type':'.ttf','X-InkSight-Bold-Type':'.otf'})
        assert (await client.post('/api/local-console/firmware/preparation/fonts',content=b'abcd',headers=headers)).status_code==422
        headers['X-InkSight-Regular-Bytes']='invalid'
        assert (await client.post('/api/local-console/firmware/preparation/fonts',content=b'abcdefgh',headers=headers)).status_code==422
    run(app,scenario)


def test_display_confirmation_does_not_claim_network(app,monkeypatch):
    state_store.write_json(firmware_tasks._STATE,{'flashes':{'flash-unit':{
        'id':'flash-unit','status':'network_timeout','verified_at':1,'network_verified':False}},
        'builds':{},'plans':{},'rollback_plans':{},'rollbacks':{}},meta=False)
    async def scenario(client):
        headers=await enter(client)
        response=await client.post('/api/local-console/firmware/flashes/flash-unit/display-confirmation',headers=headers,json={'confirmed':True})
        assert response.status_code==200
        task=response.json()['task'];assert task['physical_display_confirmed'] is True and task['network_verified'] is False
    run(app,scenario)
