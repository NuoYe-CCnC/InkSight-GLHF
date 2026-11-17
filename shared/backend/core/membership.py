"""Typed subscription projection; source precision is independent of scheduling."""
import copy
import re
import time
from datetime import date

from . import automation_schedule as schedule, state_store


def effective_member(manual, *, enabled=None, state=None, now=None):
    result = copy.deepcopy(manual or {})
    if enabled is None:
        from .operator_config import load_effective
        enabled = load_effective().config.get('apple_subscription', {}).get('enabled', False)
    if not enabled:
        return result
    if state is None:
        from .apple_subscription import _load
        try:
            state = _load()
        except state_store.StateStoreError:
            state = {'last_result': 'failed', 'blocked': 'state_unavailable'}
    now = time.time() if now is None else now
    record = state.get('record') or {}
    parts = schedule.date_parts(record.get('display_date', ''))
    trusted = record.get('source') == 'Apple 官方订阅页' and bool(parts)
    action = record.get('action_word') if trusted else None
    expired = trusted and record.get('displayed_state') == 'expired'
    dtype = ('valid_until' if expired or action in {'到期', 'Expires', 'Expired'} else
             'renewal' if action in {'续期', '续订', 'Renews'} else 'unknown')
    canonical = None
    precision = 'unknown'
    if trusted:
        year, month, day = parts
        canonical = (f'{int(year):04d}-' if year else '') + f'{int(month):02d}-{int(day):02d}'
        precision = 'date' if year else 'month_day'
    read_status = ('blocked' if state.get('blocked') else
                   'failed' if state.get('last_result') == 'failed' else
                   'success' if trusted else 'pending')
    # Stale schedule state does not change the source's renewal/expiry semantics.
    anchor = record.get('schedule_anchor') or {}
    anchor_old = bool(anchor.get('date') and anchor['date'] < schedule.datetime.fromtimestamp(now, schedule.BJ).date().isoformat())
    uncertain = bool(not trusted or dtype == 'unknown' or state.get('stale_since') or
                     state.get('retry_pending') or state.get('blocked') or read_status == 'failed' or
                     (anchor_old and not expired))
    info = dict(schema=1, source='apple_subscription', date_type=dtype,
                state='expired' if expired else 'renewing' if dtype == 'renewal' else
                'valid_until' if dtype == 'valid_until' else 'unknown',
                date=canonical, precision=precision,
                raw_date_text=record.get('display_date') if trusted else None,
                source_wording=record.get('state_wording') if trusted else None,
                last_success_at=state.get('last_success_at'), read_status=read_status,
                needs_confirmation=uncertain)
    result['subscription'] = info
    # Old devices must never interpret a renewal/month-day value as full expiry.
    result['valid_until_date'] = canonical if dtype == 'valid_until' and precision == 'date' else None
    result['renewal_status'] = info['state']
    result['precision'] = precision
    return result


def _plan(ai):
    raw = str(ai.get('plan') or '会员')
    if raw == '会员':
        return raw
    if not re.fullmatch(r'[A-Za-z0-9 _/.-]+', raw):
        return '会员'
    return raw if len(raw) <= 12 else raw[:12] + '...'


def _typed(ai):
    value = ai.get('membership')
    return value if isinstance(value, dict) else None


def valid_date(info):
    raw = info.get('date')
    precision = info.get('precision')
    if not isinstance(raw, str):
        return None
    pattern = r'\d{4}-\d{2}-\d{2}' if precision == 'date' else r'\d{2}-\d{2}' if precision == 'month_day' else r'(?!)'
    if not re.fullmatch(pattern, raw):
        return None
    try:
        date.fromisoformat(raw if precision == 'date' else '2000-' + raw)
    except ValueError:
        return None
    return raw[-5:]


def line(ai):
    plan = _plan(ai)
    info = _typed(ai)
    if info is None:
        valid = str(ai.get('valid_until_date') or '')
        return f'{plan} · 有效至 {valid[5:10] if len(valid) >= 10 else valid[:5]}'
    mmdd = valid_date(info) if info.get('schema') == 1 else None
    if info.get('schema') == 1 and info.get('state') == 'expired':
        return f'{plan} · 已到期' + (f' {mmdd}' if mmdd else '')
    label = {'renewal': '续期', 'valid_until': '有效至'}.get(info.get('date_type'))
    if mmdd and label and info.get('state') in {'renewing', 'valid_until'}:
        return f'{plan} · {label} {mmdd}'
    return f'{plan} · 待确认'


def problem(ai):
    info = _typed(ai)
    return (bool(info.get('needs_confirmation') or not valid_date(info) or info.get('schema') != 1)
            if info is not None else not bool(ai.get('plan') and ai.get('valid_until_date')))


def visual_member(member):
    """Visible semantics only: rereading unchanged values never bumps a version."""
    ai = {'plan': member.get('plan'), 'valid_until_date': member.get('valid_until_date'),
          'membership': member.get('subscription')}
    return {'line': line(ai), 'needs_confirmation': problem(ai)}
