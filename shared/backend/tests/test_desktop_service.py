from __future__ import annotations

import asyncio
import copy
import sys
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core import desktop_service, firmware_tasks, manual_issue
from api.routes.desktop import authorize, service


def test_pause_denies_new_work_but_never_cancels_admitted_work():
    gate = desktop_service.WorkGate()
    events = []
    with gate.work() as admitted:
        assert admitted
        assert gate.active == 1
        gate.pause()
        worker = threading.Thread(target=gate.wrap(lambda: events.append('unexpected')))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        # Same-thread nested work was already admitted, so it can finish.
        gate.wrap(lambda: events.append('nested'), critical=True)()
        events.append('completed')
        assert gate.active == 1
    assert gate.active == 0
    with pytest.raises(RuntimeError):
        gate.wrap(lambda: None, critical=True)()
    gate.resume()
    gate.wrap(lambda: events.append('resumed'))()
    assert events == ['nested', 'completed', 'resumed']


@pytest.mark.parametrize('start', [manual_issue.start, firmware_tasks.start_build,
                                  firmware_tasks.start_flash, firmware_tasks.start_rollback])
def test_worker_start_is_denied_while_paused(start):
    desktop_service.gate.pause()
    with pytest.raises(RuntimeError, match='暂停'):
        start('not-existing')


@pytest.mark.parametrize('jobs', [[], None, {'example': {}}, {'example': 'broken'}])
def test_malformed_jobs_fail_closed(jobs, monkeypatch):
    monkeypatch.setattr(firmware_tasks, '_read', lambda: {'flashes': jobs})
    desktop_service.gate.pause()
    assert desktop_service.status()['state_error']
    assert not desktop_service.status()['safe_to_stop']


def test_manual_worker_handoff_is_durably_queued_before_start(monkeypatch):
    task = {'id': 'example', 'status': 'uncertain'}
    monkeypatch.setattr(manual_issue, 'get', lambda _: dict(task))
    monkeypatch.setattr(manual_issue, '_set_status', lambda _, state: task.update(status=state))
    monkeypatch.setattr(firmware_tasks, '_read', lambda: {})
    monkeypatch.setattr(manual_issue, '_read', lambda: {'tasks': {'example': task}})
    class Worker:
        def __init__(self, **kwargs): pass
        def start(self):
            assert task['status'] == 'queued'
            desktop_service.gate.pause()
            assert not desktop_service.status()['safe_to_stop']
        def is_alive(self): return False
    monkeypatch.setattr(manual_issue.threading, 'Thread', Worker)
    monkeypatch.setattr(manual_issue, '_THREADS', {})
    manual_issue.start('example')
    assert not desktop_service.status()['safe_to_stop']


def test_work_exception_releases_inflight_counter():
    gate = desktop_service.WorkGate()
    def failure():
        raise ValueError('expected')
    with pytest.raises(ValueError):
        gate.wrap(failure)()
    assert gate.active == 0


@pytest.mark.parametrize('group', ['builds', 'flashes', 'rollbacks'])
@pytest.mark.parametrize('state', ['queued', 'running', 'waiting_heartbeat'])
def test_critical_jobs_block_exit(group, state, monkeypatch):
    monkeypatch.setattr(firmware_tasks, '_read', lambda: {group: {'example': {'status': state}}})
    monkeypatch.setattr(manual_issue, '_read', lambda: {'tasks': {}})
    desktop_service.gate.pause()
    result = desktop_service.status()
    assert result['critical'] == 1
    assert not result['safe_to_stop']


def test_corrupt_flash_state_fails_closed(monkeypatch):
    def failure():
        raise ValueError('corrupt')
    monkeypatch.setattr(firmware_tasks, '_read', failure)
    desktop_service.gate.pause()
    assert desktop_service.status()['state_error']
    assert not desktop_service.status()['safe_to_stop']


def test_pause_preserves_paid_ledgers_and_completed_tasks(monkeypatch):
    before = {'tasks': {'example': {'status': 'completed', 'paid': 2, 'done': True}}}
    state = copy.deepcopy(before)
    monkeypatch.setattr(firmware_tasks, '_read', lambda: {})
    monkeypatch.setattr(manual_issue, '_read', lambda: state)
    desktop_service.gate.pause()
    assert desktop_service.status()['safe_to_stop']
    desktop_service.gate.resume()
    assert not desktop_service.status()['safe_to_stop']
    assert state == before


