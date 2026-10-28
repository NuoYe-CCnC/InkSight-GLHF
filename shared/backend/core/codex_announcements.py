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
from . import codex_announcement_view as view
from .codex_announcement_network import failure_reason, summary

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
            "last_overlap_at": 0, "watermark": 0, "diagnostics": [], "series_records": [], "event_history": {}}


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
    if value.get('history_schema_version')!=1:
        value['_legacy_recognized_post_ids']=list(dict.fromkeys(row.get('post_id') for row in value.get('analysis',[])
            if isinstance(row,dict) and row.get('status') in {'pending','expired','fulfilled','retracted'}))[-MAX_POSTS:]
        value['history_schema_version']=1
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
             and 0 <= now - p["published_at"] <= max(14,cfg["max_post_age_days"]) * 86400
             and p.get("author")==cfg["target_handle"].lower() and p.get("author_id")==cfg["target_user_id"]]
    revisions, rejected = parser.parse_posts(posts, now=now,
        max_horizon_seconds=cfg["max_forecast_days"] * 86400, series_records=state.get("series_records"))
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


def _snapshot(state, cfg, now, context, *, show_reset=True):
    events,rejected=_events(state,cfg,now,context)
    result=dict(schema_version=2,enabled=True,quota_reset=None,reset_grant=None,reset_unspecified=None,
                selected_event=None,canonical_source_url=None,target_author=cfg['target_handle'].lower(),capabilities=['exact','date_range','unknown','canonical_source_v1'])
    for kind,row in events.items():
        valid_until=row['expires_at']
        if row['plans'] and 'all' not in row['plans']:
            if not parser.finite(context.get('plan_valid_until')):continue
            valid_until=min(valid_until,context['plan_valid_until'])
        if valid_until<=now:continue
        keys=('kind','status','target_at','expires_at','post_id','published_at','source_url','source_author','confidence',
              'event_id','revision','time_type','range_start_at','range_end_at','display_date','time_relation','source_time_zone','time_basis','plan_scope')
        event={k:row.get(k) for k in keys}
        event.update(valid_until=valid_until,expiry_origin_at=row.get('expiry_origin_at',row['original_published_at']),
                     canonical_source_url=parser.canonical_source_url(row['source_url'],cfg['target_handle'],row['post_id']),
                     field_sources=row.get('field_sources',{}),evidence_post_ids=row['evidence']['post_ids'])
        if view.valid_event(event,kind,2,now):result[kind]=event
    selected=min((e for k,e in result.items() if k in view.KINDS and e and (show_reset or k!='reset_grant')),
        key=parser.event_sort_key,default=None)
    result.update(selected_event=selected,canonical_source_url=selected['canonical_source_url'] if selected else None)
    return result,rejected


def snapshot(*, now=None, config=None, context=None) -> dict:
    now = time.time() if now is None else now
    cfg = (config or _config()).get("codex_announcements") or policy.DEFAULTS
    empty = {"schema_version": 2, "enabled": False, "quota_reset": None, "reset_grant": None, "selected_event": None, "canonical_source_url": None}
    if not cfg.get("enabled"):
        return empty
    try:
        result, _ = _snapshot(_state(cfg), cfg, now, context if context is not None else account_context(cfg, now),
            show_reset=(config or _config()).get("panel_display",{}).get("show_reset_opportunities",True))
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
        active, rejected = _snapshot(state, cfg, now, context,show_reset=config.get("panel_display",{}).get("show_reset_opportunities",True))
        due = policy.decision(state, cfg, now=now, mode=policy.mode_at(config, now),
                              remaining=context["remaining"], events={k: v for k, v in active.items() if k in {"quota_reset", "reset_grant",'reset_unspecified'} and v})
        return {"enabled": True, "experimental": True, "provider": "FxEmbed",
                "state": state.get("last_result", "not_checked"),"target_handle":cfg["target_handle"],
                "last_success_at": state.get("last_success_at"), "last_attempt_at": state.get("last_attempt_at"),
                "next_allowed_at": state.get("next_allowed_at"), "schedule": due,
                'auxiliary_sources': sources.diagnostics(state.get('auxiliary_sources',{}),cfg),
                'field_evidence': {k:v.get('field_sources') for k,v in active.items() if isinstance(v,dict)},
                'source_conflicts': _source_conflicts(state,active),
                "selected_event": active.get('selected_event'),
                'series_records':state.get('series_records',[]),
                'event_history':list((state.get('event_history') or {}).values())[-50:],
                'recent_candidates':_candidate_diagnostics(state,active,now,cfg),
                'next_query_at':_next_query_time(config,state,cfg,now,context,active),
                'last_error':state.get('last_error'),'request_trace':state.get('request_trace',[]),
                'publication':_publication_diagnostics(active),
                "snapshot": active, "rejections": (state.get("diagnostics", []) + rejected)[-50:],
                "note": "实验性第三方；无 Key/Cookie，无模型调用；未识别文案不推测；账户变动不作为到账确认。"}
    except (state_store.StateStoreError, ValueError, TypeError, KeyError):
        return {"enabled": True, "state": "state_unavailable", "experimental": True}


