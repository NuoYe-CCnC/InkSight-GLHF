"""Experimental FxEmbed forecasts, isolated from usage/credits and paid work.

Every GET uses the fixed public HTTPS host without Key/Cookie. Polling has its
own persisted cadence, night slots, limits, backoff and process/thread barrier.
Snapshots are local-only reads, filter expiry every time, and never advance the
source/usage success timestamps. No account change is treated as fulfillment.
"""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import codex_announcement_parser as parser
from . import codex_announcement_schedule as policy
from . import codex_announcement_sources as sources
from . import desktop_service, state_store

STATE_FILE = state_store.state_path("codex_announcements.json")
HOST = "api.fxtwitter.com"
MAX_BYTES = 1024 * 1024
MAX_POSTS = 200
MAX_REQUESTS = 4
TICK_SECONDS = 30
_poll_lock = threading.Lock()
_stop = threading.Event()
_thread = None


def configure_state_file(path):
    global STATE_FILE
    STATE_FILE = Path(path)


def _config() -> dict:
    from .operator_config import load_effective
    return load_effective().config


def _default(cfg):
    return {"schema": 1, "target": cfg["target_handle"].lower(), "author_id": cfg["target_user_id"],
            "posts": {}, "tombstones": {}, "slot_success": {}, "pending_slots": [], "last_success_at": 0,
            "last_attempt_at": 0, "next_allowed_at": 0, "failures": 0, "inflight_until": 0,
            "last_overlap_at": 0, "watermark": 0, "diagnostics": []}


def _state(cfg):
    value, error = state_store.read_json(STATE_FILE)
    if error == "missing":
        return _default(cfg)
    if error or not isinstance(value, dict) or value.get("schema") != 1:
        raise state_store.StateStoreError("announcement state unavailable")
    for key in ("posts", "tombstones", "slot_success"):
        if not isinstance(value.get(key), dict):
            raise state_store.StateStoreError("announcement state shape unavailable")
    if not isinstance(value.get("pending_slots", []), list):
        raise state_store.StateStoreError("announcement pending slots unavailable")
    for key in ("last_success_at", "last_attempt_at", "next_allowed_at", "inflight_until", "last_overlap_at", "watermark", "failures"):
        if not parser.finite(value.get(key)) or value[key] < 0:
            raise state_store.StateStoreError("announcement state timestamp unavailable")
    if value.get("target") != cfg["target_handle"].lower() or value.get("author_id") != cfg["target_user_id"]:
        fresh = _default(cfg)
        # Changing a public target cannot bypass host-level failures/rate limits.
        for key in ("last_attempt_at", "next_allowed_at", "failures", "inflight_until"):
            fresh[key] = value.get(key, fresh[key])
        return fresh
    return value


def account_context(cfg: dict, now: float) -> dict:
    from . import data_cache, desktop_quota
    identity = desktop_quota.account_key()
    group = data_cache.get_group("ai.codex") or {}
    value = group.get("value") if isinstance(group.get("value"), dict) else {}
    stamp = group.get("last_success")
    plan, valid_until = None, None
    if (identity and identity == value.get("account_key") and value.get("source") == "mac"
            and parser.finite(stamp) and 0 <= now - stamp < cfg["plan_max_age_seconds"]):
        plan = parser.normalize_plan(value.get("plan"))
        valid_until = stamp + cfg["plan_max_age_seconds"] if plan else None
    quota = desktop_quota.quota_snapshot_base(now=now)
    return {"account_present": bool(identity), "plan": plan, "plan_valid_until": valid_until,
            "remaining": quota["remaining_percent"]}


def _retirements(state, revisions, now, cfg):
    rows={}
    for row in list(state.get('terminal_revisions') or [])+revisions:
        if (isinstance(row,dict) and row.get('status') in {'retracted','fulfilled','uncertain'}
                and parser.finite(row.get('published_at'))
                and 0<=now-row['published_at']<=cfg['max_post_age_days']*86400):
            rows[(row['post_id'],row['kind'])]=row
    return sorted(rows.values(),key=lambda row:row['published_at'])[-MAX_POSTS*2:]