def request(ip='127.0.0.1', token='unit-desktop-token'):
    return Request({'type': 'http', 'method': 'POST', 'path': '/',
                    'client': (ip, 1234), 'headers': [(b'x-inksight-desktop-token', token.encode())]})


@pytest.mark.parametrize('ip,token', [('192.0.2.2', 'unit-desktop-token'),
                                    ('127.0.0.1', 'wrong'), ('127.0.0.1', '')])
def test_control_is_loopback_and_per_launch_authenticated(ip, token, monkeypatch):
    monkeypatch.setenv('INKSIGHT_DESKTOP_TOKEN', 'unit-desktop-token')
    with pytest.raises(HTTPException) as error:
        authorize(request(ip, token))
    assert error.value.status_code == 403


def test_control_not_available_to_other_backend_deployments(monkeypatch):
    monkeypatch.delenv('INKSIGHT_DESKTOP_TOKEN', raising=False)
    with pytest.raises(HTTPException):
        authorize(request())


def test_control_pause_status_resume(monkeypatch):
    monkeypatch.setenv('INKSIGHT_DESKTOP_TOKEN', 'unit-desktop-token')
    monkeypatch.setattr(firmware_tasks, '_read', lambda: {})
    monkeypatch.setattr(manual_issue, '_read', lambda: {})
    previous_loop = asyncio.get_event_loop()
    try:
        assert asyncio.run(service('pause', request()))['safe_to_stop']
        assert asyncio.run(service('status', request()))['paused']
        assert not asyncio.run(service('resume', request()))['paused']
    finally:
        # Python 3.9's Lock imports bind to the current loop. Do not let this
        # endpoint test's asyncio.run alter subsequent unrelated imports.
        asyncio.set_event_loop(previous_loop)


def test_new_flash_or_news_creation_rejected_before_mutation():
    desktop_service.gate.pause()
    with pytest.raises(RuntimeError):
        firmware_tasks.create_flash('not-existing', 'not-existing')
    with pytest.raises(RuntimeError):
        manual_issue.create('example-idempotency')


def test_publisher_graceful_stop_finishes_current_cycle(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
    import host_cycle
    events = []
    monkeypatch.setattr(sys, 'argv', ['host_cycle.py', '--mac', 'AABBCCDDEEFF'])
    monkeypatch.setattr(host_cycle, 'load_policy', lambda: host_cycle.HostPolicy())
    monkeypatch.setattr(host_cycle.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(host_cycle, '_stop_child', lambda: pytest.fail('must not interrupt admitted child'))
    def cycle(*args, **kwargs):
        events.append('start')
        host_cycle._request_stop(None, None)
        events.append('completed')
    monkeypatch.setattr(host_cycle, 'cycle', cycle)
    host_cycle.main()
    assert events == ['start', 'completed']


@pytest.mark.parametrize('succeeded', [False, True])
def test_last_success_timestamp_only_advances_after_verified_publication(monkeypatch, succeeded):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
    import cloud_publish
    state = {'_desktop_publications': {'AABBCCDDEEFF': {'last_success_at': 10, 'publish_id': 'old'}}}
    monkeypatch.setattr(cloud_publish, 'load_state', lambda: state)
    monkeypatch.setattr(cloud_publish, 'save_state', lambda value: None)
    monkeypatch.setattr(cloud_publish, 'fetch_payload', lambda *args, **kwargs: {})
    monkeypatch.setattr(cloud_publish, 'combine_dashboard', lambda payloads: {
        'screen': {'modules': [], 'payload_id': 'example', 'publish_id': 'new'}})
    monkeypatch.setattr(cloud_publish, 'publish_one', lambda *args, **kwargs: succeeded)
    monkeypatch.setattr(cloud_publish, 'payload_id', lambda payload: 'example')
    monkeypatch.setattr(cloud_publish, 'publication_id', lambda payload: 'new')
    monkeypatch.setattr(cloud_publish.time, 'time', lambda: 100)
    cloud_publish._run_unlocked({'backend': 'http://127.0.0.1:18137', 'webdav': 'https://example.invalid',
        'devices': [{'mac': 'AABBCCDDEEFF', 'modes': ['AI_USAGE']}]}, once=True)
    assert state['_desktop_publications']['AABBCCDDEEFF']['last_success_at'] == (100 if succeeded else 10)