def _update_event_history(state,cfg,now,context,config):
    history=state.setdefault('event_history',{})
    for event in state.get('analysis',[]):
        key=event.get('event_id',event.get('root_id',''))+':'+event.get('revision',event['post_id'])
        entry=history.setdefault(key,dict(event_id=event.get('event_id'),revision=event.get('revision'),
            original_published_at=event.get('original_published_at'),published_at=event['published_at'],
            first_recognized_at=None if event['post_id'] in state.get('_legacy_recognized_post_ids',[]) else now,first_selected_at=None,first_seen_at=None))
        post=state.get('posts',{}).get(event['post_id'],{})
        entry.update(first_seen_at=post.get('first_seen_at'),last_seen_at=post.get('last_seen_at'),
                     status=event.get('status'),time_type=event.get('time_type'),source_url=event.get('source_url'))
    active,_=_snapshot(state,cfg,now,context,show_reset=config.get('panel_display',{}).get('show_reset_opportunities',True))
    selected=active.get('selected_event')
    if selected:
        key=selected['event_id']+':'+selected['revision']
        if key in history and history[key]['first_selected_at'] is None:history[key]['first_selected_at']=now
    state['event_history']=dict(list(history.items())[-200:])


def _candidate_diagnostics(state,snapshot,now,cfg=None):
    selected=snapshot.get('selected_event') or {}
    cfg=cfg or policy.DEFAULTS
    current,decisions=parser.parse_posts(list(state.get('posts',{}).values()),now=now,
        max_horizon_seconds=cfg['max_forecast_days']*86400,series_records=state.get('series_records'))
    reasons={row.get('post_id'):row.get('reason') for row in decisions}
    analyses={row['post_id']:row for row in current}
    rows=[]
    for post in sorted(state.get('posts',{}).values(),key=lambda p:p['published_at'],reverse=True)[:30]:
        event=analyses.get(post['id']);reason=reasons.get(post['id'],'not_an_event')
        is_selected=bool(event and selected.get('event_id')==event.get('event_id') and selected.get('revision')==event.get('revision'))
        outcome='selected' if is_selected else ('expired' if event and event.get('expires_at',0)<=now else event.get('status') if event else reason)
        rows.append(dict(post_id=post['id'],source_url=post['url'],text=post['text'][:1000],published_at=post['published_at'],
            first_seen_at=post.get('first_seen_at'),last_seen_at=post.get('last_seen_at'),selected=is_selected,
            first_recognized_at=next((h.get('first_recognized_at') for h in (state.get('event_history') or {}).values() if event and h.get('event_id')==event.get('event_id') and h.get('revision')==event.get('revision')),None),
            outcome=outcome,reason=reason,time_basis=event.get('time_basis') if event else None,
            display_label=' / '.join(view.display_lines(event)) if event and event.get('time_type') else None,
            timing_evidence='first_seen_unknown' if post.get('first_seen_at') is None else
                'collected_after_deadline' if event and event.get('target_at') and post['first_seen_at']>=event['target_at'] else 'collection_recorded'))
    return rows


