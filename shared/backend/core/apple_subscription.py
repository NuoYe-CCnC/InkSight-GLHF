"""Persisted scheduling and final-native-identity job exchange; never controls UI."""
import re
import time
import uuid
from . import state_store, automation_schedule as schedule, desktop_service

DEFAULTS = {'enabled': False}
STATE_FILE = state_store.state_path('apple_subscription.json')

def _config():
    from .operator_config import load_effective
    return load_effective().config.get('apple_subscription', DEFAULTS)

def _load():
    state, error = state_store.read_json(STATE_FILE)
    if error == 'missing':
        return {'schema': 1, 'record': None, 'checked_nodes': {}, 'last_attempt_at': 0}
    if error or not isinstance(state, dict) or state.get('schema') != 1:
        raise state_store.StateStoreError('subscription state unavailable')
    return state

def status(*, now=None, config=None):
    now = time.time() if now is None else now
    try:
        s = _load()
        enabled = (config or _config()).get('enabled', False)
        due = schedule.apple_due(s, now, enabled)
        record = s.get('record')
        label = '待首次读取'
        if record:
            label = ('已到期' if record.get('displayed_state') == 'expired' else
                     '有效至' if record.get('action_word') in {'到期', 'Expires'} else '续订日') + ' ' + record['display_date']
        if s.get('stale_since') or due['reason'] == 'date_needs_confirmation':
            label += ' · 待确认'
        elif record and record.get('displayed_state')!='expired' and record.get('schedule_anchor') and record['schedule_anchor']['date']<schedule.datetime.fromtimestamp(now,schedule.BJ).date().isoformat():
            label += ' · 状态待确认'
        reason=s.get('last_error') or due['reason']
        summary=('已关闭' if not enabled else '正在检查' if due['reason']=='in_progress' else
                 '请为正式 InkSight 开启辅助功能' if s.get('blocked')=='待授权' else
                 '请先处理 Apple 登录或认证，再手动检查' if s.get('blocked') else
                 '已有窗口正在使用，已推迟' if 'user_target_app' in reason else
                 '界面被接管，已停止操作并保留原窗口' if 'foreground' in reason or 'preserved' in reason else
                 '读取超时，保留上次记录并稍后重试' if 'timeout' in reason else
                 '最近读取成功' if s.get('last_result')=='success' else '按日程等待检查')
        return dict(enabled=enabled, label=label, summary=summary, record=record, last_success_at=s.get('last_success_at'),
                    last_attempt_at=s.get('last_attempt_at'), next_check_at=due.get('next_at'),
                    state=due['reason'], last_result=s.get('last_result'), source='Apple 官方订阅页',
                    busy=s.get('job', {}).get('status') in {'queued', 'running'}, last_error=s.get('last_error'))
    except state_store.StateStoreError:
        return dict(enabled=False, state='state_unavailable', label='日期状态不可用', busy=False)

def request(*, manual=False, now=None):
    now = time.time() if now is None else now
    if desktop_service.gate.paused:
        return {'accepted': False, 'reason': 'paused'}
    with state_store.file_lock(STATE_FILE, operation='subscription-job'):
        s = _load()
        before=dict(s)
        job = s.get('job') or {}
        if job.get('status') == 'running' and now >= job.get('lease_until', 0):
            # A crashed reader is not success and cannot immediately relaunch UI.
            s.update(job={}, retry_pending=True, next_allowed_at=now + 6 * schedule.HOUR,
                     last_error='reader_interrupted', last_result='failed')
        if manual and s.get('blocked'):
            # User request rechecks permission/auth state once, without any OS prompt.
            s['blocked'] = None
        due = schedule.apple_due(s, now, _config().get('enabled', False), manual)
        if not due['due']:
            if s!=before:state_store.write_json(STATE_FILE, s)
            return dict(accepted=False, reason=due['reason'])
        s['job'] = dict(id=uuid.uuid4().hex, status='queued', queued_at=now, manual=manual,
                        reason=due['reason'], lease_until=now + 120)
        state_store.write_json(STATE_FILE, s)
        return dict(accepted=True, job_id=s['job']['id'])

def claim(*, now=None):
    now = time.time() if now is None else now
    if desktop_service.gate.paused or not _config().get('enabled', False):
        return None
    request(now=now)
    with state_store.file_lock(STATE_FILE, operation='subscription-job'):
        s = _load()
        job = s.get('job') or {}
        if job.get('status') != 'queued':
            return None
        job.update(status='running', lease_until=now + 100)
        state_store.write_json(STATE_FILE, s)
        return dict(id=job['id'], mode='B')

def report_permission(trusted):
    with state_store.file_lock(STATE_FILE, operation='subscription-job'):
        s = _load()
        before=dict(s)
        s['accessibility_trusted'] = bool(trusted)
        if not trusted:
            s['blocked'] = '待授权'
        elif s.get('blocked') == '待授权':
            s['blocked'] = None
        if s!=before:state_store.write_json(STATE_FILE, s)

def active_jobs():
    job=_load().get('job') or {}
    return int(job.get('status') in {'queued', 'running'} and time.time()<job.get('lease_until',0))

def complete(job_id, result, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(result, dict):
        raise ValueError('invalid reader response')
    with state_store.file_lock(STATE_FILE, operation='subscription-job'):
        s = _load()
        job = s.get('job') or {}
        if job.get('id') != job_id or job.get('status') != 'running':
            return {'accepted': False, 'reason': 'obsolete_job'}
        s['job'] = {}
        outcome = str(result.get('outcome') or '')
        attempted = result.get('official_url_count') == 1
        if attempted:
            s['last_attempt_at'] = now
        row = result.get('extracted') or {}
        confirmed = (outcome == 'confirmed_ui_row_read' and result.get('normal_cleanup_complete') is True
                     and result.get('force_termination_used') is False
                     and result.get('probe_bundle') == 'cc.nuoye.inksight'
                     and row.get('product') == 'ChatGPT Plus'
                     and row.get('action_word') in {'续期', '续订', '到期', 'Renews', 'Expires', 'Expired'}
                     and isinstance(row.get('display_date'), str) and len(row['display_date']) < 50
                     and schedule.date_parts(row['display_date']) is not None)
        if confirmed:
            safe = {k: row.get(k) for k in ('product', 'display_date', 'action_word', 'renewal_or_expiry_wording')}
            safe['state_wording']=str(result.get('state_wording') or safe['renewal_or_expiry_wording'])[:120]
            safe['displayed_state'] = result.get('displayed_state') or ('expired' if row['action_word'] == 'Expired' else 'unknown')
            safe.update(source='Apple 官方订阅页', source_precision_only=True, observed_at=now)
            schedule.accept_apple(s, safe, now)
            s.update(last_result='success', last_error=None)
        else:
            s.update(last_result='skipped' if not attempted else 'failed', last_error=outcome[:100])
            if 'permission' in outcome or 'accessibility' in outcome:
                s.update(blocked='待授权', retry_pending=False)
            elif 'authentication' in outcome or 'transaction' in outcome or 'auth_' in outcome:
                s.update(blocked='待处理认证', retry_pending=False)
            elif attempted:
                s.update(retry_pending=True, next_allowed_at=now + 6 * schedule.HOUR)
            else:
                s['skip_until'] = now + 300
        state_store.write_json(STATE_FILE, s)
        return {'accepted': True, 'ok': confirmed, 'status': status(now=now)}
