from __future__ import annotations

import logging
import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from api.routes import api_routers, page_routers
from api.shared import (
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    inksight_error_handler,
    lifespan,
    limiter,
)
from core.errors import InkSightError


def _build_allowed_hosts() -> list[str]:
    hosts = [
        "www.inksight.site",
        "inksight.site",
        "web.inksight.site",
        "localhost",
        "127.0.0.1",
        "::1",
        "test",
        "testserver",
    ]
    hosts.extend(_discover_lan_hosts())
    seen = set()
    result: list[str] = []
    extra = os.getenv("INKSIGHT_ALLOWED_HOSTS", "")
    for raw in [*hosts, *extra.split(",")]:
        host = raw.strip().lower()
        if not host:
            continue
        if host not in seen:
            seen.add(host)
            result.append(host)
    return result


def _discover_lan_hosts() -> set[str]:
    """Return concrete addresses currently owned by this host, never subnets."""
    result: set[str] = set()
    candidates: list[str] = []
    try:
        candidates.extend(
            row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None)
        )
    except OSError:
        pass
    for destination in (("192.0.2.1", 9), ("2001:db8::1", 9)):
        family = socket.AF_INET6 if ":" in destination[0] else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_DGRAM)
        try:
            probe.connect(destination)
            candidates.append(str(probe.getsockname()[0] or ""))
        except OSError:
            pass
        finally:
            probe.close()
    for raw in candidates:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        if not address.is_loopback and not address.is_unspecified:
            result.add(address.compressed.lower())
    return result


class _LanHostResolver:
    def __init__(self, ttl_seconds: float = 5.0):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._at = 0.0
        self._hosts: set[str] = set()

    def current(self) -> set[str]:
        now = time.monotonic()
        with self._lock:
            if now - self._at >= self.ttl_seconds:
                self._hosts = _discover_lan_hosts()
                self._at = now
            return set(self._hosts)


def _normalized_host(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit("//" + value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").strip().lower()
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).compressed.lower()
    except ValueError:
        return host


class DynamicTrustedHostMiddleware(BaseHTTPMiddleware):
    """Trust fixed public names plus the Mac's exact current LAN addresses."""

    def __init__(self, app: FastAPI, allowed_hosts: list[str]):
        super().__init__(app)
        self.allowed_hosts = {_normalized_host(host) for host in allowed_hosts}
        self.resolver = _LanHostResolver()

    async def dispatch(self, request: Request, call_next):
        host = _normalized_host(request.headers.get("host") or "")
        if host in self.allowed_hosts or host in self.resolver.current():
            return await call_next(request)
        return JSONResponse({"error": "host_not_allowed"}, status_code=400)


def _build_cors_settings() -> tuple[list[str], str | None]:
    """Resolve allowed browser Origins for CORS.

    - Default: official web origins + local Next.js + Expo Web on 3000 / 8081.
    - INKSIGHT_CORS_ORIGINS: comma-separated extra origins (e.g. LAN IP for phone / Expo).
    - INKSIGHT_CORS_ALLOW_LAN=1: allow any http(s) Origin on private IPv4 + localhost (dev only).
    """
    defaults = [
        "https://www.inksight.site",
        "https://inksight.site",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ]
    seen = set(defaults)
    origins = list(defaults)
    extra = os.getenv("INKSIGHT_CORS_ORIGINS", "")
    for part in extra.split(","):
        origin = part.strip()
        if origin and origin not in seen:
            seen.add(origin)
            origins.append(origin)

    origin_regex = None
    flag = os.getenv("INKSIGHT_CORS_ALLOW_LAN", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        # RFC1918 + loopback; any port (Expo / dev servers on arbitrary ports).
        origin_regex = (
            r"^https?://("
            r"localhost|127\.0\.0\.1"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
            r")(?::\d+)?$"
        )
    return origins, origin_regex


class OriginValidationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, allow_origins: list[str], allow_origin_regex: str | None = None):
        super().__init__(app)
        self.allow_origins = {origin.lower() for origin in allow_origins}
        self.allow_origin_regex = allow_origin_regex

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        if not origin:
            return await call_next(request)

        try:
            parsed = urlsplit(origin)
        except ValueError:
            return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

        if not parsed.scheme or not parsed.netloc:
            return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

        normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        same_origin = f"{forwarded_proto.lower()}://{forwarded_host.lower()}" if forwarded_host else ""
        if normalized == same_origin:
            return await call_next(request)

        if normalized in self.allow_origins:
            return await call_next(request)

        if self.allow_origin_regex:
            import re
            if re.match(self.allow_origin_regex, normalized):
                return await call_next(request)

        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)


class _AccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        full_path = ""
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            full_path = str(record.args[2] or "")
        if not full_path:
            try:
                full_path = record.getMessage()
            except Exception:
                full_path = ""
        return not (
            full_path.startswith("/api/device/")
            and (full_path.endswith("/state") or "/state?" in full_path)
        )


_uvicorn_access_logger = logging.getLogger("uvicorn.access")
_state_access_filter_present = any(isinstance(f, _AccessLogFilter) for f in _uvicorn_access_logger.filters)
if not _state_access_filter_present:
    _state_access_filter = _AccessLogFilter()
    _uvicorn_access_logger.addFilter(_state_access_filter)
    for _handler in _uvicorn_access_logger.handlers:
        _handler.addFilter(_state_access_filter)


_cors_origins, _cors_origin_regex = _build_cors_settings()
_allowed_hosts = _build_allowed_hosts()

app = FastAPI(title="InkSight API", version="1.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(
    DynamicTrustedHostMiddleware,
    allowed_hosts=_allowed_hosts,
)
app.add_middleware(
    OriginValidationMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(InkSightError, inksight_error_handler)

for router in api_routers:
    app.include_router(router, prefix="/api")
    app.include_router(router, prefix="/api/v1")

for router in page_routers:
    app.include_router(router)
