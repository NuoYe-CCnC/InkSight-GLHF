"""Experimental public HTML link discovery, never an announcement authority.

Fixed HTTPS origins only, no redirects/credentials/scripts, 512KiB cap. Each
source has its own persistent >=2h cadence/backoff. Robots are checked before
HTML; unavailable robots fail closed. No public stable API is assumed.
"""
from __future__ import annotations

import copy
import math
import time
from .codex_announcement_network import failure_reason, retry_seconds, summary
from html.parser import HTMLParser
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

SITES = {'whenreset': ('whenreset.dev', '/codex'),
         'nextreset': ('nextreset.ai', '/history/')}
AGENT = 'InkSight-experimental-announcements'
MAX_BYTES = 512*1024


class DiscoveryError(Exception):
    def __init__(self, reason, status=None, retry_after=0, *, detail=None):
        self.reason, self.status, self.retry_after = reason, status, retry_after
        self.detail = detail
        super().__init__(reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DiscoveryError('redirect_rejected')


def request(name, path, timeout):
    host, page = SITES[name]
    if path not in {'/robots.txt', page}:
        raise DiscoveryError('path_rejected')
    stage='connect'
    try:
        req=urllib.request.Request('https://'+host+path,headers={
            'User-Agent':AGENT+'/2','Accept':'text/html,text/plain'},method='GET')
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as response:
            stage='read'
            data=response.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES: raise DiscoveryError('response_too_large')
            if response.headers.get_content_type() not in {'text/plain','text/html'}:
                raise DiscoveryError('content_type_rejected')
            return data.decode('utf8',errors='strict')
    except urllib.error.HTTPError as exc:
        # 404 robots means no restrictions under RFC9309; 401/403 never bypass.
        if path=='/robots.txt' and exc.code==404: return 'User-agent: *\nAllow: /'
        retry=retry_seconds(exc.headers.get('Retry-After'),time.time())
        raise DiscoveryError('http_error',exc.code,retry,detail=summary(exc)) from None
    except UnicodeError as exc:
        raise DiscoveryError('decode',detail=summary(exc)) from None
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        reason=failure_reason(exc)
        if reason=='timeout':reason='read_timeout' if stage=='read' else 'connect_timeout'
        raise DiscoveryError(reason,detail=summary(exc)) from None



class Links(HTMLParser):
    def __init__(self, handle):
        super().__init__(convert_charrefs=True)
        self.handle,self.ids,self.text,self.skip=handle.lower(),[],[],0
        self.windows={};self.current=None
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}: self.skip+=1
        if tag!='a' or self.skip: return
        href=dict(attrs).get('href','')
        try:
            url=urllib.parse.urlsplit(href)
            match=re.fullmatch(r'/([A-Za-z0-9_]{1,15})/status/([0-9]{5,24})/?',url.path)
            if (url.scheme=='https' and url.hostname in {'x.com','twitter.com'}
                    and not url.username and not url.password and not url.port and not url.query and not url.fragment
                    and match and match[1].lower()==self.handle and match[2] not in self.ids):
                self.ids.append(match[2])
                self.current=match[2]
                self.windows[self.current]=' '.join(self.text[-8:])[-240:]
        except ValueError: pass
    def handle_endtag(self,tag):
        if tag in {'script','style'}: self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip and sum(map(len,self.text))<12000: self.text.append(data)
        if not self.skip and self.current and len(self.windows[self.current])<480:
            self.windows[self.current]=(self.windows[self.current]+' '+data)[:480]


