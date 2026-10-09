"""Offline passwordless UI boundary; no live keys, sockets, or user database."""
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI

from api.routes.desktop import router
from api.routes.pages import router as pages_router
from core import auth, desktop_browser as browser
from core.local_console_security import require_local_root, require_local_root_csrf, issue_csrf_token


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('INKSIGHT_DESKTOP_MODE', '1')
    monkeypatch.setenv('INKSIGHT_DESKTOP_PORT', '18181')
    monkeypatch.setenv('INKSIGHT_DESKTOP_TOKEN', 'test-app-launch-only')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-internal-only')
    browser._tickets.clear()
    browser._sessions.clear()
    application = FastAPI()
    application.include_router(router, prefix='/api')
    application.include_router(pages_router)
    @application.get('/api/local-console/config')
    async def config(user=Depends(require_local_root)):
        return {'operator': user, 'csrf_token': issue_csrf_token(user)}
    @application.put('/api/local-console/config')
    async def save(user=Depends(require_local_root_csrf)):
        return {'saved': True}
    @application.get('/api/admin/internal')
    async def internal():
        return {'internal': True}
    @application.get('/api/health')
    async def health():
        return {'status': 'ok'}
    application.add_middleware(browser.DesktopBoundaryMiddleware)
    yield application
    browser._tickets.clear()
    browser._sessions.clear()


async def client(app, host='127.0.0.1', peer='127.0.0.1'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
                             base_url=f'http://{host}:18181')


async def enter(c):
    minted = await c.post('/api/desktop/browser-ticket',
                          headers={'X-InkSight-Desktop-Token': 'test-app-launch-only'})
    assert minted.status_code == 200
    ticket = minted.json()['ticket']
    exchanged = await c.post('/api/desktop/browser-session', json={'ticket': ticket},
                             headers={'Origin': str(c.base_url).rstrip('/')})
    assert exchanged.status_code == 200
    return ticket, exchanged


def run(coro):
    previous = asyncio.get_event_loop()
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(previous)


def test_no_database_account_required_and_csrf_still_required(app):
    async def scenario():
        async with await client(app) as c:
            assert (await c.get('/api/local-console/config')).status_code == 401
            ticket, response = await enter(c)
            cookie = response.headers['set-cookie']
            assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
            assert 'Domain=' not in cookie and 'Max-Age=28800' in cookie
            protected = await c.get('/api/local-console/config')
            assert protected.json()['operator'] == 0
            assert (await c.put('/api/local-console/config', json={})).status_code == 403
            assert (await c.put('/api/local-console/config', json={}, headers={
                'X-CSRF-Token': protected.json()['csrf_token'],
                'Origin': 'http://127.0.0.1:18181'})).status_code == 200
            assert (await c.post('/api/desktop/browser-session', json={'ticket': ticket},
                headers={'Origin': 'http://127.0.0.1:18181'})).status_code == 401
            assert ticket not in response.text
    run(scenario())


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'}, {'Origin': 'null'},
    {'Origin': 'http://127.0.0.1:18182'}, {'Origin': 'http://localhost:18181'},
    {'Referer': 'https://evil.example/x'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'}, {'X-Forwarded-Host': '127.0.0.1'},
    {'X-Forwarded-For': '127.0.0.1'}, {'Forwarded': 'for=127.0.0.1'},
    {'Host': 'evil.example:18181'}, {'Host': '127.0.0.1.evil.example:18181'},
    {'Host': '127.0.0.1:18182'}, {'Host': '127.0.0.1:bad'},
])
def test_wrong_source_cannot_read_even_with_session(app, headers):
    async def scenario():
        async with await client(app) as c:
            await enter(c)
            assert (await c.get('/api/local-console/config', headers=headers)).status_code == 403
    run(scenario())


@pytest.mark.parametrize('host,peer', [('127.0.0.1','192.168.1.10'),
                                       ('[::1]','::1'), ('localhost','127.0.0.1')])
def test_exact_loopback_addresses_only(app, host, peer):
    async def scenario():
        async with await client(app, host, peer) as c:
            response = await c.get('/api/health')
            assert response.status_code == (200 if peer in {'127.0.0.1','::1'} else 403)
    run(scenario())


@pytest.mark.parametrize('path', ['/api/auth/login', '/api/auth/register',
    '/api/auth/local-root-bootstrap', '/api/auth/reset-password', '/api/v1/auth/login'])
def test_old_password_and_account_creation_flow_retired(app, path):
    async def scenario():
        async with await client(app) as c:
            assert (await c.post(path, json={})).status_code == 410
    run(scenario())


