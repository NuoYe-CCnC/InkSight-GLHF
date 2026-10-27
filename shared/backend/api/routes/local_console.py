from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from core import (
    codex_collector_health,
    firmware_tasks,
    firmware_setup,
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


@router.get("/codex-announcements")
async def codex_announcements_status(_user_id: int = Depends(require_local_root)):
    from core.codex_announcements import diagnostics
    return {"ok": True, **diagnostics()}

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
        "codex_collector": codex_collector_health.snapshot(),
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
    try:
        return {"ok": True, "devices": await local_console_preview.list_devices(user_id)}
    except local_console_preview.PreviewError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "detail": str(exc)},
                            status_code=exc.status)


@router.get('/cloud/connection-check')
async def cloud_check_status(_user_id: int = Depends(require_local_root)):
    from core import cloud_connection_check as check
    return {'ok':True,'confirmation_token':check.ticket(),'task':check.latest()}


@router.post('/cloud/connection-check')
async def cloud_check_run(body: dict, user_id: int = Depends(require_local_root_csrf)):
    from core import cloud_connection_check as check
    try:
        result=await check.run(user_id,body.get('device_id'),body.get('confirmation_token'))
        local_console_audit.record('cloud.connection_check.completed',user_id,task_id=result['id'])
        return {'ok':True,'task':result}
    except (RuntimeError,ValueError) as error:
        return JSONResponse({'ok':False,'error':str(error)},status_code=409)