def extract(text, handle, limit):
    if len(text.encode())>MAX_BYTES: raise DiscoveryError('response_too_large')
    parser=Links(handle);parser.feed(text)
    # Snowflakes sort newest first; archive forecasts/statistics never become
    # event fields. Only primary author links enter the verification queue.
    ids=sorted(parser.ids,key=int,reverse=True)[:limit]
    return {'post_ids':ids,'capability':'experimental-html-discovery',
            'field_hints':{ident:{'original_url':f'https://x.com/{handle}/status/{ident}',
                'untrusted_excerpt':parser.windows.get(ident,''),
                'zones':list(dict.fromkeys(re.findall(r'\b(?:PST|PDT|UTC(?:[+-]\d{1,2})?)\b',parser.windows.get(ident,'')))),
                'clocks':list(dict.fromkeys(re.findall(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b',parser.windows.get(ident,'')))),
                'accepted_as_event_field':False} for ident in ids},
            'stable_machine_api':False,'authoritative':False,
            'time_policy':'primary literal zone only; third-party dates never substituted',
            'historical_case_studies':bool(re.search(r'not a live feed|historical case studies',' '.join(parser.text),re.I))}


def due(previous,cfg,now,*,allow_retry=True):
    return any(cfg.get(name+'_enabled',False) and now>=row.get('next_allowed_at',0)
               and (allow_retry or not row.get('retry_cycle'))
               for name in SITES for row in [(previous or {}).get(name,{})])


def collect(previous, cfg, now, *, requester=None, allow_retry=True, checkpoint=None):
    """Persistent per-source cycle: retries at failure+10m/+30m, at most two.

    Callers only permit night work at existing authorized primary opportunities.
    A daylight retry crossing night waits for daylight; it never wakes the Mac.
    """
    result=copy.deepcopy(previous or {});requester=requester or request
    for name,(host,page) in SITES.items():
        row=result.setdefault(name,{})
        if not cfg.get(name+'_enabled',False):row.update(state='disabled',enabled=False);continue
        row['enabled']=True
        if now<row.get('next_allowed_at',0):continue
        cycle=row.get('retry_cycle')
        if cycle and not allow_retry:continue
        if cycle:cycle['attempts']+=1
        interval=max(7200,cfg.get('auxiliary_interval_minutes',120)*60)
        row.update(last_attempt_at=now,next_allowed_at=now+interval,source_url='https://'+host+page,stable_machine_api=False)
        if checkpoint:checkpoint(result)  # Reserve this attempt durably before I/O.
        stage='robots';started=time.monotonic()
        try:
            if not row.get('robots_checked_at') or now-row['robots_checked_at']>=86400:
                robots=requester(name,'/robots.txt',cfg.get('auxiliary_timeout_seconds',6))
                robot=urllib.robotparser.RobotFileParser();robot.parse(robots.splitlines())
                if not robot.can_fetch(AGENT,'https://'+host+page):raise DiscoveryError('robots_disallowed',403)
                delay=robot.crawl_delay(AGENT) or robot.crawl_delay('*') or 0
                row.update(robots_checked_at=now,robots_allowed=True)
                row['next_allowed_at']=max(row['next_allowed_at'],now+delay)
                if delay>0:row.update(state='robots_cooldown');continue
            stage='page';text=requester(name,page,cfg.get('auxiliary_timeout_seconds',6))
            stage='parse';found=extract(text,cfg['target_handle'],cfg.get('auxiliary_max_links',8))
            row.update(found,state='current' if found['post_ids'] else 'no_links',last_success_at=now,failures=0,
                       retry_cycle=None,last_http_status=200,last_error=None)
        except DiscoveryError as error:
            if error.reason=='robots_disallowed':row['robots_allowed']=False
            failures=min(20,row.get('failures',0)+1)
            row.update(state=error.reason,last_http_status=error.status,failures=failures,
                       last_error=dict(reason=error.reason,stage=stage,status=error.status,summary=error.detail))
            transient=error.reason in {'dns','connection','timeout','connect_timeout','read_timeout','unknown_network'} or error.status in {500,502,503,504}
            if transient:
                if not cycle:cycle=dict(started_at=now,attempts=0)
                if cycle['attempts']<2:
                    row.update(retry_cycle=cycle,next_allowed_at=cycle['started_at']+(600 if cycle['attempts']==0 else 1800))
                else:row.update(retry_cycle=None,next_allowed_at=now+interval)
            else:
                row.update(retry_cycle=None,next_allowed_at=max(now+min(86400,interval*2**min(failures-1,4)),
                    now+min(7*86400,error.retry_after),now+(86400 if error.status in {401,403} else 0)))
        finally:
            row['request_stage']=stage;row['elapsed_ms']=round((time.monotonic()-started)*1000,1)
            row['attempt_history']=(row.get('attempt_history',[])+[dict(at=now,stage=stage,state=row.get('state'),
                status=row.get('last_http_status'),elapsed_ms=row['elapsed_ms'],error=row.get('last_error'))])[-12:]
    return result


def diagnostics(previous,cfg):
    return {name:{'enabled':bool(cfg.get(name+'_enabled',False)),
                  'state':'not_checked' if cfg.get(name+'_enabled',False) else 'disabled',
                  'capability':'experimental-html-discovery','stable_machine_api':False,
                  'source_url':'https://'+host+page,
                  **(previous or {}).get(name,{})}
            for name,(host,page) in SITES.items()}
