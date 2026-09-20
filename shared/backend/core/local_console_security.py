"""Security boundary for the loopback-only operator console."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request

from .auth import get_current_root_user

_CSRF_SECRET = secrets.token_bytes(32)
_TOKEN_TTL_SECONDS = 8 * 60 * 60
_BOOTSTRAP_TTL_SECONDS = 10 * 60


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        if ipaddress.ip_address(normalized.strip("[]")).is_loopback:
            return True
    except ValueError:
        pass
    return (
        normalized == "testclient"
        and os.getenv("INKSIGHT_LOCAL_CONSOLE_TESTING", "").lower() in {"1", "true", "yes"}
    )


def require_loopback(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    request_host = request.url.hostname or ""
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    if forwarded_host:
        forwarded_host = forwarded_host.rsplit(":", 1)[0].strip("[]")
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        forwarded_for = forwarded_for.strip("[]")
    if (
        not _is_loopback_host(client_host)
        or not _is_loopback_host(request_host)
        or (forwarded_host and not _is_loopback_host(forwarded_host))
        or (forwarded_for and not _is_loopback_host(forwarded_for))
    ):
        raise HTTPException(status_code=403, detail="本机管理端只允许从当前电脑访问")


def require_same_origin(request: Request) -> None:
    if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="拒绝跨站操作")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    try:
        parsed = urlsplit(source)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="来源地址无效") from exc
    request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    source_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if (
        parsed.scheme not in {"http", "https"}
        or not _is_loopback_host(parsed.hostname or "")
        or parsed.scheme != request.url.scheme
        or source_port != request_port
    ):
        raise HTTPException(status_code=403, detail="操作来源与本机管理端不一致")


async def require_local_root(
    request: Request,
    user_id: int = Depends(get_current_root_user),
) -> int:
    require_loopback(request)
    return user_id


def issue_csrf_token(user_id: int, *, now: int | None = None) -> str:
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_urlsafe(18)
    body = f"{user_id}:{issued}:{nonce}".encode("utf-8")
    signature = hmac.new(_CSRF_SECRET, body, hashlib.sha256).digest()
    encoded_body = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return encoded_body + "." + encoded_signature


def issue_bootstrap_token(*, now: int | None = None) -> str:
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_urlsafe(24)
    body = f"bootstrap:{issued}:{nonce}".encode("utf-8")
    signature = hmac.new(_CSRF_SECRET, body, hashlib.sha256).digest()
    encoded_body = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return encoded_body + "." + encoded_signature


def verify_bootstrap_token(token: str, *, now: int | None = None) -> bool:
    try:
        encoded_body, encoded_signature = token.split(".", 1)
        body = base64.urlsafe_b64decode(
            (encoded_body + "=" * (-len(encoded_body) % 4)).encode("ascii")
        )
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode("ascii")
        )
        expected = hmac.new(_CSRF_SECRET, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        label, raw_issued, _nonce = body.decode("utf-8").split(":", 2)
        current = int(now if now is not None else time.time())
        issued = int(raw_issued)
        return label == "bootstrap" and 0 <= current - issued <= _BOOTSTRAP_TTL_SECONDS
    except (ValueError, TypeError, UnicodeDecodeError, base64.binascii.Error):
        return False


def verify_csrf_token(token: str, user_id: int, *, now: int | None = None) -> bool:
    try:
        encoded_body, encoded_signature = token.split(".", 1)
        body = base64.urlsafe_b64decode((encoded_body + "=" * (-len(encoded_body) % 4)).encode("ascii"))
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode("ascii")
        )
        expected = hmac.new(_CSRF_SECRET, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        raw_user, raw_issued, _nonce = body.decode("utf-8").split(":", 2)
        current = int(now if now is not None else time.time())
        issued = int(raw_issued)
        return int(raw_user) == user_id and 0 <= current - issued <= _TOKEN_TTL_SECONDS
    except (ValueError, TypeError, UnicodeDecodeError, base64.binascii.Error):
        return False


async def require_local_root_csrf(
    request: Request,
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    user_id: int = Depends(require_local_root),
) -> int:
    require_same_origin(request)
    if not x_csrf_token or not verify_csrf_token(x_csrf_token, user_id):
        raise HTTPException(status_code=403, detail="操作令牌无效或已过期，请刷新页面")
    return user_id
