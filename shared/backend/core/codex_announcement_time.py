"""Publication-relative event time, with an expiry independent of its target.

Unknown time is a real variant, never a sentinel timestamp. PST/PDT are literal
fixed offsets; PT is the IANA zone. No timezone is inferred from an author.
"""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

BJ = timezone(timedelta(hours=8))
ZONES = {"UTC": timezone.utc, "GMT": timezone.utc,
         "PST": timezone(timedelta(hours=-8)), "PDT": timezone(timedelta(hours=-7)),
         "EST": timezone(timedelta(hours=-5)), "EDT": timezone(timedelta(hours=-4)),
         "北京时间": BJ, "PT": ZoneInfo("America/Los_Angeles")}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6,
           "eight": 8, "ten": 10, "twelve": 12, "twenty-four": 24}
MONTHS = {name: i for i, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
ZONE_PATTERN = r"(?<![A-Za-z])(?:PT|PST|PDT|EST|EDT|UTC|GMT)(?![A-Za-z])|北京时间"


def parse_time(text, published_at, *, parents=(), original_at=None, horizon=7*86400):
    """Parents are chronological same-event, identity-checked ancestors only."""
    start = original_at if original_at is not None else published_at
    result = dict(time_type="unknown", target_at=None, range_start_at=None,
                  range_end_at=None, display_date=None, expires_at=start+86400,
                  time_basis="time_unspecified", source_time_zone=None,
                  time_relation="before" if re.search(r"\b(?:before|by|within|in|over (?:the )?next)\b|之前|结束前", text, re.I) else "at")

    def exact(stamp, basis):
        if not 0 < stamp-published_at <= horizon:
            # Past exact times remain exact so the caller can record expiry.
            if stamp > published_at: return dict(result, time_basis="time_out_of_range")
        return dict(result, time_type="exact", target_at=int(stamp),
                    expires_at=int(stamp), time_basis=basis)

    durations = list(re.finditer(r"\b(?:in|within|over (?:the )?next)\s+(\d{1,3}|"+"|".join(NUMBERS)+r")\s+(minutes?|hours?|days?)\b", text, re.I))
    if len(durations)>1:
        return dict(result,time_basis="multiple_times")
    if durations:
        if re.search(r"\d{1,2}:\d{2}|\d\s*(?:am|pm)\b|\btomorrow\b|\btoday\b", text, re.I):
            return dict(result,time_basis="multiple_times")
        match=durations[0]; number=int(match[1]) if match[1].isdigit() else NUMBERS[match[1].lower()]
        unit=match[2].lower(); seconds=number*(60 if unit.startswith('minute') else 3600 if unit.startswith('hour') else 86400)
        return exact(published_at+seconds,"publication_relative_duration") if 0<seconds<=horizon else dict(result,time_basis="time_out_of_range")
    iso=re.findall(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})\b",text)
    if iso:
        try:
            return exact(datetime.fromisoformat(iso[0].replace('Z','+00:00')).timestamp(),'explicit_iso_time') if len(iso)==1 else dict(result,time_basis='multiple_times')
        except ValueError:return dict(result,time_basis='invalid_time')
    zones=list(dict.fromkeys(z.upper() if z!='北京时间' else z for z in re.findall(ZONE_PATTERN,text,re.I)))
    if not zones:
        # A parent's explicit zone may be inherited within this event only.
        zones=list(dict.fromkeys(z.upper() if z!='北京时间' else z for p in parents for z in re.findall(ZONE_PATTERN,p['text'],re.I)))
    parent_zones=list(dict.fromkeys(z.upper() if z!='北京时间' else z for p in parents for z in re.findall(ZONE_PATTERN,p['text'],re.I)))
    if zones and parent_zones and any(z not in zones for z in parent_zones) and not re.search(r'\b(?:correction|instead|update)\b|更正',text,re.I):
        return dict(result,time_basis='timezone_conflicting_parent')
    if len(zones)!=1:return dict(result,time_basis='timezone_missing_or_ambiguous')
    zone=ZONES[zones[0]];result['source_time_zone']=zones[0]
    lowered=text.lower();base=datetime.fromtimestamp(published_at,zone)
    date_pattern=r"\btomorrow\b|\btoday\b|\btonight\b|明天|今天|今日|今晚"
    date_text=lowered;date_origin=published_at
    markers=re.findall(date_pattern,date_text)
    # A parent's "today" must not silently mean yesterday after a reply
    # crosses its source-zone date. Conflicts preserve a credible unknown plan.
    if not markers and not re.search(r"\d{4}-\d{2}-\d{2}|\b(?:"+'|'.join(MONTHS)+r")\s+\d",date_text):
        for parent in reversed(parents):
            if re.search(date_pattern,parent['text'],re.I):
                if datetime.fromtimestamp(parent['published_at'],zone).date()!=base.date():
                    return dict(result,time_basis='parent_date_crosses_day')
                date_text=parent['text'].lower();date_origin=parent['published_at'];markers=re.findall(date_pattern,date_text);break
    if len(markers)>1:return dict(result,time_basis='multiple_dates')
    date_base=datetime.fromtimestamp(date_origin,zone);day=None
    try:
        if re.search(r"\btomorrow\b|明天",date_text):day=date_base.date()+timedelta(days=1)
        elif re.search(r"\btoday\b|\btonight\b|今天|今日|今晚",date_text):day=date_base.date()
        else:
            numeric=re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b",date_text)
            month=re.search(r"\b("+'|'.join(MONTHS)+r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",date_text)
            if numeric:day=datetime(*(int(x) for x in numeric.groups())).date()
            elif month:day=datetime(int(month[3] or date_base.year),MONTHS[month[1]],int(month[2])).date()
    except ValueError:return dict(result,time_basis='invalid_date')
    if day is None:return dict(result,time_basis='date_missing')
    day_start=datetime.combine(day,datetime.min.time(),zone)
    day_end=datetime.combine(day+timedelta(days=1),datetime.min.time(),zone)
    if day_end.timestamp()>published_at+horizon:return dict(result,time_basis='time_out_of_range')
    # "by midnight" is end-of-day, not the start of the same day.
    end_phrase=re.search(r"\b(?:end of (?:today|tomorrow|the day)|by midnight|before midnight)\b|(?:今天|明天|当天)结束前",lowered)
    if end_phrase:return exact(day_end.timestamp(),'explicit_day_end')
    clocks=re.findall(r"(?<![\d:-])(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b",lowered)
    if not clocks:clocks=[(m[1],m[2],'') for m in re.finditer(r"(?<![\d:-])([01]?\d|2[0-3]):([0-5]\d)(?![\d:+-])",lowered)]
    if re.search(r'\bnoon\b',lowered):clocks.append(('12','0',''))
    if re.search(r'\bmidnight\b',lowered):
        if re.search(r'\btonight\b|今晚',lowered):return exact(day_end.timestamp(),'explicit_day_end')
        return dict(result,time_basis='midnight_boundary_ambiguous')
    if len(clocks)>1:return dict(result,time_basis='clock_missing_or_ambiguous')
    if clocks:
        hour,minute,meridiem=clocks[0];hour=int(hour);minute=int(minute or 0)
        if meridiem:
            if not 1<=hour<=12:return dict(result,time_basis='invalid_clock')
            hour=hour%12+(12 if meridiem=='pm' else 0)
        local=datetime(day.year,day.month,day.day,hour,minute,tzinfo=zone)
        if local.replace(fold=0).utcoffset()!=local.replace(fold=1).utcoffset():return dict(result,time_basis='ambiguous_dst_clock')
        return exact(local.timestamp(),'explicit_zone_and_publication_date')
    # A date-only event has a true interval and independent expiry. Show a
    # Beijing date only if every instant in the source interval falls on it.
    lo,hi=day_start.timestamp(),day_end.timestamp()
    bj_first=datetime.fromtimestamp(lo,BJ).date();bj_last=datetime.fromtimestamp(hi-1,BJ).date()
    return dict(result,time_type='date_range',range_start_at=int(lo),range_end_at=int(hi),
                expires_at=int(hi),display_date=bj_first.isoformat() if bj_first==bj_last else None,
                time_basis='explicit_source_date_range')
