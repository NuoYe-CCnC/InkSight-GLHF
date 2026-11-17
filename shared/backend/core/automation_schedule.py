"""Beijing schedules. Display precision and inferred scheduling anchors are separate."""
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
import re

BJ = ZoneInfo('Asia/Shanghai')
HOUR = 3600

def daily_slot(now):
    local = datetime.fromtimestamp(now, BJ)
    point = local.replace(hour=6, minute=0, second=0, microsecond=0)
    if local < point:
        point -= timedelta(days=1)
    return point.strftime('%Y-%m-%d@06:00'), point.timestamp()

def next_daily(now):
    _, at = daily_slot(now)
    return at + 86400

def announcement_due(state, now, manual=False):
    slot, at = daily_slot(now)
    blocked = max(state.get('next_allowed_at', 0), state.get('inflight_until', 0))
    covered = slot in (state.get('slot_success') or {}) or state.get('daily_success_slot', '') >= slot
    due = manual or not covered
    return dict(due=bool(due and now >= blocked and not state.get('auth_blocked')),
                slots=[slot] if not covered else [], slot=slot, slot_at=at,
                interval_seconds=None, coalesce=False,
                next_allowed_at=blocked, next_query_at=None if state.get('auth_blocked') else max(at if due else next_daily(now), blocked),
                reason='auth_required' if state.get('auth_blocked') else 'backoff' if now < blocked else 'daily_06' if due else 'not_due')

def retry_wait(failures):
    return [900, 1800, 3600][min(max(failures - 1, 0), 2)] if failures <= 3 else 10800

def date_parts(raw):
    match=re.fullmatch(r'(?:(\d{4})年\s*)?(\d{1,2})月\s*(\d{1,2})日',raw)
    if match:
        year,month,day=match.groups()
    else:
        names=['January','February','March','April','May','June','July','August','September','October','November','December']
        match=re.fullmatch(r'('+'|'.join(names)+r')\s+(\d{1,2})(?:,?\s+(\d{4}))?',raw,re.I)
        if not match:return None
        month=str(next(i+1 for i,n in enumerate(names) if n.lower()==match[1].lower()));day=match[2];year=match[3]
    try:
        date(int(year or 2000),int(month),int(day))
    except ValueError:return None
    return (year,month,day)

def calendar_anchor(record, observed_at, previous=None):
    """No inferred year goes into the official/displayed date. Never roll an old value."""
    raw = record.get('display_date', '')
    parts = date_parts(raw)
    if not parts:
        return None
    year, month, day = parts
    observed = datetime.fromtimestamp(observed_at, BJ).date()
    if previous and previous.get('display_date') == raw and previous.get('schedule_anchor'):
        return previous['schedule_anchor']
    try:
        if year:
            value = date(int(year), int(month), int(day))
            basis = 'explicit_source_year'
        else:
            value = date(observed.year, int(month), int(day))
            basis = 'observation_calendar_year_not_official_precision'
            delta = (value - observed).days
            if delta > 330 and observed.month == 1 and int(month) == 12:
                value = date(observed.year - 1, int(month), int(day))
                basis = 'recent_prior_december_boundary_not_official_year'
            elif delta < -300 and previous and previous.get('display_date') != raw:
                candidate = date(observed.year + 1, int(month), int(day))
                if not 0 <= (candidate - observed).days <= 62:
                    return None
                value = candidate
                basis = 'changed_source_date_near_year_boundary_not_official_year'
            elif delta < -62 or delta > 62:
                return None
    except ValueError:
        return None
    return {'date': value.isoformat(), 'basis': basis, 'official_year': bool(year)}

