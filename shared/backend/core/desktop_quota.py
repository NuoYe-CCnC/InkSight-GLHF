"""Read-only menu quota. Never probe Codex or refresh a successful timestamp."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from . import codex_collector_health, data_cache, desktop_service, state_store
from .freshness_config import ai_source_policy


def account_key() -> str | None:
    """Compare local login identity without returning any credential or id."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        identity = tokens.get("account_id") if isinstance(tokens, dict) else None
        if not isinstance(identity, str) or not identity:
            return None
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    except (OSError, ValueError, TypeError):
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


GUARD_FILE = state_store.state_path('desktop_quota_guard.json')


def _auth_boundary(now):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        return min(math.floor(now), math.floor((home / "auth.json").stat().st_mtime))
    except OSError:
        return math.floor(now)


def quota_snapshot_base(*, now: float | None = None) -> dict:
    from . import state_store
    now = time.time() if now is None else now
    ttl = min(1800, ai_source_policy()["codex"]["stale_after_s"])
    result = {"remaining_percent": None, "last_success_at": None,
              "expires_at": None, "max_age_seconds": ttl, "resets_at": None,
              "source": "Codex 本机采集 · 7D", "reason": "unavailable",
              "retrying": False, "paused": desktop_service.gate.paused}
    identity = account_key()
    try:
        with state_store.file_lock(GUARD_FILE, operation="menu-quota"):
            guard, error = state_store.read_json(GUARD_FILE)
            if error == 'missing': guard = {'identity': identity, 'not_before': 0}
            elif error or not isinstance(guard, dict):
                return dict(result, reason='guard_unavailable')
            before = json.dumps(guard, sort_keys=True)
            if guard.get('identity') != identity:
                guard.update(identity=identity, not_before=_auth_boundary(now))
            if not identity and guard.get('display_state') != 'identity_unknown':
                guard['not_before'] = max(guard.get('not_before', 0), math.floor(now))
            group = data_cache.get_group("ai.codex") or {}
            value = group.get("value")
            health = codex_collector_health.snapshot(now=int(now), stale_after=ttl)
            reason = None
            if not identity: reason = 'identity_unknown'
            elif not isinstance(value, dict): reason = 'unavailable'
            elif identity != value.get('account_key'): reason = 'account_mismatch'
            elif value.get('source') != 'mac': reason = 'wrong_source'
            elif not _number(value.get('duration_minutes')) or value['duration_minutes'] != 10080: reason = 'missing_7d'
            elif not _number(value.get('used_percent')) or not 0 <= value['used_percent'] <= 100: reason = 'invalid_percent'
            elif group.get('status') not in {'fresh', 'stale'}: reason = 'unavailable'
            stamp = group.get('last_success')
            if reason is None and (not _number(stamp) or not 0 < stamp <= now): reason = 'invalid_timestamp'
            if reason is None and _number(health.get('last_success_at')) and health['last_success_at'] > now + 2: reason = 'invalid_timestamp'
            if reason is None and stamp < guard.get('not_before', 0): reason = 'identity_boundary'
            if reason is None:
                result['last_success_at'] = stamp
                bounds = [stamp + ttl]
                for expires in [value.get('expires_at'), group.get('expires_at'), (group.get('meta') or {}).get('expires_at')]:
                    if expires is not None:
                        if not _number(expires) or expires <= stamp: reason = 'invalid_timestamp'; break
                        bounds.append(expires)
                reset = value.get('resets_at')
                if reset is not None:
                    if not _number(reset) or reset <= 0: reason = 'invalid_reset'
                    else:
                        result['resets_at'] = reset
                        bounds.append(reset)
                expiry = min(bounds)
                result['expires_at'] = expiry
                attempt = health.get('last_attempt_at') or 0
                missing = health.get('reason') == 'missing_7d' and attempt >= math.floor(stamp)
                missing = missing or ((group.get('meta') or {}).get('last_fail') in {'no 7D window in codex payload', 'no 7D window'} and group.get('last_attempt', 0) >= stamp)
                if missing: reason = 'missing_7d'
                elif now >= expiry: reason = 'reset_boundary' if reset is not None and now >= reset else 'stale'
                else:
                    # Cache writes precede collector-health writes. Health is a
                    # diagnostic, not a second success clock for the source value.
                    result.update(remaining_percent=math.floor(100-value['used_percent']+0.5),
                                  retrying=group.get('status') != 'fresh' or health.get('status') != 'current')
            if reason in {'account_mismatch','wrong_source','missing_7d','invalid_percent','invalid_reset','unavailable','invalid_timestamp'}:
                # Persist invalidation, so a late replay of an older source
                # sample cannot restore the number after an observed boundary.
                guard['not_before'] = max(guard.get('not_before', 0), math.floor(now))
            result['reason'] = reason
            state = reason or ('retrying' if result['retrying'] else 'current')
            if guard.get('display_state') != state:
                old = guard.get('display_state')
                since = guard.get('state_since', now)
                guard.update(display_state=state, state_since=now)
                guard['transitions'] = (guard.get('transitions', []) + [dict(
                    at=now, previous=old, reason=state, cause=health.get("reason") if health.get("reason") in codex_collector_health.REASONS else ("source_retry" if result["retrying"] else reason), duration_seconds=max(0, now-since))])[-100:]
            if json.dumps(guard, sort_keys=True) != before or error == 'missing':
                state_store.write_json(GUARD_FILE, guard)
    except (state_store.StateStoreError, OSError, ValueError, TypeError):
        return dict(result, remaining_percent=None, reason='guard_unavailable')
    return result


def snapshot(*, now: float | None = None) -> dict:
    result = quota_snapshot_base(now=now)
    # Forecast failures cannot corrupt the independent real 7D quota result.
    try:
        from .codex_announcements import snapshot as announcement_snapshot
        result["announcements"] = announcement_snapshot(now=now)
    except Exception:
        result["announcements"] = None
    return result
