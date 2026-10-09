"""Desktop pause barrier: finish admitted work, deny new automatic/critical work.

Does not cancel paid generations or flash workers. No configuration, paid
ledger, or deduplication state is reset by pause/resume.
"""
from __future__ import annotations

import contextlib
import functools
import os
import threading


class WorkGate:
    def __init__(self, paused=False):
        self._lock = threading.RLock()
        self.paused = paused
        self.active = 0
        self._local = threading.local()

    def pause(self):
        with self._lock:
            self.paused = True

    def resume(self):
        with self._lock:
            self.paused = False

    @contextlib.contextmanager
    def work(self, *, critical=False):
        with self._lock:
            # Nested synchronous calls belong to the already-admitted task.
            depth = getattr(self._local, 'depth', 0)
            admitted = not self.paused or depth > 0
            if admitted:
                self.active += 1
                self._local.depth = depth + 1
        if not admitted and critical:
            raise RuntimeError('后台已暂停或正在安全停止，请先恢复服务。')
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    self.active -= 1
                    self._local.depth -= 1

    def wrap(self, function, *, critical=False):
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            with self.work(critical=critical) as admitted:
                if admitted:
                    return function(*args, **kwargs)
        return guarded


gate = WorkGate(os.environ.get('INKSIGHT_DESKTOP_PAUSED') == '1')
critical_entry = lambda function: gate.wrap(function, critical=True)


def status():
    from . import firmware_tasks, manual_issue
    with gate._lock:
        # Observe the in-memory admission counter and durable queued jobs in
        # one barrier. Otherwise a creator could enqueue between file reads
        # and releasing its admission, producing a false idle snapshot.
        critical = 0
        error = False
        try:
            def count(jobs):
                if not isinstance(jobs, dict) or any(
                    not isinstance(job, dict) or not isinstance(job.get('status'), str)
                    for job in jobs.values()
                ):
                    raise ValueError('invalid task state')
                return sum(job['status'] in {'queued', 'running', 'waiting_heartbeat'}
                           for job in jobs.values())
            firmware = firmware_tasks._read()
            for group in ('builds', 'flashes', 'rollbacks'):
                critical += count(firmware.get(group, {}))
            critical += count(manual_issue._read().get('tasks', {}))
        except Exception:
            # Corrupt/unreadable state cannot be interpreted as "no flash".
            error = True
        return {'paused': gate.paused, 'active': gate.active,
                'critical': critical, 'state_error': error,
                'safe_to_stop': gate.paused and gate.active == 0 and critical == 0 and not error}