@router.get("/preview")
async def console_preview(
    device_id: str = Query(default="", max_length=64),
    page: str = Query(default="ai", pattern="^(ai|news_gold)$"),
    source: str = Query(default="published", pattern="^(published|unbound)$"),
    user_id: int = Depends(require_local_root),
):
    try:
        image, metadata = await local_console_preview.render(
            user_id, device_id=device_id or None, page=page, source=source,
        )
    except local_console_preview.PreviewError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "detail": str(exc)},
                            status_code=exc.status)
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
            "X-InkSight-Published-At": str(metadata.get("published_at") or 0),
            "X-InkSight-Render-At": str(metadata["render_at"]),
            "X-InkSight-Snapshot-SHA256": metadata.get("snapshot_sha256", ""),
            "X-InkSight-Snapshot-Stale": "1" if metadata.get("stale") else "0",
            "X-InkSight-Publication-Kind": metadata.get('publication_kind','regular'),
            "X-InkSight-Display-Confirmed": "0",
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
        return {"ok": True, **await run_in_threadpool(firmware_tasks.preflight)}
    except (operator_config.ConfigError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get('/firmware/preparation')
async def firmware_preparation(_user_id: int = Depends(require_local_root)):
    try:return {'ok':True, **await run_in_threadpool(firmware_setup.status)}
    except RuntimeError as error:return JSONResponse({'ok':False,'error':str(error)},status_code=409)


@router.get('/firmware/history')
async def firmware_history(_user_id: int = Depends(require_local_root)):
    data=firmware_tasks._read()
    return {'ok':True,**{group:[firmware_tasks._public_job(row) for row in sorted(data.get(group,{}).values(),
            key=lambda row:row.get('created_at',0),reverse=True)[:8]] for group in ['builds','plans','flashes','rollbacks']}}


@router.get('/firmware/status')
async def firmware_status(_user_id: int = Depends(require_local_root)):
    try:
        return {'ok': True, **await run_in_threadpool(firmware_tasks.status_snapshot)}
    except (operator_config.ConfigError, RuntimeError) as error:
        return JSONResponse({'ok': False, 'error': str(error)}, status_code=409)


@router.post('/firmware/flashes/{task_id}/display-confirmation')
async def firmware_display_confirmation(task_id: str, body: dict, _user_id: int = Depends(require_local_root_csrf)):
    if body.get('confirmed') is not True:return JSONResponse({'ok':False,'error':'需要明确人工确认'},status_code=422)
    try:
        row=firmware_tasks.get_flash(task_id)
        if not row.get('verified_at'):raise RuntimeError('尚未完成写入校验，不能验收实体屏')
        task=firmware_tasks._set('flashes',task_id,physical_display_confirmed=True,physical_confirmation_at=__import__('time').time())
        local_console_audit.record('firmware.display.user_confirmed',_user_id,task_id=task_id)
        return {'ok':True,'task':task}
    except (KeyError,RuntimeError) as error:return JSONResponse({'ok':False,'error':str(error)},status_code=409)


@router.post('/firmware/preparation/tools')
async def firmware_prepare_tools(_user_id: int = Depends(require_local_root_csrf)):
    try:
        job=firmware_setup.start('tools')
        local_console_audit.record('firmware.tools.started',_user_id,task_id=job['id'])
        return JSONResponse({'ok':True,'task':job},status_code=202)
    except (ValueError,RuntimeError) as exc:
        return JSONResponse({'ok':False,'error':str(exc)},status_code=409)


@router.post('/firmware/preparation/fonts')
async def firmware_import_fonts(request: Request, _user_id: int = Depends(require_local_root_csrf)):
    # Two bounded font streams joined by a declared regular-file byte count.
    # No multipart dependency, filenames, archive paths, or arbitrary writes.
    if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':
        raise HTTPException(415,'请使用字体导入界面上传两份 OTF/TTF')
    try:regular_size=int(request.headers.get('x-inksight-regular-bytes','0'))
    except ValueError:raise HTTPException(422,'字体长度无效') from None
    regular_type=request.headers.get('x-inksight-regular-type','').lower()
    bold_type=request.headers.get('x-inksight-bold-type','').lower()
    if not 0<regular_size<=firmware_setup.FONT_MAX or regular_type not in {'.ttf','.otf'} or bold_type not in {'.ttf','.otf'}:
        raise HTTPException(422,'请选择独立的 Regular / Bold OTF 或 TTF，每份最多32MiB')
    if not firmware_setup.FONT_UPLOAD_LOCK.acquire(blocking=False):
        raise HTTPException(409,'另一份字体正在上传，请等待完成')
    try:
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>regular_size+firmware_setup.FONT_MAX:
                raise HTTPException(413,'字体上传过大；每份最多32MiB')
            data.extend(chunk)
        if not regular_size<len(data):
            raise HTTPException(422,'上传中断或缺少 Bold 字体，旧字库保持不变')
        uploads={'regular':('regular'+regular_type,bytes(data[:regular_size])),
                 'bold':('bold'+bold_type,bytes(data[regular_size:]))}
        job=await run_in_threadpool(firmware_setup.start,'fonts',uploads)
        local_console_audit.record('firmware.fonts.started',_user_id,task_id=job['id'])
        return JSONResponse({'ok':True,'task':job},status_code=202)
    except (ValueError,RuntimeError) as exc:
        return JSONResponse({'ok':False,'error':str(exc)},status_code=422)
    finally:
        firmware_setup.FONT_UPLOAD_LOCK.release()


@router.post('/firmware/preparation/{task_id}/cancel')
async def firmware_cancel_preparation(task_id: str, _user_id: int = Depends(require_local_root_csrf)):
    try:return {'ok':True,'task':firmware_setup.cancel(task_id)}
    except KeyError:return JSONResponse({'ok':False,'error':'task_not_found'},status_code=404)


@router.post("/firmware/builds")
async def firmware_build_create(body: Optional[dict] = None, _user_id: int = Depends(require_local_root_csrf)):
    try:
        confirmation = (body or {}).get('build_confirmation')
        if not isinstance(confirmation, str) or len(confirmation) != 64:
            raise RuntimeError('请先完成准备检查，再确认构建')
        task = await run_in_threadpool(firmware_tasks.create_build, confirmation)
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


@router.post('/firmware/builds/{task_id}/cancel')
async def firmware_build_cancel(task_id: str, _user_id: int = Depends(require_local_root_csrf)):
    try:
        return {'ok': True, 'task': firmware_tasks.cancel_build(task_id)}
    except KeyError:
        return JSONResponse({'ok': False, 'error': '未找到这次构建'}, status_code=404)


@router.get("/firmware/devices")
async def firmware_devices(_user_id: int = Depends(require_local_root)):
    return {"ok": True, "devices": firmware_tasks.detect_devices()}


@router.post('/firmware/probe')
async def firmware_probe(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        probe=await run_in_threadpool(firmware_tasks.probe_device,str(body.get('device_id') or ''))
        return {'ok':True,'probe':probe}
    except RuntimeError as exc:return JSONResponse({'ok':False,'error':str(exc)},status_code=409)


@router.post('/firmware/bind')
async def firmware_bind(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    from core import desktop_browser, state_store, config_store
    try:
        row=firmware_tasks.consume_probe(str(body.get('probe_id') or ''),str(body.get('confirmation_token') or ''))
        if _user_id==0 and desktop_browser.enabled():
            # Desktop operator is not a synthetic root user. Store a private
            # local binding, without creating a database user or membership.
            state_store.write_json(state_store.state_path('desktop_device_binding.json'),
                {'mac':row['mac'],'nickname':'InkSight 4.26','confirmed_at':__import__('time').time()},meta=False)
        else:
            result=await config_store.bind_device(_user_id,row['mac'],'InkSight 4.26')
            if result['status']=='pending_approval':raise RuntimeError('此设备已有所有者，需要原有配对批准')
        local_console_audit.record('firmware.device.bound',_user_id,device_fingerprint=row['device_fingerprint'])
        return {'ok':True,'device_fingerprint':row['device_fingerprint'],'writes_device':False}
    except RuntimeError as exc:return JSONResponse({'ok':False,'error':str(exc)},status_code=409)


@router.post("/firmware/flash-plans")
async def firmware_flash_plan(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        from core.firmware_usb import start_plan
        plan = start_plan(
            str(body.get("build_id") or ""), str(body.get("device_id") or ""),
            str(body.get("install_mode") or "update"),
        )
        if plan.get("install_mode") == "update":
            try:
                private = firmware_tasks.get_flash_plan_identity(str(plan["id"]))
                heartbeat = await stats_store.get_latest_heartbeat(private["mac"])
                firmware_tasks.bind_previous_build(str(plan["id"]), heartbeat)
            except Exception:  # Prior heartbeat is optional; never block a safe plan.
                pass
        local_console_audit.record(
            "firmware.flash.prepared", _user_id, plan_id=plan["id"],
            target=firmware_tasks.TARGET,
        )
        return JSONResponse({'ok':True,'plan':plan},status_code=202)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get('/firmware/flash-plans/{plan_id}')
async def firmware_flash_plan_status(plan_id: str, _user_id: int = Depends(require_local_root)):
    from core.firmware_usb import get_plan
    try:return {'ok':True,'plan':get_plan(plan_id)}
    except KeyError:return JSONResponse({'ok':False,'error':'plan_not_found'},status_code=404)


@router.post("/firmware/flashes")
async def firmware_flash_create(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        try:
            identity=firmware_tasks.get_flash_plan_identity(str(body.get('plan_id') or ''))
            heartbeat=await stats_store.get_latest_heartbeat(identity['mac'])
            firmware_tasks.bind_previous_build(str(body.get('plan_id') or ''),heartbeat)
        except (KeyError,RuntimeError):pass
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
        if task.get("status") in {"waiting_heartbeat", "network_timeout"}:
            private = firmware_tasks.get_flash_identity(task_id)
            heartbeat = await stats_store.get_latest_heartbeat(private["mac"])
            task = firmware_tasks.confirm_heartbeat(task_id, heartbeat)
        return {"ok": True, "task": task}
    except KeyError:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)


@router.post("/firmware/rollback-plans")
async def firmware_rollback_plan(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        plan = await run_in_threadpool(firmware_tasks.prepare_rollback,
            str(body.get("flash_id") or ""), str(body.get("device_id") or "")
        )
        local_console_audit.record(
            "firmware.rollback.prepared", _user_id, plan_id=plan["id"],
            device_fingerprint=plan["device_fingerprint"], target=plan["target"],
        )
        return {"ok": True, "plan": plan}
    except (RuntimeError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/firmware/rollbacks")
async def firmware_rollback_create(body: dict, _user_id: int = Depends(require_local_root_csrf)):
    try:
        task = firmware_tasks.create_rollback(
            str(body.get("plan_id") or ""), str(body.get("confirmation_token") or "")
        )
        task = firmware_tasks.start_rollback(str(task["id"]))
        local_console_audit.record("firmware.rollback.started", _user_id,
                                   task_id=task["id"], target=task["target"])
        return JSONResponse({"ok": True, "task": task}, status_code=202)
    except (RuntimeError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.get("/firmware/rollbacks/{task_id}")
async def firmware_rollback_status(task_id: str, _user_id: int = Depends(require_local_root)):
    try:
        task = firmware_tasks.get_rollback(task_id)
        if task.get("status") == "written_unconfirmed":
            private = firmware_tasks.get_rollback_identity(task_id)
            heartbeat = await stats_store.get_latest_heartbeat(private["mac"])
            task = firmware_tasks.confirm_rollback_heartbeat(task_id, heartbeat)
        return {"ok": True, "task": task}
    except KeyError:
        return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)


@router.get("/audit")
async def local_audit(_user_id: int = Depends(require_local_root)):
    return {"ok": True, "events": local_console_audit.recent()}