def _next_query_time(config,state,cfg,now,context,snapshot):
    """Read-only estimate under current settings; re-evaluated after changes."""
    from datetime import timedelta
    mode=policy.mode_at(config,now)
    events={k:e for k,e in snapshot.items() if k in view.KINDS and e}
    due=policy.decision(state,cfg,now=now,mode=mode,remaining=context.get('remaining'),events=events)
    block=due['next_allowed_at']
    if due['due']:return now
    date=datetime.fromtimestamp(now,policy.BJ).replace(hour=0,minute=0,second=0,microsecond=0)
    slots=[]
    for offset in range(3):
        for clock in cfg['night_slots']:
            h,m=map(int,clock.split(':'))
            at=(date+timedelta(days=offset,hours=h,minutes=m)).timestamp()
            if at>=max(now,block):slots.append(at)
    if due['slots']:return max(now,block)
    interval=due['interval_seconds']
    if interval is not None:
        at=max(now,block,(state.get('last_success_at') or 0)+interval)
        if policy.mode_at(config,at) in {'active','light'}:slots.append(at)
    return min(slots) if slots else None


def _publication_diagnostics(snapshot):
    # GET readback is publication evidence, never device ACK. This read has no
    # side effects and cannot trigger publication or an upstream refresh.
    selected=snapshot.get('selected_event') or {}
    try:
        path=Path(__file__).resolve().parents[2]/'tools/.cloud_publish_state.json'
        value=json.loads(path.read_text())
        rows=list((value.get('_desktop_publications') or {}).values())
        found=[]
        for row in rows:
            doc=row.get('document',{})
            ann=(((doc.get('screen') or {}).get('pages') or {}).get('ai') or {}).get('announcements') or {}
            event=ann.get('selected_event') or {}
            if event.get('event_id')==selected.get('event_id') and event.get('revision')==selected.get('revision') and selected:
                found.append(dict(at=row.get('last_success_at'),event_id=event['event_id'],revision=event['revision']))
        return dict(event_history=list((value.get('_announcement_history') or {}).values())[-50:],matching_verified_publications=found,device_ack=False,note='云端回读不代表设备实屏送达')
    except (OSError,ValueError,TypeError,AttributeError):return dict(matching_verified_publications=[],device_ack=False)


