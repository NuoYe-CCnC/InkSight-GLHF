from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from core import (
    firmware_tasks,
    local_console_audit,
    local_console_config,
    local_console_preview,
    manual_issue,
    news_brief,
    news_credential,
    news_schedule,
    openai_costs,
    operator_config,
    stats_store,
)
from core.local_console_security import (
    issue_csrf_token,
    require_local_root,
    require_local_root_csrf,
)

router = APIRouter(prefix="/local-console", tags=["local-console"])

_CREDENTIAL_ERRORS = {
    "missing": "尚未配置密钥。",
    "pending_validation": "密钥已经保存并应用，但尚未验证。",
    "unauthorized": "密钥无效或已被撤销。",
    "forbidden": "密钥缺少读取组织费用所需的 Owner 权限。",
    "organization_mismatch": "密钥无权访问所填写的组织。",
    "rate_limited": "请求过于频繁，请稍后再试。",
    "network": "当前网络无法连接费用查询服务。",
    "temporary_failure": "费用查询服务暂时不可用，请稍后再试。",
    "cooldown": "刷新间隔未到，请稍后再试。",
}


def _credential_error(state: str | None) -> str:
    return _CREDENTIAL_ERRORS.get(str(state or ""), "验证未完成，请稍后重试。")


def _config_error(exc: operator_config.ConfigError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "configuration_invalid", "details": exc.errors},
        status_code=422,
    )


def _draft_conflict(exc: local_console_config.DraftConflictError) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "draft_conflict",
            "conflict": exc.code,
            "detail": str(exc),
            "reload_required": True,
        },
        status_code=409,
    )


@router.get("/bootstrap")
async def bootstrap(user_id: int = Depends(require_local_root)):
    return {
        "ok": True,
        "csrf_token": issue_csrf_token(user_id),
        "scope": "loopback-root",
        "credential": news_schedule.credential_state(),
        "openai_costs": openai_costs.snapshot(),
        "openai_costs_credential": openai_costs.credential_state(),
    }


@router.get("/overview")
async def overview(_user_id: int = Depends(require_local_root)):
    config_view = local_console_config.view()
    brief_state = news_brief._load_state()
    current = brief_state.get("current") if isinstance(brief_state, dict) else None
    current_public = None
    if isinstance(current, dict):
        current_public = {
            key: current.get(key)
            for key in (
                "mode", "period", "schedule_id", "issue_title", "date", "planned_at",
                "generated_at", "text", "freshness", "note", "origin", "version",
            )
        }
        current_public["events"] = [
            {key: event.get(key) for key in
             ("source", "url", "title", "published_at", "event_at")}
            for event in (current.get("events") or [])[:4]
            if isinstance(event, dict)
        ]
    schedule_state = news_schedule._load()
    return {
        "ok": True,
        "version": "1.1.0",
        "config": {
            "fingerprint": config_view["fingerprint"],
            "degraded": config_view["degraded"],
            "warnings": config_view["warnings"],
            "draft_pending": config_view["draft"] is not None,
        },
        "credential": news_schedule.credential_state(),
        "current_issue": current_public,
        "schedule": {
            "rows": news_schedule._config().get("schedules") or [],
            "effective_windows": news_schedule.effective_windows(),
            "recent_log": (schedule_state.get("log") or [])[-12:],
        },
        "manual_tasks": manual_issue.list_recent(5),
    }


@router.get("/preview/devices")
async def preview_devices(user_id: int = Depends(require_local_root)):
    return {"ok": True, "devices": await local_console_preview.list_devices(user_id)}


@router.get("/preview")
async def console_preview(
    device_id: str = Query(default="", max_length=64),
    page: str = Query(default="ai", pattern="^(ai|news_gold)$"),
    user_id: int = Depends(require_local_root),
):
    try:
        image, metadata = await local_console_preview.render(
            user_id, device_id=device_id or None, page=page,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-InkSight-Preview": "document-render",
            "X-InkSight-Device-Bound": "1" if metadata["device_bound"] else "0",
            "X-InkSight-Data-Policy": metadata["data_policy"],
        },
    )


