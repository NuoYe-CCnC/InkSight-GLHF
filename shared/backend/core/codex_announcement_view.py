"""Pure optional v1/v2 display contract; never changes actual quota."""
import math
import re
import time

KINDS = ('quota_reset', 'reset_grant', 'reset_unspecified')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def safe_source(value, author, post_id):
    if not all(isinstance(v,str) for v in (value,author,post_id)):return None
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}',author) or not re.fullmatch(r'[0-9]{5,24}',post_id):return None
    # Full-string allowlist rejects normalization, userinfo, ports and queries.
    return value if value in {f'https://{host}/{author}/status/{post_id}' for host in ('x.com','twitter.com')} else None


def valid_event(event, kind, version, now):
    if not isinstance(event,dict) or event.get('kind')!=kind or event.get('status')!='pending' or event.get('confidence')!='explicit':return False
    expiry,valid,published=(event.get(k) for k in ('expires_at','valid_until','published_at'))
    if not all(finite(v) for v in (expiry,valid,published)) or not 0<published<=now<valid<=expiry<=published+14*86400:return False
    target=event.get('target_at')
    if version==1:return finite(target) and valid<=target==expiry
    if not (isinstance(event.get('event_id'),str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',event['event_id'])
            and isinstance(event.get('revision'),str) and re.fullmatch(r'[a-f0-9]{24}',event['revision'])
            and safe_source(event.get('canonical_source_url'),event.get('source_author'),event.get('post_id'))):return False
    time_type=event.get('time_type')
    if time_type=='exact':return finite(target) and valid<=target==expiry and event.get('time_relation') in {'at','before'}
    if target is not None:return False
    if time_type=='unknown':
        origin=event.get('expiry_origin_at')
        return finite(origin) and 0<origin<=published and expiry<=origin+86400
    if time_type=='date_range':
        lo,hi=event.get('range_start_at'),event.get('range_end_at')
        if not (finite(lo) and finite(hi) and 0<lo<hi==expiry and hi-lo<=25*3600):return False
        date=event.get('display_date')
        if date is not None:
            from datetime import datetime,timezone,timedelta
            zone=timezone(timedelta(hours=8))
            if date!=datetime.fromtimestamp(lo,zone).strftime('%Y-%m-%d') or date!=datetime.fromtimestamp(hi-1,zone).strftime('%Y-%m-%d'):return False
        return True
    return False


def visible_events(snapshot, now=None):
    now=time.time() if now is None else now
    if not isinstance(snapshot,dict) or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] not in {1,2} or snapshot.get('enabled') is not True:return {}
    version=snapshot['schema_version']
    if version==2:
        event=snapshot.get('selected_event')
        if not isinstance(event,dict) or event.get('kind') not in KINDS or not valid_event(event,event['kind'],2,now):return {}
        if snapshot.get(event['kind'])!=event or snapshot.get('canonical_source_url')!=event['canonical_source_url'] or snapshot.get('target_author')!=event.get('source_author'):return {}
        return {event['kind']:event}
    return {kind:event for kind in KINDS if valid_event(event:=snapshot.get(kind),kind,1,now)}


def nearest_event(snapshot, now=None, *, show_reset=True):
    events=visible_events(snapshot,now)
    if not show_reset:events.pop('reset_grant',None)
    if not events:return None
    return min(events.values(),key=lambda e:(e.get('time_type','exact')!='exact',e.get('target_at') or -e['published_at'],-e['published_at'],e['kind']))


def display_lines(event):
    from datetime import datetime,timezone,timedelta
    verb='发放' if event['kind']=='reset_grant' else '重置'
    if event.get('time_type','exact')=='exact':
        stamp=datetime.fromtimestamp(event['target_at'],timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        return '预计',stamp+' '+('前' if event.get('time_relation','before')!='at' else '')+verb
    date=event.get('display_date')
    return '预计'+verb,(date[5:]+'，' if date else '')+'时间待定'
