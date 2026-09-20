"""api/routes/gold_admin.py — 金价补拉管理端点（第二阶段）

POST /api/admin/gold/catchup  发布端工具在检测到设备冷启动请求后调用（受冷却/预算保护）
GET  /api/admin/gold/status    状态查看
POST /api/admin/host/recover   macOS 原生唤醒通知触发一次受控主机恢复
鉴权：require_admin（ADMIN_TOKEN / root 会话，与现有 admin 路由一致）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from core.auth import require_admin
from core import gold_catchup

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gold-admin"])


@router.get("/admin/gold/status")
async def gold_status_endpoint(_: None = Depends(require_admin)):
    return gold_catchup.status()


@router.post("/admin/gold/catchup")
async def gold_catchup_endpoint(
    payload: Optional[dict] = Body(default=None),
    _: None = Depends(require_admin),
):
    try:
        return gold_catchup.catchup(
            request_id=(payload or {}).get("request_id"),
            reason=str((payload or {}).get("reason") or "publisher_queue"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/host/recover")
async def host_recover_endpoint(
    payload: Optional[dict] = Body(default=None),
    _: None = Depends(require_admin),
):
    from core import host_recovery
    reason = str((payload or {}).get("reason") or "host-wake")
    try:
        return await run_in_threadpool(host_recovery.recover, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/host/recovery-status")
async def host_recovery_status_endpoint(_: None = Depends(require_admin)):
    from core import host_recovery
    return host_recovery.status()


@router.post("/admin/news/check")
async def news_check_endpoint(
    payload: Optional[dict] = Body(default=None),
    _: None = Depends(require_admin),
):
    from core import news_due_request
    try:
        return await run_in_threadpool(
            news_due_request.check,
            (payload or {}).get("request_id"),
            reason=str((payload or {}).get("reason") or "publisher-queue"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/news/check-status")
async def news_check_status_endpoint(_: None = Depends(require_admin)):
    from core import news_due_request
    return news_due_request.status()