def apple_due(state, now, enabled=True, manual=False):
    if not enabled:
        return dict(due=False, reason='disabled', next_at=None)
    if state.get('job', {}).get('status') in {'queued', 'running'}:
        return dict(due=False, reason='in_progress', next_at=state['job'].get('lease_until'))
    block = max(state.get('next_allowed_at', 0), 0 if manual else state.get('skip_until', 0))
    if state.get('blocked'):
        return dict(due=False, reason=state['blocked'], next_at=None)
    if manual:
        return dict(due=now >= block, reason='manual' if now >= block else 'backoff', next_at=max(now, block))
    record = state.get('record') or {}
    if not record:
        at = max(block, state.get('last_attempt_at', 0) + 6 * HOUR if state.get('last_attempt_at') else 0)
        return dict(due=now >= at, reason='initialization', next_at=at)
    if state.get('retry_pending'):
        if state.get('stale_since') and now>=state['stale_since']+72*HOUR:
            block=max(block,state.get('last_attempt_at',now)+7*86400)
        return dict(due=now >= block, reason='failed_read_retry', next_at=block)
    if record.get('displayed_state') == 'expired':
        return dict(due=False, reason='explicitly_expired', next_at=None)
    anchor = record.get('schedule_anchor')
    if not anchor:
        return dict(due=False, reason='date_needs_confirmation', next_at=None)
    point = date.fromisoformat(anchor['date'])
    next_day = datetime.combine(point + timedelta(days=1), time(9), BJ).timestamp()
    checked = state.get('checked_nodes') or {}
    cycle = point.isoformat()
    if record.get('action_word') in {'到期', 'Expires'} or record.get('displayed_state') == 'canceled':
        if checked.get(cycle + ':after'):
            return dict(due=False, reason='valid_until_checked', next_at=None)
        at = max(next_day, block)
        return dict(due=now >= at, reason='valid_until_next_day', next_at=at)
    stale = state.get('stale_since')
    if stale:
        dense_end = stale + 72 * HOUR
        last = max(state.get('last_attempt_at', 0), state.get('last_success_at', 0), stale)
        at = max(block, last + (6 * HOUR if now < dense_end else 7 * 86400))
        return dict(due=now >= at, reason='old_date_6h' if now < dense_end else 'old_date_weekly_pending', next_at=at)
    if now >= next_day:
        if checked.get(cycle + ':after'):
            return dict(due=False, reason='next_day_already_checked', next_at=None)
        return dict(due=now >= block, reason='next_day', next_at=max(next_day, block))
    if datetime.fromtimestamp(now,BJ).date()==point:
        return dict(due=False,reason='renewal_day_no_extra_read',next_at=max(next_day,block))
    before = datetime.combine(point - timedelta(days=3), time(9), BJ).timestamp()
    if not checked.get(cycle + ':before'):
        at = max(before, block)
        return dict(due=now >= at, reason='three_days_before', next_at=at)
    return dict(due=False, reason='waiting_next_day', next_at=max(next_day, block))

def accept_apple(state, record, now):
    old = state.get('record') or {}
    same = old.get('display_date') == record.get('display_date')
    parts=date_parts(record.get('display_date',''))
    record = dict(record, precision='year_month_day' if parts and parts[0] else 'month_day')
    record['schedule_anchor'] = calendar_anchor(record, now, old)
    state.update(record=record, last_success_at=now, blocked=None, retry_pending=False, next_allowed_at=0)
    anchor = record['schedule_anchor']
    if not same:
        state.pop('stale_since', None)
    if anchor:
        point = date.fromisoformat(anchor['date'])
        local = datetime.fromtimestamp(now, BJ)
        checked = state.setdefault('checked_nodes', {})
        if local >= datetime.combine(point - timedelta(days=3), time(9), BJ):
            checked[point.isoformat() + ':before'] = now
        if local >= datetime.combine(point + timedelta(days=1), time(9), BJ):
            checked[point.isoformat() + ':after'] = now
            if record.get('action_word') in {'续期', '续订', 'Renews'} and record.get('displayed_state') not in {'expired', 'canceled'}:
                state.setdefault('stale_since', now)
    if record.get('action_word') in {'到期', 'Expires'} or record.get('displayed_state') in {'expired', 'canceled'}:
        state.pop('stale_since', None)
    state['checked_nodes'] = dict(list(state.get('checked_nodes', {}).items())[-20:])