@router.get("/config")
async def get_config(_user_id: int = Depends(require_local_root)):
    try:
        return {"ok": True, **local_console_config.view()}
    except operator_config.ConfigError as exc:
        return _config_error(exc)


@router.put("/config/draft")
async def put_config_draft(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        result = local_console_config.save_draft(
            body.get("public"),
            body.get("secret_updates") if isinstance(body.get("secret_updates"), dict) else {},
            expected_revision=body.get("expected_revision"),
        )
        local_console_audit.record("config.draft.saved", _user_id)
        return {"ok": True, **result}
    except operator_config.ConfigError as exc:
        return _config_error(exc)
    except local_console_config.DraftConflictError as exc:
        return _draft_conflict(exc)


@router.delete("/config/draft")
async def delete_config_draft(
    body: Optional[dict] = None,
    _user_id: int = Depends(require_local_root_csrf),
):
    try:
        changed = local_console_config.discard_draft(
            expected_revision=(body or {}).get("expected_revision")
        )
        local_console_audit.record("config.draft.discarded", _user_id, changed=changed)
        return {"ok": True, "changed": changed, **local_console_config.view()}
    except operator_config.ConfigError as exc:
        return _config_error(exc)
    except local_console_config.DraftConflictError as exc:
        return _draft_conflict(exc)


@router.post("/config/apply")
async def apply_config_draft(
    body: Optional[dict] = None,
    _user_id: int = Depends(require_local_root_csrf),
):
    try:
        result = local_console_config.apply_draft(
            expected_revision=(body or {}).get("expected_revision")
        )
        local_console_audit.record("config.applied", _user_id, changed=result.get("changed"))
        return {"ok": True, **result}
    except operator_config.ConfigError as exc:
        return _config_error(exc)
    except local_console_config.DraftConflictError as exc:
        return _draft_conflict(exc)


@router.post("/credentials/news/validate")
async def validate_news_key(_user_id: int = Depends(require_local_root_csrf)):
    result = news_credential.validate_configured_key()
    local_console_audit.record("credential.news.validated", _user_id, status=result["state"])
    code = 200 if result["state"] == "active" else 409
    return JSONResponse(
        {
            "ok": result["state"] == "active",
            "credential": result,
            **({"detail": _credential_error(result.get("state"))} if code != 200 else {}),
        },
        status_code=code,
    )


@router.get("/openai-costs")
async def openai_costs_status(_user_id: int = Depends(require_local_root)):
    return {
        "ok": True,
        "credential": openai_costs.credential_state(),
        "snapshot": openai_costs.snapshot(),
    }


@router.post("/openai-costs/verify")
async def verify_openai_costs_key(_user_id: int = Depends(require_local_root_csrf)):
    result = await openai_costs.refresh(reason="verify", verify=True, force=True)
    local_console_audit.record(
        "credential.openai_costs.verified", _user_id, status=result.get("state"))
    if not result.get("ok"):
        result = {
            **result,
            "error": result.get("state") or "verification_failed",
            "detail": _credential_error(result.get("state")),
        }
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/openai-costs/refresh")
async def refresh_openai_costs(_user_id: int = Depends(require_local_root_csrf)):
    result = await openai_costs.refresh(reason="manual")
    local_console_audit.record(
        "openai_costs.refreshed", _user_id, status=result.get("state"))
    if result.get("ok"):
        code = 200
    elif result.get("state") == "cooldown":
        code = 429
    else:
        code = 409
    if not result.get("ok"):
        result = {
            **result,
            "error": result.get("state") or "refresh_failed",
            "detail": _credential_error(result.get("state")),
        }
    return JSONResponse(result, status_code=code)


@router.get("/manual-issues/preflight")
async def manual_issue_preflight(_user_id: int = Depends(require_local_root)):
    return {"ok": True, **manual_issue.preflight()}


@router.get("/manual-issues")
async def manual_issue_list(_user_id: int = Depends(require_local_root)):
    return {"ok": True, "tasks": manual_issue.list_recent()}


@router.get("/manual-issues/{task_id}")
async def manual_issue_status(task_id: str, _user_id: int = Depends(require_local_root)):
    task = manual_issue.get(task_id)
    if not task:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)
    return {"ok": True, "task": task}


