"""Per-launch, loopback-only desktop lifecycle control. Not a public admin API."""
import hmac
import os
import uuid

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from core import desktop_service
from core import desktop_browser

router = APIRouter(prefix='/desktop', tags=['desktop'])


def authorize(request):
    expected = os.environ.get('INKSIGHT_DESKTOP_TOKEN', '')
    supplied = request.headers.get('X-InkSight-Desktop-Token', '')
    if (not request.client or request.client.host not in {'127.0.0.1', '::1'}
            or not expected or not hmac.compare_digest(expected, supplied)):
        raise HTTPException(status_code=403, detail='desktop lifecycle control unavailable')


@router.post('/browser-ticket')
async def browser_ticket(request: Request):
    authorize(request)
    if not desktop_browser.enabled():
        raise HTTPException(403, 'desktop browser unavailable')
    return {'ticket': desktop_browser.mint_ticket(request),
            'entry_path': '/desktop/config/' + uuid.uuid4().hex}


@router.post('/browser-session')
async def browser_session(request: Request):
    if not desktop_browser.enabled():
        raise HTTPException(403, 'desktop browser unavailable')
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, 'invalid handshake')
    return desktop_browser.exchange(request, body.get('ticket') if isinstance(body, dict) else None)


@router.post('/service/{action}')
async def service(action: str, request: Request):
    authorize(request)
    if action == 'pause':
        desktop_service.gate.pause()
    elif action == 'resume':
        desktop_service.gate.resume()
    elif action != 'status':
        raise HTTPException(status_code=400, detail='unsupported action')
    return await run_in_threadpool(desktop_service.status)


@router.get('/quota')
async def quota(request: Request):
    authorize(request)
    if not desktop_browser.enabled():
        raise HTTPException(403, 'desktop quota unavailable')
    from core.desktop_quota import snapshot
    return await run_in_threadpool(snapshot)