def _events(state, cfg, now, context):
    if not context.get("account_present"):
        return {}, [{"reason": "current_account_unknown"}]
    posts = [p for p in (state.get("posts") or {}).values()
             if isinstance(p, dict) and parser.finite(p.get("published_at"))
             and 0 <= now - p["published_at"] <= cfg["max_post_age_days"] * 86400]
    revisions, rejected = parser.parse_posts(posts, now=now,
        max_horizon_seconds=cfg["max_forecast_days"] * 86400)
    revisions.extend(_retirements(state,revisions,now,cfg))
    # An explicit tombstone or unavailable active source cannot revive from a
    # later cached/overlap timeline. A successful targeted recheck can recover it.
    tombstones = state.get("tombstones") or {}
    for row in revisions:
        if any(ident in tombstones for ident in row["evidence"]["post_ids"]):
            row["status"] = "unavailable"
        if row.get("status") == "pending" and row['plans'] and "all" not in row["plans"] and context.get("plan") not in row["plans"]:
            rejected.append({"post_id": row["post_id"], "reason": "current_plan_unknown" if not context.get("plan") else "plan_not_applicable"})
    return parser.select_events(revisions, now=now, plan=context.get("plan")), rejected


def _snapshot(state, cfg, now, context):
    events, rejected = _events(state, cfg, now, context)
    result = {"schema_version": 1, "enabled": True, "quota_reset": None, "reset_grant": None}
    for kind, row in events.items():
        valid_until = row["target_at"]
        if row['plans'] and "all" not in row["plans"]:
            if not parser.finite(context.get("plan_valid_until")):
                continue
            valid_until = min(valid_until, context["plan_valid_until"])
        if valid_until <= now:
            continue
        result[kind] = {k: row[k] for k in ("kind", "status", "target_at", "expires_at", "post_id", "published_at", "source_url", "confidence")}
        result[kind]["valid_until"] = valid_until
        result[kind].update(time_relation=row.get('time_relation','before'),
                            source_time_zone=row.get('source_time_zone'),
                            plan_scope=row.get('plan_scope','unspecified'),
                            field_sources=row.get('field_sources',{}),
                            evidence_post_ids=row['evidence']['post_ids'])
    return result, rejected


def snapshot(*, now=None, config=None, context=None) -> dict:
    now = time.time() if now is None else now
    cfg = (config or _config()).get("codex_announcements") or policy.DEFAULTS
    empty = {"schema_version": 1, "enabled": False, "quota_reset": None, "reset_grant": None}
    if not cfg.get("enabled"):
        return empty
    try:
        result, _ = _snapshot(_state(cfg), cfg, now, context if context is not None else account_context(cfg, now))
        return result
    except (state_store.StateStoreError, ValueError, TypeError, KeyError):
        return dict(empty, enabled=True)


def diagnostics(*, now=None) -> dict:
    now = time.time() if now is None else now
    config = _config()
    cfg = config.get("codex_announcements") or policy.DEFAULTS
    if not cfg.get("enabled"):
        return {"enabled": False, "state": "disabled", "experimental": True, "provider": "FxEmbed"}
    try:
        state = _state(cfg)
        context = account_context(cfg, now)
        active, rejected = _snapshot(state, cfg, now, context)
        due = policy.decision(state, cfg, now=now, mode=policy.mode_at(config, now),
                              remaining=context["remaining"], events={k: v for k, v in active.items() if k in {"quota_reset", "reset_grant",'reset_unspecified'} and v})
        return {"enabled": True, "experimental": True, "provider": "FxEmbed",
                "state": state.get("last_result", "not_checked"),
                "last_success_at": state.get("last_success_at"), "last_attempt_at": state.get("last_attempt_at"),
                "next_allowed_at": state.get("next_allowed_at"), "schedule": due,
                'auxiliary_sources': sources.diagnostics(state.get('auxiliary_sources',{}),cfg),
                'field_evidence': {k:v.get('field_sources') for k,v in active.items() if isinstance(v,dict)},
                'source_conflicts': _source_conflicts(state,active),
                "snapshot": active, "rejections": (state.get("diagnostics", []) + rejected)[-50:],
                "note": "实验性第三方；无 Key/Cookie，无模型调用；未识别文案不推测；账户变动不作为到账确认。"}
    except (state_store.StateStoreError, ValueError, TypeError, KeyError):
        return {"enabled": True, "state": "state_unavailable", "experimental": True}


