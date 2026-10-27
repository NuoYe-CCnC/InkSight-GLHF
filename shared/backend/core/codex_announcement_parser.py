"""Conservative, deterministic public-post parsing. Text is data, never commands.

Only explicit Codex event context + future time can create a forecast. No LLM,
translation service, X credential, account mutation, or inferred author timezone.
Unknown wording is deliberately omitted and recorded with a reason.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

PARSER_VERSION = 2
PLANS = {"free", "plus", "pro", "business", "team", "enterprise", "edu"}
_ID = re.compile(r"^[0-9]{5,24}$")
_USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_NEGATIVE = re.compile(r"\b(?:not|never|won['’]t|no longer|cancelled|canceled|withdrawn)\b|取消|撤回|不会")
_CONDITIONAL = re.compile(r"\b(?:if|might|may|could|would|hypothetical|rumou?r|joking|some users|selected users|eligible users)\b|假如|可能|假设|部分用户")
_FUTURE = re.compile(r"\b(?:will|going to|plan(?:ning)? to|scheduled|expect|land(?:s|ing)?|arriv\w*|by|before|within)\b|将|预计|计划|之前|前发|前补")
_COMPLETE = re.compile(r"\b(?:already|just reset|have reset|has been reset|were reset|delivered|granted|completed|done|shipped|landed|propagated)\b|已完成|已发放|已重置")
_GRANT = re.compile(r"(?:manual|extra|additional|rate.limit|reset)[ -]*(?:reset[ -]*)?(?:credits?|opportunities|cards?)|\b(?:extra|additional|more) resets?\b|手动重置|重置次数|重置卡", re.I)
_RESET = re.compile(r"(?:reset|replenish|refill|refresh)\w*.{0,70}(?:limits?|quota|usage|windows?)|(?:limits?|quota|usage).{0,70}(?:reset|replenish|refill)|重置.{0,20}(?:额度|用量)|补充额度", re.I | re.S)
_ZONES = {
    "UTC": timezone.utc, "GMT": timezone.utc,
    "PST": timezone(timedelta(hours=-8)), "PDT": timezone(timedelta(hours=-7)),
    "EST": timezone(timedelta(hours=-5)), "EDT": timezone(timedelta(hours=-4)),
    "北京时间": timezone(timedelta(hours=8)),
}
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6,
            "eight": 8, "ten": 10, "twelve": 12, "twenty-four": 24}
_MONTHS = {name: i for i, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def normalize_plan(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if re.fullmatch(r"pro(?:[ _-]?(?:100|200))?", text):
        return "pro"
    if text == "team":
        return "business"
    return text if text in PLANS else None


def normalize_posts(payload, target: str, now: float, *, max_posts=100,
                    max_age_seconds=7 * 86400, author_id=None) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("results"), list):
        raise ValueError("unsupported_timeline_schema")
    posts, rejected = [], []
    for row in payload["results"][:max_posts]:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or "")
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        if (row.get("type") != "status" or row.get('retweet') or row.get('retweeted_status')
                or str(row.get('text') or '').startswith('RT @')
                or str(author.get("screen_name") or "").lower() != target.lower()
                or (author_id is not None and str(author.get("id")) != author_id)):
            rejected.append({"post_id": ident[:24], "reason": "not_target_author"})
            continue
        body, stamp = row.get("text"), row.get("created_timestamp")
        if (not _ID.fullmatch(ident) or not isinstance(body, str)
                or not body.strip() or len(body.encode("utf-8")) > 16000
                or not finite(stamp) or not 0 <= now - stamp <= max_age_seconds):
            rejected.append({"post_id": ident[:24], "reason": "invalid_or_old_post"})
            continue
        try:
            url = urlsplit(str(row.get("url") or ""))
            if (url.scheme != "https" or url.hostname not in {"x.com", "twitter.com"}
                    or url.username or url.password or url.port
                    or url.path.lower() != f"/{target}/status/{ident}".lower()):
                raise ValueError("url")
        except ValueError:
            rejected.append({"post_id": ident, "reason": "invalid_post_url"})
            continue
        reply = row.get("replying_to") if isinstance(row.get("replying_to"), dict) else {}
        parent = str(reply.get("status") or "")
        own_parent = str(reply.get("screen_name") or "").lower() == target.lower()
        posts.append({"id": ident, "text": body.strip(), "published_at": int(stamp),
                      "url": f"https://x.com/{target}/status/{ident}",
                      "author": target.lower(), "author_id": str(author.get("id") or "")[:32],
                      "parent_id": parent if own_parent and _ID.fullmatch(parent) else None})
    return posts, rejected


def _plans(text: str) -> list[str]:
    if re.search(r'\bpaid\s+(?:codex\s+)?(?:users?|accounts?|plans?|subscribers?)\b',text,re.I):
        return ['business','edu','enterprise','plus','pro']
    if re.search(r"\ball (?:codex (?:users|plans)|plans)\b|\beveryone (?:using|on) codex\b|所有\s*Codex\s*用户|全部\s*Codex\s*用户", text, re.I):
        return ["all"]
    token = r"(?:free|plus|pro(?:[ -]?(?:100|200))?|business|enterprise|edu|team)"
    # 'free reset credits' is an adjective, not the Free subscription tier.
    # Recognize explicit plan labels/lists, not arbitrary occurrences of Pro.
    scopes = re.finditer(r"\bcodex\s+(" + token + r")\b|\b(" + token +
        r"(?:\s*(?:,|/|and|or|&)\s*" + token + r")*)\s+(?:codex\s+)?(?:users?|plans?|subscribers?|accounts?|tiers?)\b", text, re.I)
    plans = set()
    for scope in scopes:
        plans.update(normalize_plan(m.group(0)) for m in re.finditer(r"\b" + token + r"\b", scope.group(1) or scope.group(2), re.I))
    return sorted(plans)


def _kinds(text: str) -> list[str]:
    result = []
    if _GRANT.search(text):
        result.append("reset_grant")
    explicit_reset = re.search(r"(?:reset|refill|replenish)\w*\s+(?:all |your |the )?(?:(?:weekly|codex|7.day|rate) )?(?:limits?|quota|usage)|(?:quota|rate limits?)\s+(?:will|is going to|are going to)\s+(?:be )?reset|重置.{0,8}(?:周额度|七日额度)", text, re.I)
    if _RESET.search(text) and (not result or explicit_reset):
        result.append("quota_reset")
    if not result and re.search(r'\bcodex (?:users? )?(?:resets? (?:will|is|are|lands?)|will (?:get |receive )?(?:a )?reset)|\breset (?:will|is going to) (?:land|arrive)|\breset (?:is )?(?:scheduled|planned)',text,re.I):
        result.append('reset_unspecified')
    return result


def explicit_deadline(text: str, published_at: int) -> tuple[int | None, str]:
    """Relative dates use publication time in the explicitly stated zone."""
    durations = list(re.finditer(r"\b(?:in|within|over the next)\s+(\d{1,3}|" + "|".join(_NUMBERS) + r")\s+(minutes?|hours?|days?)\b", text, re.I))
    # Never assign the first of several deadlines to the wrong event.
    if len(durations) > 1 or (durations and re.search(r"\d{1,2}:\d{2}|\d\s*(?:am|pm)\b|\btomorrow\b|\btoday\b", text, re.I)):
        return None, "multiple_times"
    duration = durations[0] if durations else None
    if duration:
        raw = duration.group(1).lower()
        number = int(raw) if raw.isdigit() else _NUMBERS[raw]
        unit = duration.group(2).lower()
        seconds = number * (60 if unit.startswith("minute") else 3600 if unit.startswith("hour") else 86400)
        return (published_at + seconds, "publication_relative_duration") if 0 < seconds <= 7 * 86400 else (None, "time_out_of_range")
    iso = re.findall(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})\b", text)
    if len(iso) == 1:
        try:
            return int(datetime.fromisoformat(iso[0].replace("Z", "+00:00")).timestamp()), "explicit_iso_time"
        except ValueError:
            return None, "invalid_time"
    if len(iso) > 1:
        return None, "multiple_times"
    zones = re.findall(r"(?<![A-Za-z])(?:PT|PST|PDT|EST|EDT|UTC|GMT)(?![A-Za-z])|北京时间", text, re.I)
    zones = list(dict.fromkeys(z.upper() if z != "北京时间" else z for z in zones))
    if len(zones) != 1:
        return None, "timezone_missing_or_ambiguous"
    zone = ZoneInfo("America/Los_Angeles") if zones[0] == "PT" else _ZONES[zones[0]]
    base = datetime.fromtimestamp(published_at, zone)
    lowered = text.lower()
    day = None
    if len(re.findall(r"\btomorrow\b|\btoday\b|明天|今天|今日", lowered)) > 1:
        return None, "multiple_dates"
    if re.search(r"\btomorrow\b|明天", lowered):
        day = base.date() + timedelta(days=1)
    elif re.search(r"\btoday\b|今天|今日", lowered):
        day = base.date()
    else:
        date_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        month_match = re.search(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", lowered)
        try:
            if date_match:
                day = datetime(*(int(x) for x in date_match.groups())).date()
            elif month_match:
                day = datetime(int(month_match.group(3) or base.year), _MONTHS[month_match.group(1)], int(month_match.group(2))).date()
        except ValueError:
            return None, "invalid_date"
    if day is None:
        return None, "date_missing"
    if re.search(r"\bend of (?:today|tomorrow|the day)\b|当天结束前|今天结束前|明天结束前", lowered):
        return int(datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).timestamp()), "explicit_day_end"
    clocks = re.findall(r"(?<![\d:-])(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", lowered)
    if not clocks:
        clocks = [(m.group(1), m.group(2), "") for m in re.finditer(r"(?<![\d:-])([01]?\d|2[0-3]):([0-5]\d)(?![\d:+-])", lowered)]
    if len(clocks) != 1:
        return None, "clock_missing_or_ambiguous"
    hour, minute, meridiem = clocks[0]
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            return None, "invalid_clock"
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    try:
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        # Reject nonexistent/ambiguous wall times around PT DST transitions.
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            return None, "ambiguous_dst_clock"
        return int(local.timestamp()), "explicit_zone_and_publication_date"
    except ValueError:
        return None, "invalid_clock"


def parse_posts(posts: list[dict], *, now: float, max_horizon_seconds=7 * 86400) -> tuple[list[dict], list[dict]]:
    """Produce revisions; scope can only be inherited along the author's replies."""
    by_id = {p["id"]: p for p in posts}
    revisions, rejected = [], []
    for post in sorted(posts, key=lambda p: (p["published_at"], int(p["id"]))):
        chain, seen, parent = [], set(), post
        while parent and parent["id"] not in seen and len(chain) < 5:
            chain.append(parent); seen.add(parent["id"])
            ancestor = by_id.get(parent.get("parent_id"))
            parent = ancestor if (ancestor and ancestor["author"] == post["author"]
                                  and 0 <= post["published_at"] - ancestor["published_at"] <= 7 * 86400) else None
        chain.reverse()
        text = post["text"]
        context = "\n".join(p["text"] for p in chain)
        # A numbered own-author reset series can supply product context to its
        # adjacent instalment, never to arbitrary standalone reset jokes.
        series_anchor = None
        day = re.search(r'\bday\s+(\d{1,2})\s*[/：:]', context, re.I)
        if day and re.search(r'\breset\b', context, re.I):
            for candidate in posts:
                other_day = re.search(r'\bday\s+(\d{1,2})\s*[/：:]', candidate['text'], re.I)
                if (other_day and candidate['author'] == post['author']
                        and candidate.get('author_id') == post.get('author_id')
                        and abs(int(other_day[1])-int(day[1])) <= 1
                        and abs(candidate['published_at']-post['published_at']) <= 3*86400
                        and re.search(r'\bcodex\b', candidate['text'], re.I)
                        and re.search(r'\breset\b', candidate['text'], re.I)):
                    series_anchor = candidate
                    break
        lower = text.lower()
        kinds = _kinds(text) or next((_kinds(p["text"]) for p in reversed(chain[:-1]) if _kinds(p["text"])), [])
        if not kinds and day and series_anchor and re.search(r'\breset\b', context, re.I):
            kinds = ['reset_unspecified']
        plans = _plans(text) or next((_plans(p["text"]) for p in reversed(chain[:-1]) if _plans(p["text"])), [])
        excluded = sorted(set(normalize_plan(m[1]) for m in re.finditer(
            r'\b(?:except|excluding|not for)\s+(free|plus|pro|business|team|enterprise|edu)\b', context, re.I)))
        evidence_ids = [p['id'] for p in chain]
        if series_anchor and series_anchor['id'] not in evidence_ids:
            evidence_ids.append(series_anchor['id'])
        base = {"post_id": post["id"], "root_id": chain[0]["id"], "source_url": post["url"],
                "published_at": post["published_at"], "plans": plans, "parser_version": PARSER_VERSION,
                'excluded_plans': excluded, 'plan_scope': 'explicit' if plans else 'unspecified',
                'time_relation': 'before' if re.search(r'\b(?:before|by|within|over the next)\b|之前|前发|前补', text, re.I) else 'at',
                'source_time_zone': next(iter(re.findall(r'\b(?:PST|PDT|PT|UTC|GMT|EST|EDT)\b',text,re.I)),None),
                'field_sources': {'time': post['url'], 'scope': [p['url'] for p in chain],
                                 'product_context': series_anchor['url'] if series_anchor else chain[0]['url']},
                "evidence": {"post_ids": evidence_ids, "text": text[:4000]},
                "confidence": "explicit"}
        reason = None
        if not re.search(r"\bcodex\b", context, re.I) and not series_anchor: reason = "codex_scope_missing"
        elif not kinds: reason = "event_kind_missing"
        elif "quota_reset" in kinds and re.search(r"\b(?:5[ -]?hours?|five[ -]hours?)\b", context, re.I) and not re.search(r"\b(?:weekly|7[ -]?days?|seven[ -]days?|all (?:usage )?(?:limits|windows))\b", context, re.I):
            reason = "unsupported_quota_window"
        elif _CONDITIONAL.search(lower): reason = "conditional_or_uncertain"
        elif len(chain) > 1 and any(_CONDITIONAL.search(p["text"].lower()) for p in chain[:-1]):
            reason = "uncertain_parent_context"
        elif len(chain) > 1 and any(_NEGATIVE.search(p["text"].lower()) or
                (_completed(p['text']) and not _FUTURE.search(p["text"].lower())) for p in chain[:-1]):
            reason = "retired_parent_context"
        if reason:
            if reason == "conditional_or_uncertain" and len(chain) > 1 and kinds and re.search(r"\b(?:correction|update|instead|postpon\w*)\b|更正|延期|改为", lower):
                revisions.extend(dict(base, kind=k, status="uncertain", target_at=None, time_basis=reason) for k in kinds)
            rejected.append({"post_id": post["id"], "reason": reason}); continue
        terminal = "retracted" if _NEGATIVE.search(lower) else "fulfilled" if _completed(text) and not _FUTURE.search(lower) else None
        if terminal:
            # A terminal statement can only retire its own known reply chain.
            if len(chain) > 1:
                revisions.extend(dict(base, kind=k, status=terminal, target_at=None, time_basis=terminal) for k in kinds)
            else:
                rejected.append({"post_id": post["id"], "reason": "negative_or_past_not_a_forecast"})
            continue
        if not _FUTURE.search(lower):
            rejected.append({"post_id": post["id"], "reason": "future_intent_missing"}); continue
        target_at, basis = explicit_deadline(text, post["published_at"])
        if target_at is None or not now < target_at <= post["published_at"] + max_horizon_seconds:
            if target_at is not None and target_at <= now:
                revisions.extend(dict(base, kind=k, status="expired", target_at=target_at, time_basis=basis) for k in kinds)
            elif len(chain) > 1 and re.search(r"\b(?:correction|update|instead|reschedul\w*|postpon\w*)\b|更正|延期|改为", lower):
                revisions.extend(dict(base, kind=k, status="uncertain", target_at=None, time_basis=basis) for k in kinds)
            rejected.append({"post_id": post["id"], "reason": basis if target_at is None else "expired_or_far_future"}); continue
        revisions.extend(dict(base, kind=k, status="pending", target_at=target_at, expires_at=target_at, time_basis=basis) for k in kinds)
    return revisions, rejected


