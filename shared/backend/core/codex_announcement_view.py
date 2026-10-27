"""Pure, fail-closed validation of the optional versioned display snapshot."""
import math
import time


def visible_events(snapshot, now=None):
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1 or snapshot.get("enabled") is not True:
        return {}
    result = {}
    for kind in ("quota_reset", "reset_grant", 'reset_unspecified'):
        event = snapshot.get(kind)
        if not isinstance(event, dict) or event.get("kind") != kind or event.get("status") != "pending" or event.get("confidence") != "explicit":
            continue
        values = [event.get(k) for k in ("target_at", "expires_at", "valid_until", "published_at")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
            continue
        target, expiry, valid, published = values
        if not (0 < published <= now < valid <= target == expiry <= now + 14 * 86400):
            continue
        result[kind] = event
    return result


def nearest_event(snapshot, now=None, *, show_reset=True):
    events=visible_events(snapshot,now)
    if not show_reset: events.pop('reset_grant',None)
    if not events: return None
    return min(events.values(),key=lambda row:(row['target_at'],-row['published_at'],row['kind']))
