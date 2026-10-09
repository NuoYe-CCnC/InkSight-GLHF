"""Passwordless desktop UI capability, separate from device/internal credentials.

Only the owning App can mint a 60-second, single-use browser ticket. Tickets
travel in a fragment (never in HTTP URLs); JS immediately removes it and POSTs
it for a host-only, HttpOnly, SameSite=Strict session. All capabilities are
memory-only and invalidated by backend restart. Local processes are trusted;
remote pages, LAN clients and legacy JWT cookies are not.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE = "ink_desktop_session_v1"
SESSION_TTL = 8 * 60 * 60
_lock = threading.RLock()
_tickets: dict = {}
_sessions: dict = {}


def enabled():
    return os.environ.get("INKSIGHT_DESKTOP_MODE") == "1"


def origin(request):
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def validate_source(request):
    """No proxy trust, wildcard localhost subdomains, or same-site exceptions."""
    try:
        host = urlsplit("//" + request.headers.get("host", ""))
        if (host.hostname not in {"127.0.0.1", "::1", "localhost"}
                or host.username or host.password or host.path
                or request.client is None
                or request.client.host not in {"127.0.0.1", "::1"}
                or (os.environ.get("INKSIGHT_DESKTOP_PORT")
                    and host.port != int(os.environ["INKSIGHT_DESKTOP_PORT"]))):
            raise ValueError("not local")
        if any(name in request.headers for name in
               ("forwarded", "x-forwarded-host", "x-forwarded-for", "x-forwarded-proto")):
            raise ValueError("proxy not supported")
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            parsed = urlsplit(source)
            if (f"{parsed.scheme}://{parsed.netloc}" != origin(request)
                    or parsed.username or parsed.password):
                raise ValueError("wrong origin")
        if request.headers.get("sec-fetch-site", "none") not in {"none", "same-origin"}:
            raise ValueError("cross origin")
    except (ValueError, TypeError):
        raise HTTPException(403, "本机配置拒绝此请求来源")


def _prune(table):
    now = time.monotonic()
    for key in list(table):
        if table[key][0] <= now:
            del table[key]


def mint_ticket(request):
    validate_source(request)
    with _lock:
        _prune(_tickets)
        if len(_tickets) >= 32:
            raise HTTPException(429, "请稍后重新打开配置")
        token = secrets.token_urlsafe(32)
        _tickets[token] = (time.monotonic() + 60, origin(request))
    return token


def exchange(request, ticket):
    validate_source(request)
    if (request.headers.get("content-type", "").split(";", 1)[0] != "application/json"
            or request.headers.get("origin") != origin(request)):
        raise HTTPException(403, "需要同源启动握手")
    if not isinstance(ticket, str) or len(ticket) > 128:
        raise HTTPException(401, "请从 InkSight 菜单重新打开配置")
    with _lock:
        _prune(_tickets)
        record = _tickets.pop(ticket, None)
        if not record or record[1] != origin(request):
            raise HTTPException(401, "配置入口已过期，请从菜单重新打开")
        _prune(_sessions)
        if len(_sessions) >= 32:
            del _sessions[next(iter(_sessions))]
        token = secrets.token_urlsafe(32)
        _sessions[token] = (time.monotonic() + SESSION_TTL, origin(request))
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        secure=False, max_age=SESSION_TTL, path="/")
    response.delete_cookie("ink_session", path="/")
    return response


def authorize_session(request):
    validate_source(request)
    with _lock:
        _prune(_sessions)
        record = _sessions.get(request.cookies.get(COOKIE, ""))
        if not record or record[1] != origin(request):
            raise HTTPException(401, "请从 InkSight 菜单打开配置")
    # A local operator is not a database/root account. Never synthesize a user.
    return 0


class DesktopBoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not enabled():
            return await call_next(request)
        try:
            validate_source(request)
            path = request.url.path
            canonical = path.replace("/api/v1/", "/api/", 1)
            if canonical.startswith("/api/auth/"):
                raise HTTPException(410, "desktop_password_login_retired")
            public = (path == "/" or path.startswith("/desktop/config/")
                      or path.startswith("/static/manager/")
                      or canonical == "/api/health"
                      or canonical.startswith("/api/desktop/")
                      or canonical.startswith("/api/local-console/"))
            if not public:
                # Internal App workers keep their existing Bearer credential.
                # Device APIs retain their own per-device token checks.
                from .auth import is_admin_authorized
                if canonical.startswith('/api/device/') and canonical.endswith('/heartbeat'):
                    # Preserve the route's token OR approved-build upgrade
                    # heartbeat handshake; the route validates that capability.
                    pass
                elif not is_admin_authorized(request.headers.get("authorization")):
                    if canonical.startswith("/api/device/") and request.headers.get("x-device-token"):
                        from .auth import require_device_token, validate_mac_param
                        mac = validate_mac_param(canonical.split("/")[3])
                        await require_device_token(mac, request.headers.get("x-device-token"))
                    else:
                        raise HTTPException(403, "此接口不向本机网页开放")
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'")
        return response