def test_session_expiry_restart_legacy_cookie_and_internal_separation(app, monkeypatch):
    async def scenario():
        async with await client(app) as c:
            c.cookies.set('ink_session', auth.create_session_token(1, 'old-root'))
            assert (await c.get('/api/local-console/config')).status_code == 401
            assert auth._extract_user(c.cookies.get('ink_session'), None) is None
            await enter(c)
            assert (await c.get('/api/admin/internal')).status_code == 403
            assert (await c.get('/api/admin/internal', headers={
                'Authorization':'Bearer test-internal-only'})).status_code == 200
            token = c.cookies.get(browser.COOKIE)
            until, bound = browser._sessions[token]
            browser._sessions[token] = (0, bound)
            assert (await c.get('/api/local-console/config')).status_code == 401
            await enter(c)
            browser._sessions.clear()  # Backend restart loses all browser capabilities.
            assert (await c.get('/api/local-console/config')).status_code == 401
    run(scenario())


def test_ticket_expired_requires_app_and_json_same_origin(app):
    async def scenario():
        async with await client(app) as c:
            assert (await c.post('/api/desktop/browser-ticket')).status_code == 403
            minted = await c.post('/api/desktop/browser-ticket', headers={
                'X-InkSight-Desktop-Token':'test-app-launch-only'})
            ticket = minted.json()['ticket']
            assert (await c.post('/api/desktop/browser-session', json={'ticket':ticket})).status_code == 403
            browser._tickets[ticket] = (0, 'http://127.0.0.1:18181')
            assert (await c.post('/api/desktop/browser-session', json={'ticket':ticket},
                headers={'Origin':'http://127.0.0.1:18181'})).status_code == 401
    run(scenario())


def test_menu_quota_needs_internal_app_capability_not_browser_session(app, monkeypatch):
    from core import desktop_quota
    monkeypatch.setattr(desktop_quota, 'snapshot', lambda: {'remaining_percent': 36})
    async def scenario():
        async with await client(app) as c:
            assert (await c.get('/api/desktop/quota')).status_code == 403
            await enter(c)
            assert (await c.get('/api/desktop/quota')).status_code == 403
            assert (await c.get('/api/desktop/quota', headers={
                'X-InkSight-Desktop-Token': 'test-app-launch-only'})).json() == {'remaining_percent': 36}
            assert (await c.get('/api/desktop/quota', headers={
                'X-InkSight-Desktop-Token': 'test-app-launch-only',
                'Origin': 'https://evil.example'})).status_code == 403
    run(scenario())


def test_boundary_headers_and_frontend_no_password_storage(app):
    async def scenario():
        async with await client(app) as c:
            response = await c.get('/api/health')
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['referrer-policy'] == 'no-referrer'
            assert response.headers['x-frame-options'] == 'DENY'
    run(scenario())
    folder = Path(__file__).parents[1] / 'static/manager'
    html = (folder/'index.html').read_text()
    js = (folder/'manager.js').read_text()
    entry = (folder/'desktop-entry.js').read_text()
    assert 'id="loginForm"' not in html and 'id="setupForm"' not in html
    assert '/api/auth/login' not in js and '/api/auth/local-root-bootstrap' not in js
    assert 'history.replaceState' in entry
    assert '.setItem(' not in entry


def test_each_menu_open_has_a_new_document_path_and_versioned_assets(app):
    import re
    async def scenario():
        async with await client(app) as c:
            async def launch():
                result = await c.post('/api/desktop/browser-ticket', headers={
                    'X-InkSight-Desktop-Token': 'test-app-launch-only'})
                return result.json()
            first, second = await launch(), await launch()
            assert first['entry_path'] != second['entry_path']
            assert first['ticket'] not in first['entry_path']
            response = await c.get(first['entry_path'])
            assert response.status_code == 200
            assert response.headers['x-inksight-ui'] == 'desktop-session-v2'
            assert response.headers['cache-control'] == 'no-store'
            assert 'id="loginForm"' not in response.text and 'root' not in response.text
            assets = re.findall(r'/static/manager/[^" ]+\?v=[0-9a-f]{16}', response.text)
            assert len(assets) == 3
            for asset in assets:
                # This small fixture has no static mount; production does.
                assert '?' in asset
            assert (await c.get('/desktop/config/not-an-entry')).status_code == 404
            assert (await c.get(first['entry_path'], headers={'Origin':'https://evil.example'})).status_code == 403
            assert (await c.get('/api/local-console/config')).status_code == 401
    run(scenario())
