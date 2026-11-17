"""Own-author banked plans and fractional progress, never account receipt evidence."""
import hashlib
import re
from .codex_announcement_time import parse_time

def revisions(posts, now, series_records=()):
    rows=[];plans=[]
    for post in sorted(posts,key=lambda p:(p['published_at'],int(p['id']))):
        text=post['text']
        if re.search(r'\b(?:I demand|please|could you|fellow resetters)\b',text,re.I):
            continue
        future=re.search(r'\bnext\s+(two|2)\s+days\s+will\s+be\s+resets?\b.{0,35}\bbanked\b',text,re.I|re.S)
        if future:
            # An explicit series supplies Codex context. A standalone generic
            # banked financial remark is not promoted to a Codex announcement.
            number=re.search(r'\bday\s*(\d+)\s*[/：:]',text,re.I)
            anchor=next((r for r in series_records if number and r.get('author')==post['author']
                         and r.get('author_id')==post['author_id'] and r.get('created_at',0)<=post['published_at']<r.get('expires_at',0)),None)
            if not re.search(r'\bcodex\b',text,re.I) and not anchor:
                continue
            timing=parse_time('within two days',post['published_at'])
            timing.update(time_relation='before',time_basis='literal_next_two_days_plan_deadline',expiry_origin_at=post['published_at'])
            plans.append({'root':post,'latest':post,'timing':timing,'completed':0,'total':2,'posts':[post]})
        progress=re.search(r'\bbanked\s*(\d+)\s*/\s*(\d+)\b',text,re.I)
        if progress:
            done,total=map(int,progress.groups())
            candidates=[p for p in plans if p['total']==total and p['root']['author_id']==post['author_id']
                        and 0<=post['published_at']-p['root']['published_at']<=3*86400]
            if len(candidates)!=1 or not 0<done<=total:
                continue
            plan=candidates[0]
            if done<plan['completed']:
                continue
            plan.update(completed=done,latest=post);plan['posts'].append(post)
        else:
            plan=plans[-1] if future and plans and plans[-1]['root']['id']==post['id'] else None
            if not plan and re.search(r'cancel|withdrawn|delay|postpon|completed|done',text,re.I):
                candidates=[p for p in plans if p['root']['author_id']==post['author_id']
                            and 0<=post['published_at']-p['root']['published_at']<=3*86400
                            and (post.get('parent_id') in {r['id'] for r in p['posts']} or re.search(r'banked',text,re.I))]
                if len(candidates)==1:
                    plan=candidates[0];plan['latest']=post;plan['posts'].append(post)
                    if re.search(r'cancel|withdrawn',text,re.I):plan['terminal']='retracted'
                    elif re.search(r'delay|postpon',text,re.I):
                        plan['timing']=parse_time(text,post['published_at'],parents=plan['posts'][:-1],original_at=post['published_at'])
                        plan['timing']['expiry_origin_at']=post['published_at']
                    elif re.search(r'completed|done',text,re.I):plan['terminal']='fulfilled'
        if not plan:
            continue
        root=plan['root'];latest=plan['latest'];timing=plan['timing']
        ids=[p['id'] for p in plan['posts']]
        status=plan.get('terminal') or ('fulfilled' if plan['completed']==plan['total'] else 'pending' if timing['expires_at']>now else 'expired')
        rows.append(dict(post_id=latest['id'],root_id=root['id'],event_id=f"{root['author_id']}:{root['id']}",
            revision=hashlib.sha256(('banked-plan-v1\n'+'\n'.join(p['text'] for p in plan['posts'])).encode()).hexdigest()[:24],
            source_url=latest['url'],source_author=latest['author'],published_at=latest['published_at'],original_published_at=root['published_at'],
            plans=[],excluded_plans=[],parser_version='banked-plan-v1',plan_scope='unspecified',confidence='explicit',
            kind='reset_grant',status=status,grant_count=plan['total']-plan['completed'],
            stages=[dict(ordinal=i,status='fulfilled' if i<=plan['completed'] else 'pending') for i in range(1,plan['total']+1)],
            related_post_ids=ids,field_sources={'kind':root['url'],'time':root['url'],'progress':latest['url']},
            evidence={'post_ids':ids,'text':latest['text'][:4000]},**timing))
    return rows
