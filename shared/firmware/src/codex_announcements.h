#pragma once
#include <ArduinoJson.h>
#include <math.h>
#include <string.h>
#include <stdio.h>
#include <time.h>

inline double announcementNumber(JsonVariantConst v) {
    if (v.is<bool>() || !(v.is<double>() || v.is<long long>())) return NAN;
    return v.as<double>();
}
inline bool announcementKind(const char *v) {
    return v && (!strcmp(v,"quota_reset") || !strcmp(v,"reset_grant") || !strcmp(v,"reset_unspecified"));
}
inline bool announcementToken(const char *v, int min, int max, bool numeric=false, bool hex=false) {
    if (!v || (int)strlen(v)<min || (int)strlen(v)>max) return false;
    for (const char *p=v; *p; p++) {
        bool ok=(*p>='0' && *p<='9');
        if (hex) ok=ok || (*p>='a' && *p<='f');
        else if (!numeric) ok=ok || (*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || *p=='_' || *p=='-' || *p==':';
        if (!ok) return false;
    }
    return true;
}
inline bool announcementSource(JsonObjectConst event) {
    const char *author=event["source_author"] | "", *id=event["post_id"] | "", *url=event["canonical_source_url"] | "";
    if (!announcementToken(author,1,15) || strchr(author,':') || strchr(author,'-') || !announcementToken(id,5,24,true)) return false;
    char expected[128];
    snprintf(expected,sizeof(expected),"https://x.com/%s/status/%s",author,id);
    if (!strcmp(expected,url)) return true;
    snprintf(expected,sizeof(expected),"https://twitter.com/%s/status/%s",author,id);
    return !strcmp(expected,url);
}
inline bool codexAnnouncementValid(JsonObjectConst e,const char *kind,int schema,time_t now) {
    if (!announcementKind(kind) || strcmp(e["kind"] | "",kind) || strcmp(e["status"] | "","pending") || strcmp(e["confidence"] | "","explicit")) return false;
    double target=announcementNumber(e["target_at"]),expiry=announcementNumber(e["expires_at"]),valid=announcementNumber(e["valid_until"]),published=announcementNumber(e["published_at"]);
    if (!isfinite(expiry) || !isfinite(valid) || !isfinite(published) || !(0<published && published<=now && now<valid && valid<=expiry && expiry<=published+14*86400)) return false;
    if (schema==1) return isfinite(target) && valid<=target && target==expiry;
    if (schema!=2 || !announcementSource(e) || !announcementToken(e["event_id"] | "",1,100) || !announcementToken(e["revision"] | "",24,24,false,true)) return false;
    const char *type=e["time_type"] | "";
    if (!strcmp(type,"exact")) {
        const char *relation=e["time_relation"] | "";
        return isfinite(target) && valid<=target && target==expiry && (!strcmp(relation,"at") || !strcmp(relation,"before"));
    }
    if (!e["target_at"].isNull()) return false;
    if (!strcmp(type,"unknown")) {
        double origin=announcementNumber(e["expiry_origin_at"]);
        return isfinite(origin) && 0<origin && origin<=published && expiry<=origin+86400;
    }
    if (!strcmp(type,"date_range")) {
        double lo=announcementNumber(e["range_start_at"]),hi=announcementNumber(e["range_end_at"]);
        if (!isfinite(lo) || !isfinite(hi) || !(0<lo && lo<hi && hi==expiry && hi-lo<=25*3600)) return false;
        if (!e["display_date"].isNull()) {
            if (!e["display_date"].is<const char *>()) return false;
            const char *date=e["display_date"];
            char first[16],last[16];struct tm a={},b={};time_t start=(time_t)lo+8*3600,end=(time_t)hi-1+8*3600;
            gmtime_r(&start,&a);gmtime_r(&end,&b);
            strftime(first,sizeof(first),"%Y-%m-%d",&a);strftime(last,sizeof(last),"%Y-%m-%d",&b);
            if (strcmp(first,date) || strcmp(last,date)) return false;
        }
        return true;
    }
    return false;
}
inline JsonObjectConst codexSelectedAnnouncement(JsonObjectConst ai,time_t now,bool showReset=true) {
    JsonObjectConst snap=ai["announcements"].as<JsonObjectConst>();
    if (!snap["schema_version"].is<int>() || snap["schema_version"].is<bool>() || !snap["enabled"].is<bool>() || !snap["enabled"].as<bool>()) return JsonObjectConst();
    int schema=snap["schema_version"];
    if (schema==2) {
        JsonObjectConst e=snap["selected_event"].as<JsonObjectConst>();const char *kind=e["kind"] | "";
        if (strcmp(snap["target_author"] | "",e["source_author"] | "") || (!showReset && !strcmp(kind,"reset_grant")) || !codexAnnouncementValid(e,kind,2,now) || strcmp(snap["canonical_source_url"] | "",e["canonical_source_url"] | "")) return JsonObjectConst();
        JsonObjectConst row=snap[kind].as<JsonObjectConst>();
        const char *keys[]={"kind","status","confidence","target_at","expires_at","valid_until","published_at","time_relation","time_type","event_id","revision","canonical_source_url","source_author","post_id","range_start_at","range_end_at","display_date","expiry_origin_at","plan_scope","source_time_zone"};
        for (const char *key:keys) if (e[key]!=row[key]) return JsonObjectConst();
        return e;
    }
    if (schema!=1) return JsonObjectConst();
    JsonObjectConst chosen;
    const char *kinds[]={"quota_reset","reset_grant","reset_unspecified"};
    for (const char *kind:kinds) {
        if (!showReset && !strcmp(kind,"reset_grant")) continue;
        JsonObjectConst e=snap[kind].as<JsonObjectConst>();
        if (!codexAnnouncementValid(e,kind,1,now)) continue;
        double target=announcementNumber(e["target_at"]),old=announcementNumber(chosen["target_at"]);
        if (chosen.isNull() || target<old || (target==old && (announcementNumber(e["published_at"])>announcementNumber(chosen["published_at"]) ||
                (announcementNumber(e["published_at"])==announcementNumber(chosen["published_at"]) && strcmp(kind,chosen["kind"] | "")<0)))) chosen=e;
    }
    return chosen;
}
// Legacy callers retain exact-target-only semantics. Unknown never becomes 0
// or another fabricated time in the v2 event itself.
inline time_t codexAnnouncementTarget(JsonObjectConst ai,const char *kind,time_t now) {
    JsonObjectConst snap=ai["announcements"].as<JsonObjectConst>();
    if (snap["schema_version"].is<int>() && snap["schema_version"].as<int>()==1 && snap["enabled"]==true) {
        JsonObjectConst e=snap[kind].as<JsonObjectConst>();
        return codexAnnouncementValid(e,kind,1,now) ? (time_t)announcementNumber(e["target_at"]) : 0;
    }
    JsonObjectConst e=codexSelectedAnnouncement(ai,now);
    return !e.isNull() && !strcmp(e["kind"] | "",kind) && isfinite(announcementNumber(e["target_at"])) ? (time_t)announcementNumber(e["target_at"]) : 0;
}
inline time_t codexNearestAnnouncement(JsonObjectConst ai,time_t now,bool showReset,const char **kind) {
    JsonObjectConst e=codexSelectedAnnouncement(ai,now,showReset);
    *kind=e.isNull() ? nullptr : e["kind"].as<const char *>();
    double target=announcementNumber(e["target_at"]);
    return isfinite(target) ? (time_t)target : 0;
}

// News page shares the selected v2 event, never creates a second inference.
inline bool codexNewsFooterText(JsonObjectConst ai, time_t now, bool showReset,
                               char *out, size_t cap) {
    if (!out || !cap) return false;
    out[0]=0;
    if (now < 1672502400LL) return false;
    JsonObjectConst e=codexSelectedAnnouncement(ai,now,showReset);
    if (e.isNull()) return false;
    const char *verb=!strcmp(e["kind"] | "","reset_grant") ? "发放" : "重置";
    double target=announcementNumber(e["target_at"]);
    if (isfinite(target)) {
        char stamp[24]; struct tm t={}; time_t beijing=(time_t)target+8*3600;
        if (!gmtime_r(&beijing,&t)) return false;
        strftime(stamp,sizeof(stamp),"%m-%d %H:%M",&t);
        snprintf(out,cap,"Codex 预计 %s %s%s",stamp,
                 !strcmp(e["time_relation"] | "before","at") ? "" : "前",verb);
    } else {
        const char *date=e["display_date"] | "";
        if (date[0]) snprintf(out,cap,"Codex 预计%s · %s，时间待定",verb,date+5);
        else snprintf(out,cap,"Codex 预计%s · 时间待定",verb);
    }
    return out[0]!=0;
}

inline void codexForecastDisplayKey(JsonObjectConst ai,time_t now,bool showReset,
                                    char *out,size_t cap) {
    char text[192];codexNewsFooterText(ai,now,showReset,text,sizeof(text));
    // Full visible UTF-8 text, not polling/revision/auxiliary source metadata.
    unsigned long long hash=14695981039346656037ULL;
    if (text[0]) for (const unsigned char *p=(const unsigned char*)text;*p;p++)
        hash=(hash ^ *p)*1099511628211ULL;
    else hash=0;
    snprintf(out,cap,"f%016llx",hash);
}

inline bool codexForecastMetaChanged(const char *meta,const char *key,bool legacyVisible=false) {
    if (!meta || !key) return false;
    const char *first=strchr(meta,'|');
    const char *second=first ? strchr(first+1,'|') : nullptr;
    const char *end=second ? strchr(second+1,'|') : nullptr;
    if (end && end-second>=19 && end[-18]=='.' && end[-17]=='f')
        return strncmp(end-17,key,17)!=0;
    // Upgrade old metadata only for an actually visible forecast. No extra
    // refresh of a page with neither old nor current forecast.
    return strcmp(key,"f0000000000000000")!=0 || legacyVisible;
}