def _completed(text):
    # Shipping another product in a multi-topic series post is not delivery of
    # a reset. Short own-chain completion replies remain terminal.
    if len(text)>100:
        for verb in ['shipped','landed','propagated']:
            if re.search(r'\b'+verb+r'\b',text,re.I) and not re.search(r'\b'+verb+r'\b.{0,35}(?:reset|credits?|quota)|(?:reset|credits?|quota).{0,35}\b'+verb+r'\b',text,re.I):
                text=re.sub(r'\b'+verb+r'\b','',text,flags=re.I)
    return _COMPLETE.search(text.lower())


def select_events(revisions: list[dict], *, now: float, plan: str | None) -> dict:
    """Newest revision per root/kind first, then earliest future event per kind."""
    latest = {}
    for row in sorted(revisions, key=lambda r: (r["published_at"], int(r["post_id"]))):
        key = row['root_id']
        if key not in latest or latest[key][0]['post_id'] != row['post_id']:
            latest[key] = []
        latest[key].append(row)
    result = {}
    for row in (item for group in latest.values() for item in group):
        if (row.get("status") != "pending" or not finite(row.get("target_at"))
                or row["target_at"] <= now or (plan and plan in row.get('excluded_plans', []))
                or (row['plans'] and not ("all" in row["plans"] or plan in row["plans"]))):
            continue
        old = result.get(row["kind"])
        if old is None or (row["target_at"], -row["published_at"]) < (old["target_at"], -old["published_at"]):
            result[row["kind"]] = row
    return result