@router.post("/manual-issues")
async def manual_issue_create(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        task, created = manual_issue.create(
            str(body.get("idempotency_key") or ""),
            issue_title=str(body.get("title") or "手动生成"),
        )
        task = manual_issue.start(str(task["id"]))
        local_console_audit.record("manual_issue.started", _user_id, task_id=task["id"], status=task["status"])
        return JSONResponse(
            {"ok": True, "created": created, "task": task},
            status_code=202 if created else 200,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/manual-issues/{task_id}/resume")
async def manual_issue_resume(task_id: str, _user_id: int = Depends(require_local_root_csrf)):
    try:
        task = manual_issue.start(task_id)
        local_console_audit.record("manual_issue.resumed", _user_id, task_id=task_id, status=task["status"])
        return {"ok": True, "task": task}
    except KeyError:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get("/firmware/preflight")
async def firmware_preflight(_user_id: int = Depends(require_local_root)):
    try:
        return {"ok": True, **firmware_tasks.preflight()}
    except (operator_config.ConfigError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/firmware/builds")
async def firmware_build_create(_user_id: int = Depends(require_local_root_csrf)):
    try:
        task = firmware_tasks.create_build()
        task = firmware_tasks.start_build(str(task["id"]))
        local_console_audit.record("firmware.build.started", _user_id, task_id=task["id"], target=task["target"])
        return JSONResponse({"ok": True, "task": task}, status_code=202)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get("/firmware/builds/{task_id}")
async def firmware_build_status(task_id: str, _user_id: int = Depends(require_local_root)):
    try:
        return {"ok": True, "task": firmware_tasks.get_build(task_id)}
    except KeyError:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)


@router.get("/firmware/devices")
async def firmware_devices(_user_id: int = Depends(require_local_root)):
    return {"ok": True, "devices": firmware_tasks.detect_devices()}


@router.post("/firmware/flash-plans")
async def firmware_flash_plan(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        plan = firmware_tasks.prepare_flash(
            str(body.get("build_id") or ""), str(body.get("device_id") or ""),
            str(body.get("install_mode") or "update"),
        )
        local_console_audit.record(
            "firmware.flash.prepared", _user_id, plan_id=plan["id"],
            device_fingerprint=plan["device_fingerprint"], target=plan["target"],
        )
        return {
            "ok": True,
            "plan": plan,
        }
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/firmware/flashes")
async def firmware_flash_create(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        task = firmware_tasks.create_flash(
            str(body.get("plan_id") or ""), str(body.get("confirmation_token") or "")
        )
        task = firmware_tasks.start_flash(str(task["id"]))
        local_console_audit.record("firmware.flash.started", _user_id, task_id=task["id"], target=task["target"])
        return JSONResponse({"ok": True, "task": task}, status_code=202)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get("/firmware/flashes/{task_id}")
async def firmware_flash_status(task_id: str, _user_id: int = Depends(require_local_root)):
    try:
        task = firmware_tasks.get_flash(task_id)
        if task.get("status") == "waiting_heartbeat":
            private = firmware_tasks.get_flash_identity(task_id)
            heartbeat = await stats_store.get_latest_heartbeat(private["mac"])
            task = firmware_tasks.confirm_heartbeat(task_id, heartbeat)
        return {"ok": True, "task": task}
    except KeyError:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)


@router.get("/audit")
async def local_audit(_user_id: int = Depends(require_local_root)):
    return {"ok": True, "events": local_console_audit.recent()}
