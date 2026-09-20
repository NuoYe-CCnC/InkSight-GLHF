"""Configurable, restart-safe Beijing-time news digest scheduler.

Schedule identity is ``date|stable-id``: editing a title or time never causes a
second paid issue. Only the current Beijing-time issue window may run. Workday
windows end at the next issue (or 22:00); rest-day windows end at the next issue
(or 20:00). Free RSS pre-collection occurs shortly before an issue without
calling the model.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import news_brief as brief
from . import news_budget as budget
from . import news_calendar as cal
from . import news_digest as digest
from . import state_store

logger = logging.getLogger(__name__)
_STATE = state_store.state_path("news_schedule_state.json")
AUTHORIZED_FILE = state_store.state_path("news_authorized.json")
COLLECT_MIN_GAP_S = 600
_LOG_KEEP = 300
_DONE_KEEP = 180


def configure_state_file(path) -> None:
    global _STATE
    _STATE = Path(path)


def authorized() -> bool:
    return credential_state()["state"] == "active"


def credential_state() -> dict:
    """Separate secret presence, validation state, and explicit enablement."""
    key_present = bool(brief._env_key())
    value, error = state_store.read_json(AUTHORIZED_FILE)
    auth = value if isinstance(value, dict) and not error else {}
    declared = str(auth.get("key_status") or auth.get("status") or "").lower()
    if not key_present:
        return {"state": "missing", "key_present": False, "reason": "api-key-missing"}
    if declared in {"auth_failure", "invalid", "revoked"}:
        return {"state": "auth_failure", "key_present": True, "reason": declared}
    if declared in {"temporary_failure", "network", "rate_limited"}:
        return {"state": "temporary_failure", "key_present": True, "reason": declared}
    if declared in {"pending", "pending_validation", "unverified"}:
        return {"state": "pending_validation", "key_present": True, "reason": declared}
    if auth.get("authorized") is False:
        return {"state": "disabled", "key_present": True,
                "reason": str(auth.get("reason") or "explicit-or-legacy-disable")}
    if auth.get("authorized") is True and auth.get("key_configured") is True:
        return {"state": "active", "key_present": True, "reason": "validated-enabled"}
    return {"state": "pending_validation", "key_present": True,
            "reason": "key-present-not-validated"}


def _config() -> dict:
    try:
        from .operator_config import load_effective
        return dict(load_effective().config.get("news_digest") or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NEWS] operator config unavailable, using safe defaults: %s", exc)
        from .operator_config import DEFAULT_CONFIG
        return dict(DEFAULT_CONFIG["news_digest"])


def enabled_source_ids(cfg: dict | None = None) -> list[str]:
    """Resolve the effective source allow-list, including legacy snapshots."""
    active = cfg or _config()
    switches = active.get("source_enabled")
    if isinstance(switches, dict):
        return [source_id for source_id in brief.SOURCE_IDS
                if switches.get(source_id) is True]
    return brief.normalize_source_ids(active.get("sources") or [])


def _schedules(now: datetime, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or _config()
    if not cfg.get("enabled", True):
        return []
    actual = "workday" if cal.is_workday(now) else "restday"
    rows = []
    for item in cfg.get("schedules") or []:
        if not item.get("enabled", False):
            continue
        day_types = item.get("day_types") or []
        if "all" not in day_types and actual not in day_types:
            continue
        rows.append(dict(item))
    rows = sorted(rows, key=lambda row: (row.get("time", "00:00"), row.get("id", "")))
    # The historical default used the stable id ``morning`` for both day types.
    # Preserve that ledger identity while presenting the approved rest-day title.
    if actual == "restday" and len(rows) == 1 and rows[0].get("id") == "morning":
        rows[0]["label"] = "科技 / AI 日报"
    return rows


def _load() -> dict:
    value, error = state_store.read_json(_STATE)
    if isinstance(value, dict) and not error:
        value.setdefault("done", {})
        value.setdefault("log", [])
        value.setdefault("attempts", {})
        value.setdefault("model_calls", {})
        value.setdefault("issue_configs", {})
        value.setdefault("inflight", {})
        value.setdefault("aliases", {})
        value.setdefault("last_collect", 0)
        return value
    clean = {"done": {}, "log": [], "attempts": {}, "model_calls": {},
             "issue_configs": {}, "inflight": {}, "aliases": {}, "last_collect": 0}
    if error == "missing":
        return clean  # normal first install; writes atomically at end of first tick
    clean["_state_error"] = error or "corrupt:root-not-object"
    return clean


def _save(value: dict) -> None:
    value["log"] = (value.get("log") or [])[-_LOG_KEEP:]
    done = value.get("done") or {}
    if len(done) > _DONE_KEEP:
        done = dict(sorted(done.items())[-_DONE_KEEP:])
    value["done"] = done
    calls = value.get("model_calls") or {}
    if len(calls) > _DONE_KEEP:
        calls = dict(sorted(calls.items())[-_DONE_KEEP:])
    value["model_calls"] = calls
    issue_configs = value.get("issue_configs") or {}
    if len(issue_configs) > _DONE_KEEP:
        issue_configs = dict(sorted(issue_configs.items())[-_DONE_KEEP:])
    value["issue_configs"] = issue_configs
    inflight = value.get("inflight") or {}
    if len(inflight) > 16:
        inflight = dict(sorted(inflight.items(),
                               key=lambda item: float((item[1] or {}).get("started_at") or 0))[-16:])
    value["inflight"] = inflight
    value.pop("_state_error", None)
    state_store.write_json(_STATE, value)


def _log(state: dict, at: int, note: str, period: str | None = None) -> None:
    row = {"at": at, "note": note}
    if period:
        row["period"] = period
    state.setdefault("log", []).append(row)


def _schedule_for(period: str, now: datetime) -> dict:
    for row in _schedules(now):
        if row.get("id") == period:
            return row
    # Compatibility for direct tests/callers using the historical ids.
    h, m = digest.PERIOD_SCHED.get(period, (now.hour, now.minute))
    return {"id": period, "label": f"科技 / AI {digest.PERIOD_NAMES.get(period, period)}",
            "time": f"{h:02d}:{m:02d}", "enabled": True, "day_types": ["all"]}


def _planned(period_or_schedule, now: datetime) -> datetime:
    row = period_or_schedule if isinstance(period_or_schedule, dict) else _schedule_for(period_or_schedule, now)
    hour, minute = (int(part) for part in str(row["time"]).split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _at_time(now: datetime, value: str) -> datetime:
    hour, minute = (int(part) for part in value.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _daily_cutoff(now: datetime, cfg: dict) -> datetime:
    field = "workday_cutoff" if cal.is_workday(now) else "restday_cutoff"
    fallback = "22:00" if field == "workday_cutoff" else "20:00"
    return _at_time(now, str(cfg.get(field) or fallback))


def _issue_window(row: dict, rows: list[dict], now: datetime,
                  cfg: dict) -> tuple[datetime, datetime]:
    """Return one same-day half-open [start, cutoff) window."""
    planned = _planned(row, now)
    candidates = [_daily_cutoff(now, cfg)]
    explicit = row.get("cutoff")
    if isinstance(explicit, str) and explicit:
        candidates.append(_at_time(now, explicit))
    candidates.extend(_planned(other, now) for other in rows
                      if _planned(other, now) > planned)
    if cfg.get("allow_missed_catchup", True) is False:
        candidates.append(planned + timedelta(minutes=int(cfg.get("retry_window_minutes") or 30)))
    return planned, min(candidates)


def effective_windows(now_dt: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    """Side-effect-free schedule preview used by the local settings page."""
    now = now_dt or cal.bj_now()
    active = cfg or _config()
    rows = _schedules(now, active)
    result = []
    for row in rows:
        start, cutoff = _issue_window(row, rows, now, active)
        result.append({
            "id": row.get("id"), "label": row.get("label"),
            "start": start.strftime("%H:%M"), "cutoff": cutoff.strftime("%H:%M"),
            "day_type": "workday" if cal.is_workday(now) else "restday",
            "catchup": bool(active.get("allow_missed_catchup", True)),
        })
    return result


def _key(day: str, schedule_id: str) -> str:
    return f"{day}|{schedule_id}"


def _adopt_matching_legacy_ledger(state: dict, day: str, row: dict,
                                  now: datetime) -> None:
    """Map a renamed same-time issue without minting a fresh paid-call budget."""
    new_id = str(row.get("id") or "")
    new_key = _key(day, new_id)
    if any(new_key in (state.get(name) or {})
           for name in ("done", "attempts", "model_calls", "issue_configs")):
        return
    new_time = str(row.get("time") or "")
    prefix = day + "|"
    candidates = set()
    for name in ("done", "attempts", "model_calls", "issue_configs"):
        candidates.update(key for key in (state.get(name) or {}) if key.startswith(prefix))
    matched = None
    for old_key in sorted(candidates):
        old_id = old_key.split("|", 1)[1]
        old_cfg = (state.get("issue_configs") or {}).get(old_key)
        old_time = None
        if isinstance(old_cfg, dict):
            for old_row in old_cfg.get("schedules") or []:
                if isinstance(old_row, dict) and str(old_row.get("id") or "") == old_id:
                    old_time = str(old_row.get("time") or "")
                    break
        legacy_rest = (not cal.is_workday(now) and new_id == "restday-daily"
                       and old_id == "morning")
        if old_time == new_time or legacy_rest:
            matched = old_key
            break
    if not matched:
        return
    for name in ("done", "attempts", "model_calls", "issue_configs"):
        source = (state.get(name) or {}).get(matched)
        if source is not None:
            state.setdefault(name, {})[new_key] = copy.deepcopy(source)
    state.setdefault("aliases", {})[new_key] = matched
    _log(state, int(time.time()), f"adopted legacy issue ledger {matched}", new_id)


def _apply_runtime_config(cfg: dict) -> None:
    from .news_model_limits import validate_generation_limits
    model = str(cfg.get("model") or "deepseek-v4-flash")
    input_limit = cfg.get("max_input_tokens", 4000)
    output_limit = cfg.get("max_output_tokens", 500)
    problems = validate_generation_limits(model, input_limit, output_limit)
    if problems:
        raise ValueError("invalid news generation limits: " + "; ".join(problems))
    brief.MODEL_NAME = model
    brief.MAX_INPUT_TOKEN = int(input_limit)
    brief.MAX_OUTPUT_TOKEN = int(output_limit)
    budget.configure_limit(cfg.get("monthly_budget_cny"))


def _generate(cands: list[dict], period: str, hint: str | None,
              cfg: dict | None = None) -> tuple[dict | None, str | None]:
    _apply_runtime_config(cfg or _config())
    estimate = brief.estimate_max_cost_cny()
    budget.reserve(estimate)  # telemetry only; never authorizes or blocks
    try:
        result = digest._call_digest(cands, period, hint=hint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NEWS] digest call exception: %s", exc)
        budget.settle(estimate, release=estimate)
        return None, "skip:gen"
    if not result.get("ok") and not result.get("usage"):
        budget.settle(estimate, release=estimate)
    return result, None


def _real_planned_epoch(planned: datetime, now: datetime) -> int:
    return int(time.time()) - int(max(0.0, (now - planned).total_seconds()))


def _publish(state: dict, result: dict, period: str, planned: datetime,
             now: datetime, issue_title: str | None = None, *,
             completion_now: datetime | None = None,
             cutoff: datetime | None = None) -> str:
    finished = completion_now or now
    if cutoff is not None and (finished.date() != planned.date() or finished >= cutoff):
        _log(state, int(time.time()),
             f"in-flight result expired before publish cutoff={cutoff.strftime('%H:%M')}", period)
        return "skip:expired-inflight"
    text = "\n".join((result.get("lines") or [])[:digest.MAX_LINES])
    final_check = digest.validate_digest_text(text)
    if not final_check.get("ok"):
        reason = str(final_check.get("reason") or "unknown")
        _log(state, int(time.time()), f"final publication rejected: {reason}", period)
        return _retain_previous_valid(state, period, planned, now, reason)
    text = "\n".join(final_check.get("lines") or [])
    events = [{"id": event.get("id"), "category": str(event.get("category") or ""),
               "source": event.get("source"), "url": event.get("url"),
               "title": event.get("title"), "event_type": event.get("event_type"),
               "published_at": event.get("published_at"), "trusted_at": event.get("trusted_at"),
               "event_at": event.get("event_at"), "update_of": event.get("update_of"),
               "time_trust": event.get("time_trust"), "fingerprint": event.get("fingerprint"),
               "evidence": event.get("evidence")}
              for event in (result.get("events") or [])[:4] if event.get("id")]
    version = digest_publish_version(period, text)
    brief_state = brief._load_state()
    new_current = {
        "mode": "digest", "period": period, "schedule_id": period,
        "issue_title": issue_title or _schedule_for(period, now).get("label"),
        "date": planned.strftime("%Y-%m-%d"),
        "planned_at": _real_planned_epoch(planned, now),
        "generated_at": int(time.time()), "text": text, "events": events,
        "reasons": result.get("reasons") or [], "exception": result.get("exception") or "",
        "freshness": "fresh", "note": result.get("publish_note"),
        "origin": result.get("origin") or "deepseek", "version": version,
    }
    brief_state["current"] = new_current
    brief_state["last_valid_digest"] = copy.deepcopy(new_current)
    from . import news_semantics as semantics
    issued_at = brief_state["current"]["generated_at"]
    prior = semantics.prune_history(brief_state.get("published_events") or [], now=issued_at)
    brief_state["published_events"] = semantics.prune_history(
        semantics.history_rows(events, issued_at=issued_at) + prior, now=issued_at)
    brief._save_state(brief_state)
    _log(state, int(time.time()),
         f"published digest id={period} origin={brief_state['current']['origin']} text_len={len(text)}",
         period)
    return "published"


def _valid_digest_current(value: object) -> bool:
    return (isinstance(value, dict) and value.get("mode") == "digest"
            and bool(value.get("text"))
            and digest.validate_digest_text(str(value.get("text") or "")).get("ok") is True)


def _retain_previous_valid(state: dict, period: str, planned: datetime,
                           now: datetime, reason: str) -> str:
    """Never replace a good issue with invalid text or relabel old copy as new."""
    brief_state = brief._load_state()
    current = brief_state.get("current")
    previous = current if _valid_digest_current(current) else brief_state.get("last_valid_digest")
    note = f"本期简报未通过发布校验（{reason}），未更新正文"
    if _valid_digest_current(previous):
        kept = copy.deepcopy(previous)
        kept["freshness"] = "stale"
        kept["note"] = note
        brief_state["current"] = kept
        brief_state["last_valid_digest"] = copy.deepcopy(previous)
        brief._save_state(brief_state)
        return "skip:keep-old:final-validation"
    status_text = "本期简报未通过发布校验，暂时没有可显示的新内容。"
    checked = digest.validate_digest_text(status_text)
    brief_state["current"] = {
        "mode": "status", "period": period, "schedule_id": period,
        "issue_title": "资讯状态", "date": planned.strftime("%Y-%m-%d"),
        "planned_at": _real_planned_epoch(planned, now),
        "generated_at": int(time.time()),
        "text": "\n".join(checked.get("lines") or [status_text]), "events": [],
        "freshness": "error", "note": note, "origin": "local-status",
        "version": digest_publish_version(period, status_text),
    }
    brief._save_state(brief_state)
    return "published:status:validation"


def _publish_daily_message(state: dict, period: str, planned: datetime,
                           now: datetime, issue_title: str | None = None, *,
                           note: str = "未配置新闻生成 API，显示本地每日寄语") -> str:
    from . import daily_message
    brief_state = brief._load_state()
    current = brief_state.get("current")
    if (isinstance(current, dict) and current.get("mode") == "daily_message"
            and current.get("date") == planned.strftime("%Y-%m-%d")
            and current.get("schedule_id") == period and current.get("text")):
        return "published:daily-message"
    recent = list(brief_state.get("daily_message_history") or [])
    selected = daily_message.choose(planned.strftime("%Y-%m-%d"), period, recent)
    text = "\n".join(selected["lines"])
    brief_state["current"] = {
        "mode": "daily_message", "period": period, "schedule_id": period,
        "issue_title": "每日寄语", "date": planned.strftime("%Y-%m-%d"),
        "planned_at": _real_planned_epoch(planned, now),
        "generated_at": int(time.time()), "text": text, "events": [],
        "freshness": "fresh", "note": note,
        "origin": "local-daily-message", "message_id": selected["id"],
        "version": daily_message.version(planned.strftime("%Y-%m-%d"), period, selected["id"]),
    }
    brief_state["daily_message_history"] = (recent + [selected["id"]])[-8:]
    brief._save_state(brief_state)
    _log(state, int(time.time()), f"published daily-message id={selected['id']}", period)
    return "published:daily-message"


def _mark_generation_unavailable(state: dict, period: str, credential: dict) -> str:
    brief_state = brief._load_state()
    current = brief_state.get("current")
    status = credential["state"]
    note_map = {
        "disabled": "新闻生成已停用，保留上一期内容",
        "pending_validation": "新闻 API 等待验证，保留上一期内容",
        "auth_failure": "新闻 API 授权失败，保留上一期内容",
        "temporary_failure": "新闻 API 暂时不可用，保留上一期内容",
    }
    note = note_map.get(status, "新闻生成暂不可用")
    if isinstance(current, dict) and current.get("mode") == "digest" and current.get("text"):
        current["freshness"] = "stale"
        current["note"] = note
        brief._save_state(brief_state)
        _log(state, int(time.time()), note, period)
        return f"skip:keep-old:{status}"
    text_map = {
        "disabled": "新闻生成功能当前已停用。请在配置中启用并完成密钥验证。",
        "pending_validation": "新闻 API 已填写，正在等待验证。验证完成后将从下一期恢复。",
        "auth_failure": "新闻 API 授权失败，暂时没有可显示的新简报。请检查密钥状态。",
        "temporary_failure": "新闻服务暂时不可用，暂时没有可显示的新简报。稍后会按规则重试。",
    }
    checked = digest.validate_digest_text(text_map.get(status, note))
    brief_state["current"] = {
        "mode": "status", "period": period, "schedule_id": period,
        "issue_title": "资讯状态", "generated_at": int(time.time()),
        "text": "\n".join(checked.get("lines") or [note]), "events": [],
        "freshness": "error" if status == "auth_failure" else "stale",
        "note": note, "origin": "local-status",
        "version": digest_publish_version(period, note),
    }
    brief._save_state(brief_state)
    _log(state, int(time.time()), note, period)
    return f"published:status:{status}"


def _max_model_calls(cfg: dict | None) -> int:
    value = (cfg or {}).get("max_calls_per_issue", 5)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 5
    return value


def _take_model_call(state: dict, issue_key: str, period: str, limit: int) -> bool:
    used = int((state.get("model_calls") or {}).get(issue_key, 0))
    if used >= limit:
        return False
    state.setdefault("model_calls", {})[issue_key] = used + 1
    _log(state, int(time.time()),
         f"model-call {used + 1}/{limit} reserved before network send", period)
    _save(state)  # crash/network uncertainty must still consume the attempt
    return True


def digest_publish_version(period: str, text: str) -> str:
    return hashlib.md5((period + "|" + text).encode("utf-8")).hexdigest()[:16]


def _topic_order(candidates: list[dict], preferences: list[str]) -> list[dict]:
    words = [word.lower() for word in preferences if word.strip()]
    if not words:
        return candidates
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (-sum(word in (str(pair[1].get("title") or "") + " " +
                                                   str(pair[1].get("summary") or "")).lower()
                                             for word in words), pair[0]))
    return [row for _, row in indexed]


def _collect_if_due(state: dict, cfg: dict) -> None:
    source_ids = enabled_source_ids(cfg)
    if not source_ids:
        _log(state, int(time.time()), "remote sources disabled; collection skipped")
        return
    if time.time() - float(state.get("last_collect") or 0) < COLLECT_MIN_GAP_S:
        return
    try:
        brief.collect_all(source_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NEWS] collect failed: %s", exc)
    state["last_collect"] = int(time.time())


def _run_one(state: dict, cands: list[dict] | None, period: str, planned: datetime,
             now: datetime, schedule: dict | None = None, cfg: dict | None = None,
             issue_key: str | None = None, cutoff: datetime | None = None) -> str:
    cfg = cfg or _config()
    schedule = schedule or _schedule_for(period, now)
    source_ids = enabled_source_ids(cfg)
    if not source_ids:
        return _publish_daily_message(
            state, period, planned, now, schedule.get("label"),
            note="未启用远程新闻源，显示本地每日寄语")
    credential = credential_state()
    if credential["state"] == "missing":
        return _publish_daily_message(state, period, planned, now, schedule.get("label"))
    if credential["state"] != "active":
        return _mark_generation_unavailable(state, period, credential)
    _collect_if_due(state, cfg)
    if cands is None:
        candidates = brief.build_candidates(source_ids=source_ids)
    else:
        candidates = brief.filter_candidates(cands, source_ids)
    candidates = _topic_order(candidates, list(cfg.get("topic_preferences") or []))
    if not candidates:
        _log(state, int(time.time()), "no candidates", period)
        return "skip:no-candidates"
    _apply_runtime_config(cfg)
    started = time.monotonic()

    def publish(value: dict) -> str:
        completed = now + timedelta(seconds=max(0.0, time.monotonic() - started))
        return _publish(state, value, period, planned, now, schedule.get("label"),
                        completion_now=completed, cutoff=cutoff)

    call_key = issue_key or _key(planned.strftime("%Y-%m-%d"), period)
    call_limit = _max_model_calls(cfg)
    if not _take_model_call(state, call_key, period, call_limit):
        return "skip:max-model-calls"
    result, why = _generate(candidates, period, None, cfg)
    if result is None:
        _log(state, int(time.time()), f"generation unavailable: {why or 'unknown'}", period)
        return str(why)
    while not result.get("ok"):
        if result.get("body"):
            reason = result.get("note") or "validate"
            _log(state, int(time.time()), f"model result rejected: {reason}", period)
            if not _take_model_call(state, call_key, period, call_limit):
                fallback = digest.build_local_fallback(candidates)
                if fallback is not None:
                    _log(state, int(time.time()),
                         f"generation attempts exhausted: {reason}; local fallback", period)
                    return publish(fallback)
                _log(state, int(time.time()),
                     f"generation attempts exhausted: {reason}; previous issue retained", period)
                return "skip:keep-old"
            revised, why2 = _generate(candidates, period, reason, cfg)
            if revised is not None and revised.get("ok"):
                return publish(revised)
            if revised is None:
                _log(state, int(time.time()),
                     f"revision unavailable: {why2 or 'unknown'}", period)
                return str(why2 or "skip:gen")
            result = revised
            continue
        if result.get("failure_kind") in {"auth_failure", "temporary_failure"}:
            return _mark_generation_unavailable(state, period, {
                "state": result["failure_kind"], "reason": result.get("note")})
        return "skip:gen"
    return publish(result)


def digest_freshness(cur: dict | None, now_dt: datetime | None = None) -> str:
    if not cur or cur.get("mode") not in {"digest", "daily_message", "status"}:
        return "fresh"
    current = str(cur.get("freshness") or "fresh")
    if current == "error":
        return "error"
    now = now_dt or cal.bj_now()
    cfg = _config()
    rows = _schedules(now, cfg)
    due = [(row, _planned(row, now)) for row in rows if _planned(row, now) <= now]
    if not due:
        return current
    row, planned = due[-1]
    _, cutoff = _issue_window(row, rows, now, cfg)
    if now < cutoff:
        return current
    done = _load().get("done") or {}
    return "fresh" if done.get(_key(now.strftime("%Y-%m-%d"), row["id"])) == "published" else "stale"


def _terminal_outcome(outcome: str) -> bool:
    return (outcome in {"published", "published:daily-message", "skip:max-model-calls",
                        "skip:keep-old", "skip:keep-old:final-validation",
                        "skip:expired-inflight"})


def _tick_unlocked(now_dt: datetime | None = None) -> dict:
    now = now_dt or cal.bj_now()
    cfg = _config()
    state = _load()
    rows = _schedules(now, cfg)
    day = now.strftime("%Y-%m-%d")
    workday = cal.is_workday(now)
    state_error = state.pop("_state_error", None)
    if state_error:
        due_rows = [row for row in rows if _planned(row, now) <= now]
        if due_rows:
            row = due_rows[-1]
            state["done"][_key(day, row["id"])] = "skip:state-unknown"
        _log(state, int(time.time()), f"schedule state {state_error}; due slots sealed")
        _save(state)
        return {"tick_at": int(time.time()), "workday": workday,
                "degraded_calendar": False, "done_slots": len(state["done"]),
                "latest": {"done": "skip:state-unknown", "reason": state_error}}

    # A missing API never justifies a paid call.  Keep the established stable
    # date+issue local message available before the first issue time; the same
    # identity is reused at 08:55 and cannot duplicate content or budget.
    credential = credential_state()
    no_remote_sources = not enabled_source_ids(cfg)
    if (rows and (credential["state"] == "missing" or no_remote_sources)
            and not brief._load_state().get("current")):
        first = rows[0]
        _publish_daily_message(state, str(first["id"]), _planned(first, now), now,
                               str(first.get("label") or ""),
                               note=("未启用远程新闻源，显示本地每日寄语"
                                     if no_remote_sources else
                                     "未配置新闻生成 API，显示本地每日寄语"))

    # Pre-collect once in the configured lead window; this is free RSS only.
    lead = int(cfg.get("precollect_minutes") or 0)
    upcoming = [(_planned(row, now), row) for row in rows
                if now <= _planned(row, now) <= now + timedelta(minutes=lead)]
    if upcoming and credential["state"] == "active" and not no_remote_sources:
        _collect_if_due(state, cfg)

    # Never start a paid issue before its configured wall-clock time. The old
    # +2 minute tolerance caused an 08:55 issue to consume its budget at 08:53.
    due = [(row, _planned(row, now)) for row in rows if _planned(row, now) <= now]
    summary = None
    if due:
        attempt_outcome = None
        for old_row, _ in due[:-1]:
            old_key = _key(day, old_row["id"])
            if not state["done"].get(old_key):
                state["done"][old_key] = "skip:superseded"
        row, planned = due[-1]
        period = str(row["id"])
        key = _key(day, period)
        _adopt_matching_legacy_ledger(state, day, row, now)
        issue_cfg = state.setdefault("issue_configs", {}).get(key)
        window_cfg = issue_cfg if isinstance(issue_cfg, dict) else cfg
        _, cutoff = _issue_window(row, rows, now, window_cfg)
        # Old versions sealed a missed issue after 30 minutes or at night.  A
        # same-day issue now inside the effective window reuses its exact ledger
        # and call count instead of receiving a fresh budget.
        if (cfg.get("allow_missed_catchup", True)
                and state["done"].get(key) in {"skip:window", "skip:night", "skip:budget"}
                and planned <= now < cutoff):
            old = state["done"].pop(key)
            _log(state, int(time.time()), f"migrated legacy {old} into current window", period)
        attempts = int(state["attempts"].get(key, 0))
        if now >= cutoff:
            state["done"][key] = "skip:window"
            _log(state, int(time.time()),
                 f"automatic window expired at {cutoff.strftime('%H:%M')}", period)
        elif not state["done"].get(key):
            effective_limit = _max_model_calls(issue_cfg if isinstance(issue_cfg, dict) else cfg)
            if int(state.get("model_calls", {}).get(key, 0)) >= effective_limit:
                state["done"][key] = "skip:max-model-calls"
            else:
                if not isinstance(issue_cfg, dict):
                    issue_cfg = copy.deepcopy(cfg)
                    state["issue_configs"][key] = issue_cfg
                state.setdefault("inflight", {})[key] = {
                    "started_at": int(time.time()), "period": period,
                    "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save(state)
                try:
                    outcome = _run_one(state, None, period, planned, now, row,
                                       issue_cfg, key, cutoff)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[NEWS] issue run failed: %s", type(exc).__name__)
                    _log(state, int(time.time()),
                         f"issue run exception: {type(exc).__name__}", period)
                    outcome = "skip:gen"
                finally:
                    state.setdefault("inflight", {}).pop(key, None)
                state["attempts"][key] = attempts + 1
                attempt_outcome = outcome
                issue_limit = _max_model_calls(issue_cfg)
                if int(state.get("model_calls", {}).get(key, 0)) >= issue_limit:
                    if not _terminal_outcome(outcome):
                        outcome = "skip:max-model-calls"
                    state["done"][key] = outcome
                elif _terminal_outcome(outcome):
                    state["done"][key] = outcome
                else:
                    _log(state, int(time.time()),
                         "issue remains due within current automatic window", period)
        used_cfg = (state.get("issue_configs") or {}).get(key) or cfg
        summary = {"period": period, "title": row.get("label"),
                   "done": state["done"].get(key) or attempt_outcome,
                   "window": {"start": planned.strftime("%H:%M"),
                              "cutoff": cutoff.strftime("%H:%M")},
                   "generation_limits": {
                       "model": used_cfg.get("model"),
                       "max_input_tokens": used_cfg.get("max_input_tokens"),
                       "max_output_tokens": used_cfg.get("max_output_tokens"),
                       "max_calls_per_issue": _max_model_calls(used_cfg),
                       "count_method": "conservative_utf8_byte_upper_bound"}}
    _save(state)
    calendar = cal.calendar_status()
    degraded = bool(calendar.get("degraded")) or str(now.year) not in (calendar.get("years") or [])
    return {"tick_at": int(time.time()), "workday": workday,
            "degraded_calendar": degraded, "done_slots": len(state.get("done") or {}),
            "latest": summary}


def tick(now_dt: datetime | None = None) -> dict:
    with state_store.file_lock(_STATE, operation="schedule"):
        return _tick_unlocked(now_dt)


def current_due_status(now_dt: datetime | None = None) -> dict:
    """Describe the latest issue that should exist now without running it.

    Selection uses only the publisher's trusted calendar and active operator
    configuration. Device-supplied dates or schedule ids never choose an issue.
    """
    now = now_dt or cal.bj_now()
    cfg = _config()
    rows = _schedules(now, cfg)
    due = [(row, _planned(row, now)) for row in rows if _planned(row, now) <= now]
    if not due:
        return {"state": "not-due", "due": False, "current": True,
                "issue_key": None, "schedule_id": None}
    row, planned = due[-1]
    schedule_id = str(row.get("id") or "")
    issue_key = _key(now.strftime("%Y-%m-%d"), schedule_id)
    schedule_state = _load()
    done = (schedule_state.get("done") or {}).get(issue_key)
    current = brief._load_state().get("current") or {}
    current_matches = bool(
        isinstance(current, dict)
        and current.get("date") == now.strftime("%Y-%m-%d")
        and str(current.get("schedule_id") or current.get("period") or "") == schedule_id
        and current.get("mode") in {"digest", "daily_message", "status"}
        and current.get("text")
    )
    issue_cfg = (schedule_state.get("issue_configs") or {}).get(issue_key)
    _, cutoff = _issue_window(row, rows, now,
                              issue_cfg if isinstance(issue_cfg, dict) else cfg)
    inside_window = planned <= now < cutoff
    inflight = (schedule_state.get("inflight") or {}).get(issue_key)
    running = (isinstance(inflight, dict)
               and time.time() - float(inflight.get("started_at") or 0) < 600)
    if current_matches and str(done or "").startswith("published"):
        state_name = "current"
    elif running:
        state_name = "generating"
    elif not inside_window:
        state_name = "expired"
    elif done:
        state_name = "failed"
    elif credential_state()["state"] not in {"active", "missing"}:
        state_name = "waiting"
    else:
        state_name = "due"
    return {
        "state": state_name,
        "due": True,
        "current": current_matches,
        "inside_window": inside_window,
        "window_start": planned.strftime("%H:%M"),
        "window_cutoff": cutoff.strftime("%H:%M"),
        "issue_key": issue_key,
        "schedule_id": schedule_id,
        "label": row.get("label"),
        "planned_at": int(planned.timestamp()),
        "done": done,
        "model_calls": int((schedule_state.get("model_calls") or {}).get(issue_key, 0)),
        "max_model_calls": _max_model_calls(
            (schedule_state.get("issue_configs") or {}).get(issue_key) or cfg),
    }


def run_manual_issue(issue_key: str, issue_config: dict, *,
                     now_dt: datetime | None = None,
                     issue_title: str = "手动生成") -> str:
    """Run one manual issue without changing scheduled-slot completion state.

    The issue snapshot and every model-call reservation share the scheduler's
    durable state and lock.  A crash after reservation therefore consumes the
    uncertain call, while the normal schedule's ``done`` and ``attempts`` maps
    are left untouched.
    """
    now = now_dt or cal.bj_now()
    with state_store.file_lock(_STATE, operation="schedule"):
        state = _load()
        snapshot = copy.deepcopy(issue_config)
        state.setdefault("issue_configs", {})[issue_key] = snapshot
        _save(state)
        schedule = {
            "id": "manual",
            "label": issue_title,
            "time": now.strftime("%H:%M"),
            "enabled": True,
            "day_types": ["all"],
        }
        outcome = _run_one(
            state,
            None,
            "manual",
            now.replace(second=0, microsecond=0),
            now,
            schedule,
            snapshot,
            issue_key,
        )
        _save(state)
        return outcome
