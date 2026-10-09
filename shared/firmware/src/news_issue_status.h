// Pure issue selection/status policy shared by the renderer and wake request.
// No networking, Preferences writes, or paid work. Missing metadata is generic.
#pragma once
#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

struct NewsExpectedIssue {
    bool trusted = false;
    bool due = false;
    char day[11] = "";
    char id[65] = "";
    const char *label = "资讯";
};

static inline int newsIssueMinute(const char *s) {
    if (!s || strlen(s) != 5 || s[2] != ':') return -1;
    const int positions[] = {0, 1, 3, 4};
    for (int i : positions) if (s[i] < '0' || s[i] > '9') return -1;
    int h = (s[0]-'0')*10+s[1]-'0', m = (s[3]-'0')*10+s[4]-'0';
    return h < 24 && m < 60 ? h*60+m : -1;
}

static inline bool newsIssueDate(const char *s) {
    if (!s || strlen(s) != 10 || s[4] != '-' || s[7] != '-') return false;
    for (int i = 0; i < 10; ++i)
        if (i != 4 && i != 7 && (s[i] < '0' || s[i] > '9')) return false;
    int y, m, d;
    if (sscanf(s, "%d-%d-%d", &y, &m, &d) != 3 || y < 2000 || y > 2099 || m < 1 || m > 12) return false;
    static const int days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    int maxDay = days[m-1] + (m == 2 && y%4 == 0 && (y%100 != 0 || y%400 == 0));
    return d >= 1 && d <= maxDay;
}

static inline bool newsIssueApplies(JsonArrayConst kinds, bool workday) {
    for (JsonVariantConst v : kinds) {
        const char *s = v | "";
        if (!strcmp(s, "all") || !strcmp(s, workday ? "workday" : "restday")) return true;
    }
    return false;
}

static inline NewsExpectedIssue newsExpectedIssue(const JsonDocument &doc, time_t now) {
    NewsExpectedIssue out;
    if (now < 1600000000LL) return out;
    long long publishedAt = doc["ts"].as<long long>();
    if (publishedAt > (long long)now + 300) return out;
    struct tm t;
    if (!localtime_r(&now, &t)) return out;
    snprintf(out.day, sizeof(out.day), "%04d-%02d-%02d", t.tm_year+1900, t.tm_mon+1, t.tm_mday);
    bool workday = false, calendarKnown = false;
    for (JsonVariantConst v : doc["screen"]["calendar"]["days"].as<JsonArrayConst>()) {
        JsonObjectConst row = v.as<JsonObjectConst>();
        if (!strcmp(row["d"] | "", out.day) && row["workday"].is<bool>()) {
            if (row["workday_degraded"] | false) return out;
            workday = row["workday"].as<bool>(); calendarKnown = true; break;
        }
    }
    JsonArrayConst schedules = doc["screen"]["device_policy"]["news_check"]["schedules"].as<JsonArrayConst>();
    if (!calendarKnown || schedules.isNull()) return out;
    out.trusted = true;
    int best = -1, atNow = t.tm_hour*60+t.tm_min;
    for (JsonVariantConst v : schedules) {
        JsonObjectConst row = v.as<JsonObjectConst>();
        if (!(row["enabled"] | false) || !newsIssueApplies(row["day_types"].as<JsonArrayConst>(), workday)) continue;
        const char *id = row["id"] | "";
        int at = newsIssueMinute(row["time"] | "");
        if (!id[0] || strlen(id) >= sizeof(out.id) || at < 0) { out.trusted = false; return out; }
        if (at > atNow || at < best || (at == best && strcmp(id, out.id) <= 0)) continue;
        best = at;
        snprintf(out.id, sizeof(out.id), "%s", id);
        out.label = row["display_label"] | "资讯";
    }
    out.due = best >= 0;
    return out;
}

static inline bool newsBodyMatches(JsonObjectConst news, const NewsExpectedIssue &due) {
    const char *mode = news["mode"] | "";
    return due.trusted && due.due && !strcmp(news["date"] | "", due.day)
        && !strcmp(news["schedule_id"] | (news["period"] | ""), due.id)
        && (!strcmp(mode, "digest") || !strcmp(mode, "daily_message"))
        && (news["text"] | "")[0] && strcmp(news["freshness"] | "", "error");
}

static inline bool newsLocalUpdateValid(JsonObjectConst news, const NewsExpectedIssue &due, time_t now) {
    char key[80]; snprintf(key, sizeof(key), "%s|%s", due.day, due.id);
    long long expires = news["device_update_expires_at"].as<long long>();
    return due.trusted && due.due && !newsBodyMatches(news, due)
        && !strcmp(news["device_update_issue_key"] | "", key)
        && expires >= (long long)now && expires <= (long long)now+900;
}

