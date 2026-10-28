"""Conservative, deterministic public-post parsing. Text is data, never commands.

Only verified Codex event context + a credible plan can create a forecast. No LLM,
translation service, X credential, account mutation, or inferred author timezone.
Unknown time stays nullable with a bounded TTL; unsupported intent is rejected.
"""
from __future__ import annotations

import math
import hashlib
import html
from .codex_announcement_time import parse_time
import re
from urllib.parse import urlsplit

PARSER_VERSION = 3
PLANS = {"free", "plus", "pro", "business", "team", "enterprise", "edu"}
_ID = re.compile(r"^[0-9]{5,24}$")
_USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_NEGATIVE = re.compile(r"\b(?:not|never|won['’]t|no longer|cancelled|canceled|withdrawn)\b|取消|撤回|不会")
_CONDITIONAL = re.compile(r"\b(?:if|might|may|could|would|hypothetical|rumou?r|joking|some users|selected users|eligible users)\b|假如|可能|假设|部分用户")
_FUTURE = re.compile(r"\b(?:will|going to|plan(?:ning)? to|scheduled|expect|land(?:s|ing)?|arriv\w*|by|before|within)\b|将|预计|计划|之前|前发|前补")
_COMPLETE = re.compile(r"\b(?:already|just reset|have reset|has been reset|were reset|delivered|granted|completed|done|shipped|landed|propagated)\b|已完成|已发放|已重置")
_GRANT = re.compile(r"(?:manual|extra|additional|rate.limit|reset)[ -]*(?:reset[ -]*)?(?:credits?|opportunities|cards?)|\b(?:extra|additional|more) resets?\b|手动重置|重置次数|重置卡", re.I)
_RESET = re.compile(r"(?:reset|replenish|refill|refresh)\w*.{0,70}(?:limits?|quota|usage|windows?)|(?:limits?|quota|usage).{0,70}(?:reset|replenish|refill)|重置.{0,20}(?:额度|用量)|补充额度", re.I | re.S)


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
                    or url.username or url.password or url.port or url.query or url.fragment
                    or url.path.lower() != f"/{target}/status/{ident}".lower()):
                raise ValueError("url")
        except ValueError:
            rejected.append({"post_id": ident, "reason": "invalid_post_url"})
            continue
        reply = row.get("replying_to") if isinstance(row.get("replying_to"), dict) else {}
        parent = str(reply.get("status") or "")
        own_parent = str(reply.get("screen_name") or "").lower() == target.lower()
        posts.append({"id": ident, "text": html.unescape(body).strip(), "published_at": int(stamp),
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


def canonical_source_url(value, author, post_id=None):
    """Exact public author/status URL; no query, fragment, credentials or port."""
    if not isinstance(value,str) or not isinstance(author,str) or not _USER.fullmatch(author):return None
    if any(ord(c)<33 or ord(c)>126 for c in value):return None
    try:
        url=urlsplit(value)
        match=re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/([0-9]{5,24})',url.path)
        if (url.scheme!='https' or url.netloc not in {'x.com','twitter.com'} or url.query or url.fragment
                or not match or match[1].lower()!=author.lower() or (post_id and match[2]!=post_id)):return None
        return f'https://x.com/{author.lower()}/status/{match[2]}'
    except ValueError:return None


def explicit_deadline(text, published_at):
    result=parse_time(text,published_at)
    return result['target_at'],result['time_basis']


def _semantic(text):
    # In "Just kidding, today is a reset", the correction overrides the joke
    # before it. "a reset ... just kidding" instead retires that statement.
    text=html.unescape(text)
    pivot=list(re.finditer(r"\bjust kidding\b[,!.:—\s]*",text,re.I))
    if pivot:
        tail=text[pivot[-1].end():].strip()
        if re.search(r"\b(?:today|tomorrow|reset|will|plan|land)\b",tail,re.I):return tail
        return 'joking'
    text=re.sub(r"\bnot today\s*[,;]?\s*but\s+(tomorrow)\b",r"\1",text,flags=re.I)
    return text


def _day(text):
    m=re.search(r'\bday\s+(\d{1,3})\s*[/：:]',text,re.I)
    return int(m[1]) if m and 0<int(m[1])<=366 else None


def _series_product(text):
    number=_day(text);text=_semantic(text)
    return bool(number is not None and re.search(r'\bcodex\b',text,re.I) and re.search(r'\breset\w*\b',text,re.I)
                and not _CONDITIONAL.search(text.lower()) and not re.search(r'\bnot (?:about |for )?codex\b|\b(?:joke|haha)\b',text,re.I))


def series_state(posts, previous=None):
    """Bounded Day series: 14d initial/rolling window, 90d absolute cap.

    A gap can inherit background while the record is alive, but only observed
    consecutive numbers extend its rolling expiry. Explicit end or an explicit
    different reset product stops the series. Records never carry event time.
    """
    records={}
    for row in (previous or []):
        if (isinstance(row,dict) and row.get('version')==1 and row.get('anchor_post_id')
                and finite(row.get('created_at')) and finite(row.get('expires_at'))
                and row['expires_at']<=row['created_at']+90*86400
                and isinstance(row.get('anchor_text'),str) and _series_product(row['anchor_text'])
                and canonical_source_url(row.get('anchor_url'),row.get('author'),row.get('anchor_post_id'))):
            records[(row['author'],row['author_id'])]=dict(row)
    contexts={}
    for post in sorted(posts,key=lambda x:(x['published_at'],int(x['id']))):
        number=_day(post['text'])
        if number is None:continue
        key=(post['author'],post.get('author_id',''));stamp=post['published_at'];text=_semantic(post['text'])
        row=records.get(key)
        if row and stamp<row['created_at']:continue
        end=re.search(r'\b(?:series (?:ends?|over)|last day|no more (?:codex )?resets|end of (?:the )?series)\b',text,re.I)
        other=re.search(r'\b(?:password|router|database|iphone|calendar)\b.{0,24}\breset|\breset.{0,24}\b(?:password|router|database|iphone|calendar)\b',text,re.I)
        if row and row['anchor_post_id']==post['id'] and not _series_product(post['text']):
            row.update(expires_at=min(row['expires_at'],stamp),ended_at=stamp,termination_reason='anchor_revised')
            continue
        if row and (end or (other and not re.search(r'\bcodex\b',text,re.I))):
            row.update(expires_at=min(row['expires_at'],stamp),ended_at=stamp,termination_reason='explicit_end' if end else 'topic_changed')
            continue
        if _series_product(post['text']):
            if row is None or stamp>=row['expires_at'] or number<=row['last_number']:
                row=dict(version=1,series_id=f"{post['author_id']}:day:{post['id']}",author=post['author'],author_id=post.get('author_id',''),
                    anchor_post_id=post['id'],anchor_url=post['url'],anchor_text=post['text'][:4000],created_at=stamp,
                    expires_at=stamp+14*86400,last_number=number,last_published_at=stamp,termination_reason=None)
                records[key]=row
        if row and not row.get('termination_reason') and row['created_at']<=stamp<row['expires_at'] and number>=row['last_number']:
            if number==row['last_number']+1:
                row['expires_at']=min(stamp+14*86400,row['created_at']+90*86400)
            contexts[post['id']]=dict(row)
            row.update(last_number=number,last_published_at=stamp)
    # Retrospective product-only context is allowed for an older numbered
    # installment in the initial 14d corpus; never borrow its deadline or plan.
    anchors=[p for p in posts if _series_product(p['text'])]
    for post in posts:
        if post['id'] in contexts or _day(post['text']) is None:continue
        for anchor in sorted(anchors,key=lambda p:abs(p['published_at']-post['published_at'])):
            if (post['author']==anchor['author'] and post.get('author_id')==anchor.get('author_id')
                    and 0<anchor['published_at']-post['published_at']<=14*86400 and _day(post['text'])<_day(anchor['text'])):
                if any(post['published_at']<p['published_at']<anchor['published_at'] and p['author']==post['author']
                    and re.search(r'\b(?:series (?:ends?|over)|last day|no more resets)\b',p['text'],re.I) for p in posts):continue
                contexts[post['id']]=dict(series_id=f"{anchor.get('author_id','')}:day:{anchor['id']}",
                    anchor_post_id=anchor['id'],anchor_url=anchor['url'])
                break
    return list(records.values())[-8:],contexts


def parse_posts(posts, *, now, max_horizon_seconds=7*86400, series_records=None):
    by_id={p['id']:p for p in posts};revisions=[];rejected=[]
    _,series=series_state(posts,series_records)
    for post in sorted(posts,key=lambda p:(p['published_at'],int(p['id']))):
        chain=[];seen=set();parent=post;chain_error=None
        while parent:
            if parent['id'] in seen:chain_error='reply_cycle';break
            if len(chain)>=5:chain_error='reply_chain_too_long';break
            chain.append(parent);seen.add(parent['id'])
            ancestor=by_id.get(parent.get('parent_id'))
            if ancestor and (ancestor['author']!=post['author'] or ancestor.get('author_id')!=post.get('author_id')
                    or not 0<=parent['published_at']-ancestor['published_at']<=7*86400):
                chain_error='invalid_reply_identity_or_order';break
            parent=ancestor
        if chain_error:
            rejected.append(dict(post_id=post['id'],reason=chain_error));continue
        chain.reverse();text=_semantic(post['text']);context='\n'.join(_semantic(p['text']) for p in chain)
        anchor=next((series[p['id']] for p in reversed(chain) if p['id'] in series),None)
        kinds=_kinds(text) or next((_kinds(_semantic(p['text'])) for p in reversed(chain[:-1]) if _kinds(_semantic(p['text']))),[])
        reset_plan=re.search(r"\b(?:today is a reset|reset day (?:today|tomorrow)|that[’']?s a reset|reset (?:is )?(?:scheduled|planned)|(?:codex|we).{0,35}(?:will|plan).{0,30}reset)\b",context,re.I)
        if not kinds and reset_plan:kinds=['reset_unspecified']
        plans=next((_plans(re.sub(r'\b(?:except|excluding|not for)\s+(?:free|plus|pro|business|team|enterprise|edu)(?: users?)?', '',_semantic(p['text']),flags=re.I)) for p in reversed(chain)
                    if _plans(re.sub(r'\b(?:except|excluding|not for)\s+(?:free|plus|pro|business|team|enterprise|edu)(?: users?)?', '',_semantic(p['text']),flags=re.I))),[])
        excluded=sorted(set(normalize_plan(m[1]) for m in re.finditer(r'\b(?:except|excluding|not for)\s+(free|plus|pro|business|team|enterprise|edu)\b',context,re.I)))
        evidence=[p['id'] for p in chain]
        if anchor and anchor['anchor_post_id'] not in evidence:evidence.append(anchor['anchor_post_id'])
        correction=bool(re.search(r'\b(?:correction|update|instead|reschedul\w*|postpon\w*|delayed)\b|更正|延期|改为',text,re.I))
        terminal='retracted' if re.search(r"\b(?:cancelled|canceled|withdrawn|no longer|won[’']t|not happening)\b|取消|撤回|不会",text,re.I) else 'fulfilled' if _completed(text) and not _FUTURE.search(text.lower()) else None
        if not terminal and _NEGATIVE.search(text.lower()) and not re.search(r'\b(?:except|excluding|not for)\b',text,re.I):terminal='retracted'
        reason=None
        if not re.search(r'\bcodex\b',context,re.I) and not anchor:reason='codex_scope_missing'
        elif not kinds:reason='event_kind_missing'
        elif 'quota_reset' in kinds and re.search(r'\b(?:5[ -]?hours?|five[ -]hours?)\b',context,re.I) and not re.search(r'\b(?:weekly|7[ -]?days?|seven[ -]days?|all (?:usage )?(?:limits|windows))\b',context,re.I):reason='unsupported_quota_window'
        elif re.search(r'\b(?:please|can you|could you|when will|haha|joke)\b',text,re.I):reason='request_or_joke'
        elif _CONDITIONAL.search(text.lower()):reason='conditional_or_uncertain'
        elif any(_CONDITIONAL.search(_semantic(p['text']).lower()) for p in chain[:-1]):reason='uncertain_parent_context'
        elif any(re.search(r'\b(?:cancelled|canceled|withdrawn)\b',_semantic(p['text']),re.I) or (_completed(_semantic(p['text'])) and not _FUTURE.search(_semantic(p['text']))) for p in chain[:-1]) and not correction:reason='retired_parent_context'
        base=dict(post_id=post['id'],root_id=chain[0]['id'],event_id=f"{post.get('author_id','')}:{chain[0]['id']}",
            revision=hashlib.sha256((post['id']+'\n'+context).encode()).hexdigest()[:24],source_url=post['url'],source_author=post['author'],
            published_at=post['published_at'],original_published_at=chain[0]['published_at'],plans=plans,excluded_plans=excluded,
            parser_version=PARSER_VERSION,plan_scope='explicit' if plans else 'unspecified',confidence='explicit',
            series_id=anchor['series_id'] if anchor else None,
            field_sources=dict(time=post['url'],scope=[p['url'] for p in chain],product_context=anchor['anchor_url'] if anchor else chain[0]['url']),
            evidence=dict(post_ids=evidence,text=post['text'][:4000]))
        if reason:
            if correction and len(chain)>1 and kinds:revisions.extend(dict(base,kind=k,status='uncertain',target_at=None,time_basis=reason) for k in kinds)
            rejected.append(dict(post_id=post['id'],reason=reason));continue
        if terminal:
            if len(chain)>1:revisions.extend(dict(base,kind=k,status=terminal,target_at=None,time_basis=terminal) for k in kinds)
            else:rejected.append(dict(post_id=post['id'],reason='negative_or_past_not_a_forecast'))
            continue
        intent=bool(_FUTURE.search(text.lower()) or reset_plan or (len(chain)>1 and correction) or re.search(r'\b(?:today|tomorrow|tonight)\b.{0,25}reset|\breset.{0,25}\b(?:today|tomorrow|tonight)\b',text,re.I))
        if not intent:
            rejected.append(dict(post_id=post['id'],reason='future_intent_missing'));continue
        origin=post['published_at'] if correction else chain[0]['published_at']
        timing=parse_time(text,post['published_at'],parents=chain[:-1],original_at=origin,horizon=max_horizon_seconds)
        if correction and re.search(r'\b(?:share|announce) (?:a )?new time\b',text,re.I):
            revisions.extend(dict(base,kind=k,status='uncertain',target_at=None,time_basis='schedule_withdrawn') for k in kinds)
            continue
        if timing['time_basis']=='time_out_of_range':
            rejected.append(dict(post_id=post['id'],reason='time_out_of_range'));continue
        timing['expiry_origin_at']=origin
        status='pending' if timing['expires_at']>now else 'expired'
        revisions.extend(dict(base,kind=k,status=status,**timing) for k in kinds)
        if timing['time_type']=='unknown' or status=='expired':rejected.append(dict(post_id=post['id'],reason=timing['time_basis'] if status=='pending' else 'expired_or_far_future',accepted=status=='pending'))
    return revisions,rejected


def _completed(text):
    # Shipping another product in a multi-topic series post is not delivery of
    # a reset. Short own-chain completion replies remain terminal.
    if len(text)>100:
        for verb in ['shipped','landed','propagated']:
            if re.search(r'\b'+verb+r'\b',text,re.I) and not re.search(r'\b'+verb+r'\b.{0,35}(?:reset|credits?|quota)|(?:reset|credits?|quota).{0,35}\b'+verb+r'\b',text,re.I):
                text=re.sub(r'\b'+verb+r'\b','',text,flags=re.I)
    return _COMPLETE.search(text.lower())


def event_sort_key(row):
    exact=finite(row.get('target_at')) and row.get('time_type','exact')=='exact'
    return (0 if exact else 1,row['target_at'] if exact else -row['published_at'],
            -row['published_at'],row.get('event_id',row.get('root_id','')),row['kind'])


def select_events(revisions, *, now, plan):
    latest={}
    for row in sorted(revisions,key=lambda r:(r['published_at'],int(r['post_id']))):
        key=row.get('event_id',row.get('root_id',''))
        if key not in latest or latest[key][0]['post_id']!=row['post_id']:latest[key]=[]
        latest[key].append(row)
    result={}
    for row in (item for group in latest.values() for item in group):
        expiry=row.get('expires_at',row.get('target_at'))
        if (row.get('status')!='pending' or not finite(expiry) or expiry<=now
                or (plan and plan in row.get('excluded_plans',[]))
                or (row['plans'] and 'all' not in row['plans'] and plan not in row['plans'])):continue
        old=result.get(row['kind'])
        if old is None or event_sort_key(row)<event_sort_key(old):result[row['kind']]=row
    return result
