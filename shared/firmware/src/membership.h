#pragma once
#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>

// v1 typed date; no inferred year or renewal date enters the legacy expiry.
static bool membershipDate(JsonObjectConst info, char *out, size_t cap) {
    const char *raw=info["date"] | "", *precision=info["precision"] | "";
    int y=2000,m=0,d=0; size_t len=strlen(raw);
    bool full=strcmp(precision,"date")==0;
    if (len!=(full?10u:5u) || (!full && strcmp(precision,"month_day")!=0)) return false;
    for (size_t i=0;i<len;i++) {
        bool dash=full?(i==4||i==7):(i==2);
        if (dash ? raw[i]!='-' : !isdigit((unsigned char)raw[i])) return false;
    }
    if (full) sscanf(raw,"%d-%d-%d",&y,&m,&d); else sscanf(raw,"%d-%d",&m,&d);
    if (y<1||y>9999||m<1||m>12||d<1) return false;
    const int days[]={31,28,31,30,31,30,31,31,30,31,30,31};
    int maxDay=days[m-1]+(m==2 && (y%400==0 || (y%4==0 && y%100!=0)));
    if (d>maxDay) return false;
    snprintf(out,cap,"%02d-%02d",m,d); return true;
}

static void membershipText(JsonObjectConst ai, char *out, size_t cap) {
    const char *raw=ai["plan"] | ""; char plan[40]="会员";
    if (raw[0]) {
        bool safe=true;
        for (const char *p=raw;*p;p++) if (!isalnum((unsigned char)*p) && !strchr(" _/.-",*p)) safe=false;
        if (safe) snprintf(plan,sizeof(plan),strlen(raw)>12?"%.12s...":"%s",raw);
    }
    JsonObjectConst info=ai["membership"].as<JsonObjectConst>();
    if (info.isNull()) {
        const char *v=ai["valid_until_date"] | "";
        snprintf(out,cap,"%s · 有效至 %.5s",plan,strlen(v)>=10?v+5:v); return;
    }
    char mmdd[6]={0}; bool schema=(info["schema"] | 0)==1;
    bool valid=schema && membershipDate(info,mmdd,sizeof(mmdd));
    const char *state=info["state"] | "", *type=info["date_type"] | "";
    if (schema && strcmp(state,"expired")==0) {
        snprintf(out,cap,valid?"%s · 已到期 %s":"%s · 已到期",plan,mmdd); return;
    }
    const char *label=strcmp(type,"renewal")==0?"续期":strcmp(type,"valid_until")==0?"有效至":nullptr;
    if (valid && label && (strcmp(state,"renewing")==0||strcmp(state,"valid_until")==0))
        snprintf(out,cap,"%s · %s %s",plan,label,mmdd);
    else snprintf(out,cap,"%s · 待确认",plan);
}

static bool membershipProblem(JsonObjectConst ai) {
    JsonObjectConst info=ai["membership"].as<JsonObjectConst>();
    if (info.isNull()) return !((ai["plan"] | "")[0] && (ai["valid_until_date"] | "")[0]);
    char mmdd[6];
    return (info["schema"] | 0)!=1 || (info["needs_confirmation"] | false) || !membershipDate(info,mmdd,sizeof(mmdd));
}