static inline void newsClearObsoleteLocalUpdate(JsonDocument &doc, time_t now) {
    JsonObject news = doc["screen"]["pages"]["news_gold"]["news"].as<JsonObject>();
    if (news.isNull()) return;
    if (!newsLocalUpdateValid(news, newsExpectedIssue(doc, now), now)) {
        news.remove("device_update_state"); news.remove("device_update_expires_at");
        news.remove("device_update_issue_key");
    }
}

// Return null to preserve the existing aggregate quality status. Names are
// accepted only after the renderer verifies UTF-8, glyphs, and actual ink width.
static inline const char *newsIssueStatusText(const JsonDocument &doc, time_t now,
                                            bool (*safeLabel)(const char *)) {
    static char result[96];
    JsonObjectConst news = doc["screen"]["pages"]["news_gold"]["news"].as<JsonObjectConst>();
    if (news.isNull()) return nullptr;
    NewsExpectedIssue due = newsExpectedIssue(doc, now);
    if (!due.trusted) {
        const char *coarse = news["update_state"] | "";
        if (!strcmp(coarse, "due")) return "资讯待更新";
        if (!strcmp(coarse, "not-updated")) return "资讯暂未更新";
        return news["issue_status"].isNull() ? nullptr : "资讯待更新";
    }
    const char *date = news["date"] | "";
    if (newsIssueDate(date) && strcmp(date, due.day) < 0 && !due.due) {
        time_t yesterday = now-86400; struct tm t; char day[11];
        localtime_r(&yesterday, &t);
        snprintf(day, sizeof(day), "%04d-%02d-%02d", t.tm_year+1900,t.tm_mon+1,t.tm_mday);
        return !strcmp(date, day) ? "昨日内容" : "资讯陈旧";
    }
    JsonObjectConst detail = news["issue_status"].as<JsonObjectConst>();
    if (!detail.isNull() && ((detail["schema"] | 0) != 1 || !(detail["coherent"] | false)
        || !(detail["clock_trusted"] | false)
        || !(detail["calendar_trusted"] | true)
        || strcmp(detail["current"]["date"] | "", date)
        || strcmp(detail["current"]["id"] | "", news["schedule_id"] | (news["period"] | ""))))
        return "资讯待更新";
    if (newsBodyMatches(news, due)) return nullptr;
    if (!due.due) return newsIssueDate(date) && !strcmp(date, due.day) ? nullptr : "资讯待更新";
    JsonObjectConst expected = detail["expected"].as<JsonObjectConst>();
    bool detailMatches = (detail["schema"] | 0) == 1 && (detail["coherent"] | false)
        && (detail["clock_trusted"] | false) && !strcmp(detail["as_of_date"] | "", due.day)
        && !strcmp(expected["date"] | "", due.day) && !strcmp(expected["id"] | "", due.id)
        && !strcmp(detail["current"]["date"] | "", date)
        && !strcmp(detail["current"]["id"] | "", news["schedule_id"] | (news["period"] | ""));
    // Publisher applies rest-day/custom labels to the same issue snapshot.
    // The base device policy cannot override that validated issue name.
    const char *label = detailMatches ? (expected["display_label"] | "资讯") : "资讯";
    if (!safeLabel || !safeLabel(label)) label = "资讯";
    const char *state = detailMatches ? (detail["state"] | "") : "";
    const char *suffix = "待更新";
    long long cutoff = expected["cutoff_at"].as<long long>();
    if (!strcmp(state, "failed") || !strcmp(state, "expired") ||
        (detailMatches && cutoff > 0 && now >= cutoff)) suffix = "暂未更新";
    else if (!strcmp(state, "generating") && doc["ts"].as<long long>() >= now-600) suffix = "生成中";
    else if (!strcmp(state, "unknown") || (!detail.isNull() && !detailMatches)) label = "资讯";
    if (newsLocalUpdateValid(news, due, now) &&
        !strcmp(news["device_update_state"] | "", "unreachable")) suffix = "暂未更新";
    if (detail.isNull()) {
        // Old publishers cannot name the expected issue; do not infer an enum.
        label = "资讯";
        if (!strcmp(news["update_state"] | "", "not-updated")) suffix = "暂未更新";
    }
    snprintf(result, sizeof(result), "%s%s", label, suffix);
    return result;
}
