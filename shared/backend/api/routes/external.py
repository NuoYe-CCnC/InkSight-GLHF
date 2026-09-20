"""外部数据源接收端点（Codex 订阅额度等由本机同步客户端推送）。

接口契约 §2.2：
  POST /api/device/{mac}/external/codex-usage
  Authorization: Bearer <token>  或  X-Device-Token: <token>

鉴权两种：
  1) 绑定设备的 device token（后端模式设备拉内容）；
  2) ADMIN_TOKEN（云端模式：发布端所在机器即受信后端，推送到伪设备名即可，
     设备不绑定后端也能把 Codex 数据送进本机 AI_USAGE）。
"""
from __future__ import annotations

import hmac
import os
import time
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException

from core.auth import require_device_token
from core.codex_usage_store import set_codex_usage

from pydantic import BaseModel, Field, field_validator

router = APIRouter(tags=["external"])

VALID_SOURCES = ("mac", "windows")


class CodexWindow(BaseModel):
    label: str = ""
    duration_minutes: Optional[float] = None
    used_percent: float = Field(ge=0, le=100)
    resets_at: Optional[float] = None


class CodexUsagePayload(BaseModel):
    source: str
    ts: Optional[float] = None
    windows: List[CodexWindow] = Field(default_factory=list, max_length=8)
    reset_credits_available: Optional[int] = Field(default=None, ge=0, le=100)
    auth_ok: Optional[bool] = None
    # 手动重置机会到期列表（2026-09-07 接入：rateLimitResetCredits.credits[].expiresAt）
    reset_expiry_list: Optional[List[int]] = None   # Unix 秒，升序；None=未接通，[]=明确0次
    reset_expiry_source: Optional[str] = None       # "api" | "manual"
    reset_expiry_total: Optional[int] = None        # 原始返回项数（核对用）
    reset_expiry_note: Optional[str] = None         # 次数/列表不一致标记
    plan: Optional[str] = None
    # Codex account credits are independent from rateLimitResetCredits.
    # Balance stays a string so the source precision is never rounded in transit.
    credit_balance: Optional[str] = Field(default=None, max_length=64)
    credit_has_credits: Optional[bool] = None
    credit_unlimited: Optional[bool] = None
    account_key: Optional[str] = Field(default=None, pattern="^[0-9a-f]{20}$")

    @field_validator("credit_balance")
    @classmethod
    def validate_credit_balance(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("credit_balance must be a decimal string") from exc
        if not number.is_finite() or number < 0:
            raise ValueError("credit_balance must be finite and nonnegative")
        return value


def _extract_token(x_device_token: Optional[str], authorization: Optional[str]) -> str:
    if x_device_token and x_device_token.strip():
        return x_device_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


@router.post("/device/{mac}/external/codex-usage")
async def post_codex_usage(
    mac: str,
    body: CodexUsagePayload,
    x_device_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    mac = mac.strip().upper()
    if not mac:
        raise HTTPException(status_code=400, detail="invalid mac")

    token = _extract_token(x_device_token, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    admin_token = os.environ.get("ADMIN_TOKEN") or ""
    if not (admin_token and hmac.compare_digest(token, admin_token)):
        try:
            await require_device_token(mac, token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="unauthorized") from exc

    if body.source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {VALID_SOURCES}")

    payload = body.model_dump(exclude_none=True)
    if not payload.get("ts"):
        payload["ts"] = time.time()
    await set_codex_usage(mac, body.source, payload)
    return {"ok": True, "mac": mac, "source": body.source}