class SourceFailure(Exception):
    def __init__(self, reason, *, status=None, headers=None):
        self.reason, self.status, self.headers = reason, status, headers or {}
        self.partial = None
        super().__init__(reason)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise SourceFailure("redirect_rejected")
        # Even same-host redirects would hide extra HTTP requests from the
        # four-request budget. Fail closed rather than following a chain.
        raise SourceFailure("redirect_rejected")


def _request(path, query, timeout):
    if not re.fullmatch(r"/2/(?:profile/[A-Za-z0-9_]{1,15}/statuses|status/[0-9]{5,24})", path):
        raise SourceFailure("invalid_fixed_host_path")
    url = "https://" + HOST + path + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "InkSight-experimental-announcements/1"}, method="GET")
    try:
        with urllib.request.build_opener(_SameHostRedirect()).open(request, timeout=timeout) as response:
            if response.status == 204:
                return 204, None
            if response.status != 200:
                raise SourceFailure("unexpected_http_status", status=response.status)
            headers = dict(response.headers)
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SourceFailure("response_too_large")
            if response.headers.get_content_type() != "application/json":
                raise SourceFailure("non_json_response")
            try:
                body = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise SourceFailure("invalid_json")
            code = body.get("code") if isinstance(body, dict) else None
            if type(code) is not int or code != 200:
                raise SourceFailure("provider_error", status=code, headers=headers)
            return 200, body
    except urllib.error.HTTPError as exc:
        raise SourceFailure("http_error", status=exc.code, headers=dict(exc.headers or {})) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SourceFailure("network_or_timeout") from None