class SourceFailure(Exception):
    def __init__(self, reason, *, status=None, headers=None, detail=None, stage="primary", elapsed_ms=None):
        self.reason, self.status, self.headers = reason, status, headers or {}
        self.partial = None
        self.detail, self.stage, self.elapsed_ms = detail, stage, elapsed_ms
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
    started=time.monotonic()
    stage="connect"
    try:
        with urllib.request.build_opener(_SameHostRedirect()).open(request, timeout=timeout) as response:
            if response.status == 204:
                return 204, None
            if response.status != 200:
                raise SourceFailure("unexpected_http_status", status=response.status)
            headers = dict(response.headers)
            stage="read"
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SourceFailure("response_too_large")
            if response.headers.get_content_type() != "application/json":
                raise SourceFailure("non_json_response")
            try:
                body = json.loads(data.decode("utf-8",errors="strict"))
            except UnicodeDecodeError:
                raise SourceFailure("decode",stage="decode") from None
            except ValueError:
                raise SourceFailure("invalid_json",stage="parse")
            code = body.get("code") if isinstance(body, dict) else None
            if type(code) is not int or code != 200:
                raise SourceFailure("provider_error", status=code, headers=headers)
            return 200, body
    except urllib.error.HTTPError as exc:
        raise SourceFailure("http_error", status=exc.code, headers=dict(exc.headers or {}),detail=summary(exc),stage=stage,elapsed_ms=round((time.monotonic()-started)*1000,1)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason=failure_reason(exc)
        if reason=="timeout":reason="read_timeout" if stage=="read" else "connect_timeout"
        raise SourceFailure(reason,detail=summary(exc),stage=stage,elapsed_ms=round((time.monotonic()-started)*1000,1)) from None


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


def _collect(state, cfg, now, requester, context=None, auxiliary_requester=None, auxiliary_checkpoint=None):
    posts = dict(state.get("posts") or {})
    tombstones = dict(state.get("tombstones") or {})
    diagnostics, calls, seen_ids = [], 0, set()
    request_trace=[]
    watermark = state.get("watermark") or 0
    overlap_due = not state.get("last_overlap_at") or now - state["last_overlap_at"] >= cfg["overlap_recheck_minutes"] * 60
    auxiliary = sources.collect(state.get('auxiliary_sources'),cfg,now,requester=auxiliary_requester,checkpoint=auxiliary_checkpoint)

    def checkpoint(error):
        # A later network failure must not discard an already verified own
        # cancellation/correction. Keep partial evidence, never success slots,
        # watermark or source-success timestamps from an incomplete operation.
        keep = sorted((p for p in posts.values() if parser.finite(p.get("published_at"))
                       and 0 <= now - p["published_at"] <= max(14,cfg["max_post_age_days"]) * 86400),
                      key=lambda p: (p["published_at"], int(p["id"])), reverse=True)[:MAX_POSTS]
        analysis, rejected = parser.parse_posts(keep, now=now,
            max_horizon_seconds=cfg["max_forecast_days"] * 86400, series_records=state.get("series_records"))
        error.partial = {"posts": {p["id"]: p for p in keep},
                         'terminal_revisions':_retirements(state,analysis,now,cfg),
                         'auxiliary_sources': auxiliary,
                         "tombstones": {k: v for k, v in tombstones.items() if k in ({p["id"] for p in keep}|{r["anchor_post_id"] for r in state.get("series_records",[])})},
                         "request_trace":request_trace,"analysis": analysis[-MAX_POSTS * 2:], "diagnostics": (diagnostics + rejected)[-50:]}
        return error

    def get(path, query):
        nonlocal calls
        if calls >= MAX_REQUESTS:
            raise checkpoint(SourceFailure("request_budget_exhausted"))
        calls += 1
        started=time.monotonic()
        operation='timeline' if '/profile/' in path else 'status'
        try:
            result=requester(path, query, cfg['timeout_seconds'])
            request_trace.append(dict(stage='primary',operation=operation,status=result[0],elapsed_ms=round((time.monotonic()-started)*1000,1)))
            return result
        except SourceFailure as error:
            if error.elapsed_ms is None:error.elapsed_ms=round((time.monotonic()-started)*1000,1)
            request_trace.append(dict(stage=error.stage,operation=operation,status=error.status,reason=error.reason,elapsed_ms=error.elapsed_ms,summary=error.detail))
            raise checkpoint(error)

    def merge(body, *, checked=False):
        try:
            fresh, rejected = parser.normalize_posts(body, cfg["target_handle"], now,
                max_posts=MAX_POSTS, max_age_seconds=max(14,cfg["max_post_age_days"]) * 86400,
                author_id=cfg["target_user_id"])
        except ValueError:
            raise checkpoint(SourceFailure("invalid_timeline_schema")) from None
        diagnostics.extend(rejected)
        for row in fresh:
            seen_ids.add(row["id"])
            row["collected_at"] = now  # Legacy last-collection field, never first-seen evidence.
            previous = posts.get(row["id"])
            row['first_seen_at']=previous.get('first_seen_at') if previous else now
            row['last_seen_at']=now
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
    if not state.get("series_records") and not watermark:query["since"]=int(now-14*86400)
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
        max_horizon_seconds=cfg["max_forecast_days"] * 86400, series_records=state.get("series_records"))
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
    keep = sorted((p for p in posts.values() if 0 <= now - p["published_at"] <= max(14,cfg["max_post_age_days"]) * 86400),
                  key=lambda p: (p["published_at"], int(p["id"])), reverse=True)[:MAX_POSTS]
    series_records,_=parser.series_state(keep,state.get("series_records"))
    revisions, parse_diagnostics = parser.parse_posts(keep, now=now,
        max_horizon_seconds=cfg["max_forecast_days"] * 86400, series_records=state.get("series_records"))
    return {"request_trace":request_trace,"series_records":series_records,"posts": {p["id"]: p for p in keep}, "analysis": revisions[-MAX_POSTS * 2:],
            'terminal_revisions':_retirements(state,revisions,now,cfg),
            'auxiliary_sources':auxiliary,
            "tombstones": {k: v for k, v in tombstones.items() if k in ({p["id"] for p in keep}|{r["anchor_post_id"] for r in state.get("series_records",[])})},
            "diagnostics": (diagnostics + parse_diagnostics)[-50:], "requests": calls,
            "watermark": max([watermark] + [p["published_at"] for p in newest]),
            "last_overlap_at": now if overlap_due else state.get("last_overlap_at", 0)}


