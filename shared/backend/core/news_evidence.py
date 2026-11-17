"""Program-owned original-language excerpts, claim binding and bounded diagnostics."""
import html
import re
import unicodedata
from decimal import Decimal


def normalized(text):
    text=unicodedata.normalize('NFKC',html.unescape(str(text or '')))
    text = re.sub(r'\s+',' ',text.translate(str.maketrans({'“':'"','”':'"','‘':"'",'’':"'",'–':'-','—':'-','\u00ad':''}))).strip()
    return re.sub(r'(?<=[\u3400-\u9fff，。；：！？、]) (?=[\u3400-\u9fff，。；：！？、])','',text)


def options(candidates, summary_chars=80, title_chars=160):
    rows={}
    for i,c in enumerate(candidates,1):
        for kind,text in [('T',str(c.get('title') or '')[:title_chars]),('S',str(c.get('summary') or '')[:summary_chars])]:
            if text.strip():rows[f'E{i}{kind}']=dict(candidate_id=i,text=text)
    return rows


def _numbers(text):
    factors={'billion':10**9,'million':10**6,'trillion':10**12,'亿':10**8,'万':10**4,'千':10**3}
    out=set()
    for m in re.finditer(r'(?<![A-Za-z0-9])(\d+(?:[.,]\d+)*)(?:\s*(billion|million|trillion|亿|万|千))?',normalized(text),re.I):
        try:out.add(Decimal(m[1].replace(',',''))*factors.get((m[2] or '').lower(),1))
        except Exception:pass
    return out


def claim_for(body,candidate,explicit=None):
    if explicit:
        return explicit if normalized(explicit) in normalized(body) else None
    clauses=[s.strip() for s in re.split(r'[；。\n]',body) if s.strip()]
    material=normalized(str(candidate.get('title') or '')+' '+str(candidate.get('summary') or '')).casefold()
    def score(s):
        latin=re.findall(r'[A-Za-z][A-Za-z0-9.+/-]*',s)
        chinese=re.findall(r'[\u3400-\u9fff]{2,}',s)
        return sum(4 for t in latin if len(t)>1 and t.casefold() in material)+sum(1 for t in chinese for j in range(len(t)-1) if t[j:j+2] in material)
    return max(clauses,key=score,default='')


def check(body, candidate, event, evidence_map, candidate_id):
    ids=event.get('evidence_ids')
    if ids is None and 'evidence_id' in event:ids=[event['evidence_id']]
    if ids is not None:
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in evidence_map or evidence_map[i]['candidate_id']!=candidate_id for i in ids):
            return None,'evidence-id-invalid',None
        quote=' '.join(evidence_map[i]['text'] for i in ids)
    else:
        quote=str(event.get('evidence') or '')
        material=normalized(str(candidate.get('title') or '')+' '+str(candidate.get('summary') or ''))
        if not quote.strip():return None,'evidence-missing',None
        if normalized(quote).casefold() not in material.casefold():return None,'evidence-not-in-source',None
    claim=claim_for(body,candidate,event.get('claim'))
    if not claim:return None,'claim-not-in-body',None
    anchor=normalized(quote).casefold()
    latin=re.findall(r'[A-Za-z][A-Za-z0-9.+/-]*',claim)
    cjk=re.findall(r'[\u3400-\u9fff]{2}',claim)
    generic={'发布','推出','计划','宣布','模型','功能','产品','公司','平台','技术','服务','研究'}
    aliases={'苹果':'apple','微软':'microsoft','谷歌':'google','英伟达':'nvidia','英特尔':'intel','亚马逊':'amazon'}
    linked=any(len(t)>1 and t.casefold() in anchor for t in latin) or any(t not in generic and t in anchor for t in cjk) or any(k in claim and v in anchor for k,v in aliases.items())
    if not linked:return None,'claim-not-supported-by-excerpt',claim
    if not _numbers(claim)<=_numbers(quote):return None,'claim-number-not-supported',claim
    future=re.search(r'\b(?:will|plans?|expected|aims?|reportedly|rumou?r)\b|计划|预计|传闻|申请',quote,re.I)
    promoted=re.search(r'发布|上线|上市|开售',claim)
    if future and promoted and not re.search(r'将|计划|预计|传闻|申请|尚未',claim):return None,'claim-tense-not-supported',claim
    if re.search(r'\b(?:not|never)\b.{0,20}\b(?:release|launch|ship)|尚未发布|不会发布',quote,re.I) and promoted and not re.search(r'未|不',claim):
        return None,'claim-negation-not-supported',claim
    # Proper technical entities/names must retain the source spelling. Generic
    # English prose is handled separately, and translated Chinese is not a quote.
    for token in re.findall(r'\b(?:GPT|DeepSeek|Claude|Gemini|Llama)[- ]?[A-Za-z0-9.]+\b',claim,re.I):
        if re.sub(r'\s+','',token).casefold() not in re.sub(r'\s+','',quote).casefold():return None,'claim-entity-not-supported',claim
    return quote,None,claim


def covers_body(body,claims):
    text=normalized(body);covered=[False]*len(text)
    for claim in claims:
        part=normalized(claim);start=text.find(part) if part else -1
        if start>=0:
            covered[start:start+len(part)]=[True]*len(part)
    leftover=''.join(ch for ch,yes in zip(text,covered) if not yes)
    return not re.sub(r'[\W_]+','',leftover)