def _retry_at(error, now, cfg, failures):
    headers = {k.lower(): v for k, v in error.headers.items()}
    waits = [now + min(cfg["retry_max_seconds"], cfg["retry_initial_seconds"] * 2 ** min(failures - 1, 10))]
    if error.status in (401, 403):
        waits.append(now + cfg["forbidden_backoff_seconds"])
    retry = headers.get("retry-after")
    if retry:
        try:
            waits.append(now + max(0, float(retry)))
        except ValueError:
            try:
                waits.append(parsedate_to_datetime(retry).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    for key in ("x-ratelimit-reset", "ratelimit-reset"):
        try:
            value = float(headers.get(key, ""))
            if parser.finite(value):
                waits.append(value if value > 1e9 else now + max(0, value))
        except ValueError:
            pass
    return max(w for w in waits if parser.finite(w))


def _collect(state, cfg, now, requester, context=None, auxiliary_requester=None):
    posts = dict(state.get("posts") or {})
    tombstones = dict(state.get("tombstones") or {})
    diagnostics, calls, seen_ids = [], 0, set()
    watermark = state.get("watermark") or 0
    overlap_due = not state.get("last_overlap_at") or now - state["last_overlap_at"] >= cfg["overlap_recheck_minutes"] * 60
    auxiliary = sources.collect(state.get('auxiliary_sources'),cfg,now,requester=auxiliary_requester)

    def checkpoint(error):
        # A later network failure must not discard an already verified own
        # cancellation/correction. Keep partial evidence, never success slots,
        # watermark or source-success timestamps from an incomplete operation.
        keep = sorted((p for p in posts.values() if parser.finite(p.get("published_at"))
                       and 0 <= now - p["published_at"] <= cfg["max_post_age_days"] * 86400),
                      key=lambda p: (p["published_at"], int(p["id"])), reverse=True)[:MAX_POSTS]
        analysis, rejected = parser.parse_posts(keep, now=now,
            max_horizon_seconds=cfg["max_forecast_days"] * 86400)
        error.partial = {"posts": {p["id"]: p for p in keep},
                         'terminal_revisions':_retirements(state,analysis,now,cfg),
                         'auxiliary_sources': auxiliary,
                         "tombstones": {k: v for k, v in tombstones.items() if k in {p["id"] for p in keep}},
                         "analysis": analysis[-MAX_POSTS * 2:], "diagnostics": (diagnostics + rejected)[-50:]}
        return error

    def get(path, query):
        nonlocal calls
        if calls >= MAX_REQUESTS:
            raise checkpoint(SourceFailure("request_budget_exhausted"))
        calls += 1
        try:
            return requester(path, query, cfg["timeout_seconds"])
        except SourceFailure as error:
            raise checkpoint(error)

    def merge(body, *, checked=False):
        try:
            fresh, rejected = parser.normalize_posts(body, cfg["target_handle"], now,
                max_posts=MAX_POSTS, max_age_seconds=cfg["max_post_age_days"] * 86400,
                author_id=cfg["target_user_id"])
        except ValueError:
            raise checkpoint(SourceFailure("invalid_timeline_schema")) from None
        diagnostics.extend(rejected)
        for row in fresh:
            seen_ids.add(row["id"])
            row["collected_at"] = now
            previous = posts.get(row["id"])
            if previous and previous.get('verified_at') and not checked and previous.get('text')!=row['text']:
                diagnostics.append({'post_id':row['id'],'reason':'timeline_targeted_conflict_kept_verified'})
                continue
            if previous and previous.get("text") != row["text"]:
                row["previous_text"] = previous.get("text", "")[:4000]
                row["edited_observed_at"] = now
            elif previous:
                for key in ("previous_text", "edited_observed_at"):
                    if key in previous:
                        row[key] = previous[key]
            posts[row["id"]] = row
            if checked:
                row['verified_at']=now
                tombstones.pop(row["id"], None)
            elif previous and previous.get('verified_at'):
                row['verified_at']=previous['verified_at']
        return fresh

    path = "/2/profile/" + cfg["target_handle"] + "/statuses"
    query = {"count": cfg["page_size"], "with_replies": "1"}
    if watermark:
        query["since"] = max(0, int(watermark) - 1)
    status, body = get(path, query)
    newest = []
    if status == 200:
        newest = merge(body)
    elif status != 204:
        raise checkpoint(SourceFailure("timeline_failed", status=status))
    # Revisit a bounded overlap independently of the incremental 204 result.
    # A single page is enough for overlap; no unbounded cursor traversal.
    if overlap_due and watermark and calls < MAX_REQUESTS:
        status, overlap = get(path, {**query, "since": max(0, int(watermark) - cfg["overlap_hours"] * 3600)})
        if status == 200:
            merge(overlap)
        elif status != 204:
            raise checkpoint(SourceFailure("overlap_failed", status=status))
    elif status == 200:
        cursor = (body.get("cursor") or {}).get("bottom") if isinstance(body.get("cursor"), dict) else None
        pages, used = 1, set()
        while cursor and pages < cfg["max_pages"] and calls < 2:
            if not isinstance(cursor, str) or len(cursor) > 512 or cursor in used or any(ord(c) < 32 for c in cursor):
                diagnostics.append({"reason": "invalid_or_repeated_cursor"}); break
            used.add(cursor)
            status, page = get(path, {"count": cfg["page_size"], "with_replies": "1", "cursor": cursor})
            if status != 200:
                raise checkpoint(SourceFailure("page_failed", status=status))
            merge(page); pages += 1
            cursor = (page.get("cursor") or {}).get("bottom") if isinstance(page.get("cursor"), dict) else None
    # Verify up to two active forecast sources even when incremental has no
    # newer posts; timeline absence itself is never treated as deletion.
    revisions, _ = parser.parse_posts(list(posts.values()), now=now,
        max_horizon_seconds=cfg["max_forecast_days"] * 86400)
    revisions.extend(_retirements(state,revisions,now,cfg))
    selected = parser.select_events(revisions, now=now, plan=(context or {}).get("plan"))
    active_sources = list(dict.fromkeys(r["post_id"] for r in selected.values()))[:2]
    for ident in active_sources:
        if ident in seen_ids or calls >= MAX_REQUESTS:
            continue
        try:
            code, checked = get("/2/status/" + ident, {})
        except SourceFailure as error:
            if error.status == 404:
                tombstones[ident] = {"at": now, "reason": "source_unavailable"}
                diagnostics.append({"post_id": ident, "reason": "source_unavailable"})
                continue
            raise
        row = checked.get("status") if isinstance(checked, dict) else None
        if code == 200 and isinstance(row, dict) and row.get("type") == "tombstone":
            tombstones[ident] = {"at": now, "reason": str(row.get("reason") or "unavailable")[:32]}
        elif code == 200 and isinstance(row, dict) and str(row.get("id")) == ident:
            merge({"code": 200, "results": [row]},checked=True)
        else:
            raise checkpoint(SourceFailure("invalid_post_check_schema"))
    # Auxiliary links are hints only. Reuse the same four primary GET budget;
    # verify own author + stable ID + timestamp, then at most one missing own
    # parent. Historic/foreign/guessed third-party dates cannot create events.
    candidates=list(dict.fromkeys(p['parent_id'] for p in sorted(posts.values(),key=lambda p:p['published_at'],reverse=True)
                                 if p.get('parent_id') and p['parent_id'] not in posts))[:2]
    for name,row in auxiliary.items():
        if row.get('state')=='current' and now-row.get('last_success_at',0)<cfg['max_post_age_days']*86400:
            for ident in row.get('post_ids',[]):
                if ident not in candidates and ident not in posts: candidates.append(ident)
    for ident in candidates[:2]:
        if calls>=MAX_REQUESTS: break
        try:
            code,body=get('/2/status/'+ident,{})
            row=body.get('status') if isinstance(body,dict) else None
            if code!=200 or not isinstance(row,dict) or str(row.get('id'))!=ident:
                diagnostics.append({'post_id':ident,'reason':'auxiliary_primary_unverified'});continue
            fresh=merge({'code':200,'results':[row]},checked=True)
            for post in fresh:
                parent=post.get('parent_id')
                if parent and parent not in posts and calls<MAX_REQUESTS:
                    parent_code,parent_body=get('/2/status/'+parent,{})
                    parent_row=parent_body.get('status') if isinstance(parent_body,dict) else None
                    if parent_code==200 and isinstance(parent_row,dict) and str(parent_row.get('id'))==parent:
                        merge({'code':200,'results':[parent_row]},checked=True)
        except SourceFailure as error:
            diagnostics.append({'post_id':ident,'reason':'auxiliary_primary_unverified','http_status':error.status})
            # A failed hint lookup is not deletion and does not erase verified
            # evidence or turn a successful main timeline into an empty result.
            if error.status==429: raise
    keep = sorted((p for p in posts.values() if 0 <= now - p["published_at"] <= cfg["max_post_age_days"] * 86400),
                  key=lambda p: (p["published_at"], int(p["id"])), reverse=True)[:MAX_POSTS]
    revisions, parse_diagnostics = parser.parse_posts(keep, now=now,
        max_horizon_seconds=cfg["max_forecast_days"] * 86400)
    return {"posts": {p["id"]: p for p in keep}, "analysis": revisions[-MAX_POSTS * 2:],
            'terminal_revisions':_retirements(state,revisions,now,cfg),
            'auxiliary_sources':auxiliary,
            "tombstones": {k: v for k, v in tombstones.items() if k in {p["id"] for p in keep}},
            "diagnostics": (diagnostics + parse_diagnostics)[-50:], "requests": calls,
            "watermark": max([watermark] + [p["published_at"] for p in newest]),
            "last_overlap_at": now if overlap_due else state.get("last_overlap_at", 0)}


def _source_conflicts(state,active):
    result=[]
    for event in active.values():
        if not isinstance(event,dict):continue
        for name,source in (state.get('auxiliary_sources') or {}).items():
            for ident,hint in (source.get('field_hints') or {}).items():
                if ident not in event.get('evidence_post_ids',[]):continue
                result.append({'source':name,'post_id':ident,'primary_time_zone':event.get('source_time_zone'),
                    'primary_target_at':event['target_at'],'auxiliary_clocks_unverified':hint.get('clocks',[]),
                    'auxiliary_zones_unverified':hint.get('zones',[]),'policy':'primary_literal_time_wins',
                    'needs_manual_comparison':True,'event_fields_overridden':False})
    return result


def tick(*, now=None, config=None, context=None, requester=None, auxiliary_requester=None) -> dict:
    """One bounded operation; safe on startup, wake, tick and concurrent triggers."""
    if not _poll_lock.acquire(blocking=False):
        return {"queried": False, "reason": "concurrent_coalesced"}
    try:
        with desktop_service.gate.work() as admitted:
            if not admitted:
                return {"queried": False, "reason": "paused"}
            config = config or _config()
            cfg = config.get("codex_announcements") or policy.DEFAULTS
            if not cfg.get("enabled"):
                return {"queried": False, "reason": "disabled"}
            with state_store.file_lock(STATE_FILE, operation="announcement-poll"):
                current = time.time() if now is None else now
                state = _state(cfg)
                context = context if context is not None else account_context(cfg, current)
                events, _ = _events(state, cfg, current, context)
                due = policy.decision(state, cfg, now=current, mode=policy.mode_at(config, current),
                                      remaining=context.get("remaining"), events=events)
                if due["coalesce"] and state.get("last_attempt_at", 0) <= state.get("last_success_at", 0):
                    for slot in due["slots"]:
                        state["slot_success"][slot] = state["last_success_at"]
                    state_store.write_json(STATE_FILE, state)
                    return {"queried": False, "reason": "successful_boundary_coalesced"}
                if not due["due"]:
                    return {"queried": False, "reason": due["reason"]}
                state.update(last_attempt_at=current, inflight_until=current + MAX_REQUESTS * cfg["timeout_seconds"] +
                             4*cfg.get('auxiliary_timeout_seconds',6) + 15)
                state_store.write_json(STATE_FILE, state)
                try:
                    collected = _collect(state, cfg, current, requester or _request, context,auxiliary_requester)
                    finished = time.time() if now is None else now
                    state.update(collected, last_success_at=finished, next_allowed_at=finished + 60, pending_slots=[],
                                 failures=0, inflight_until=0, last_result="current")
                    for slot in set(due["slots"] + policy.relevant_slots(cfg, finished)):
                        state["slot_success"][slot] = finished
                    # Keep 14 days of success slots, keyed by Beijing date.
                    cutoff = datetime.fromtimestamp(finished - 14 * 86400, policy.BJ).strftime("%Y-%m-%d")
                    state["slot_success"] = {k: v for k, v in state["slot_success"].items() if k[:10] >= cutoff}
                    state_store.write_json(STATE_FILE, state)
                    return {"queried": True, "ok": True, "requests": collected["requests"]}
                except (SourceFailure, ValueError, TypeError, KeyError) as error:
                    failure = error if isinstance(error, SourceFailure) else SourceFailure("invalid_provider_schema")
                    if failure.partial:
                        state.update(failure.partial)
                    state["failures"] = min(1000, state.get("failures", 0) + 1)
                    state.update(inflight_until=0, last_result=failure.reason,
                                 last_http_status=failure.status,
                                 pending_slots=due["slots"],
                                 next_allowed_at=_retry_at(failure, current, cfg, state["failures"]))
                    state_store.write_json(STATE_FILE, state)
                    return {"queried": True, "ok": False, "reason": failure.reason, "status": failure.status}
    except (state_store.StateStoreError, ValueError, TypeError, KeyError):
        return {"queried": False, "reason": "state_or_configuration_unavailable"}
    finally:
        _poll_lock.release()


def _loop():
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            # Never break unrelated source collectors or emit public-post text.
            pass
        _stop.wait(TICK_SECONDS)


def start_poller():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="codex-announcement-poller", daemon=True)
        _thread.start()
    return _thread


def stop_poller():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=1)