def _source_conflicts(state,active):
    result=[]
    for event in active.values():
        if not isinstance(event,dict) or event.get('kind') not in view.KINDS:continue
        for name,source in (state.get('auxiliary_sources') or {}).items():
            for ident,hint in (source.get('field_hints') or {}).items():
                if ident not in event.get('evidence_post_ids',[]):continue
                result.append({'source':name,'post_id':ident,'primary_time_zone':event.get('source_time_zone'),
                    'primary_target_at':event.get('target_at'),'auxiliary_clocks_unverified':hint.get('clocks',[]),
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
                mode=policy.mode_at(config,current)
                auxiliary_due=sources.due(state.get('auxiliary_sources'),cfg,current,allow_retry=mode!='night')
                if not due['due']:
                    if auxiliary_due and mode in {'active','light'} and current>=state.get('inflight_until',0):
                        state['inflight_until']=current+4*cfg.get('auxiliary_timeout_seconds',6)+15
                        state_store.write_json(STATE_FILE,state)
                        def auxiliary_checkpoint(result):
                            state['auxiliary_sources']=result
                            state_store.write_json(STATE_FILE,state)
                        state['auxiliary_sources']=sources.collect(state.get('auxiliary_sources'),cfg,current,requester=auxiliary_requester,checkpoint=auxiliary_checkpoint)
                        state['inflight_until']=0
                        state_store.write_json(STATE_FILE,state)
                        return dict(queried=True,ok=True,requests=0,reason='auxiliary_only')
                    return {"queried": False, "reason": due["reason"]}
                state.update(last_attempt_at=current, inflight_until=current + MAX_REQUESTS * cfg["timeout_seconds"] +
                             4*cfg.get('auxiliary_timeout_seconds',6) + 15)
                state_store.write_json(STATE_FILE, state)
                try:
                    def primary_auxiliary_checkpoint(result):
                        state['auxiliary_sources']=result
                        state_store.write_json(STATE_FILE,state)
                    collected = _collect(state, cfg, current, requester or _request, context,auxiliary_requester,primary_auxiliary_checkpoint)
                    finished = time.time() if now is None else now
                    state.update(collected, last_success_at=finished, next_allowed_at=finished + 60, pending_slots=[],
                                 failures=0, inflight_until=0, last_result="current")
                    for slot in set(due["slots"] + policy.relevant_slots(cfg, finished)):
                        state["slot_success"][slot] = finished
                    # Keep 14 days of success slots, keyed by Beijing date.
                    cutoff = datetime.fromtimestamp(finished - 14 * 86400, policy.BJ).strftime("%Y-%m-%d")
                    state["slot_success"] = {k: v for k, v in state["slot_success"].items() if k[:10] >= cutoff}
                    _update_event_history(state,cfg,finished,context,config)
                    state_store.write_json(STATE_FILE, state)
                    return {"queried": True, "ok": True, "requests": collected["requests"]}
                except (SourceFailure, ValueError, TypeError, KeyError) as error:
                    failure = error if isinstance(error, SourceFailure) else SourceFailure("invalid_provider_schema")
                    if failure.partial:
                        state.update(failure.partial)
                    state["failures"] = min(1000, state.get("failures", 0) + 1)
                    state.update(last_error=dict(at=current,reason=failure.reason,stage=failure.stage,status=failure.status,summary=failure.detail,elapsed_ms=failure.elapsed_ms),inflight_until=0, last_result=failure.reason,
                                 last_http_status=failure.status,
                                 pending_slots=due["slots"],
                                 next_allowed_at=_retry_at(failure, current, cfg, state["failures"]))
                    _update_event_history(state,cfg,current,context,config)
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
