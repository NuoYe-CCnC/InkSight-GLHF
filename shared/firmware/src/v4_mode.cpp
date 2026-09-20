// v4_mode.cpp — 第三阶段正式调度：法定工作日时段、自适应检查、自动切页、统一退避。
#include "v4_mode.h"
#include "config.h"
#include "display.h"
#include "epd_driver.h"
#include "network.h"
#include "offline_cache.h"
#include "panel_pages.h"
#include "phase3_policy.h"
#include "schedule.h"
#include "structured_display.h"

#include <Preferences.h>
#include <WiFi.h>
#include <driver/gpio.h>
#include <esp_sleep.h>
#include <climits>
#include <string.h>
#include <time.h>

#if ENABLE_BLE_TRIGGER
#include "ble_trigger.h"
#endif

#ifndef ENABLE_STRUCTURED
bool schedulerV4Cycle() { return false; }
void v4SleepAfterNetworkFailure(int) {}
void v4RecordValidData() {}
#else

static const uint32_t P3_RTC_MAGIC = 0x50335332u;

struct Phase3RtcState {
    uint32_t magic;
    long long nextCheckAt;
    long long lastSuccessAt;
    long long lastChangeAt;
    long long observeUntil;
    long long aiHoldUntil;
    long long newsFollowUntil;
    long long lastNtpAt;
    int lastMode;
    int lastYday;
    char activityKey[48];
};

RTC_DATA_ATTR static Phase3RtcState s_p3;

static long long _nowSec() { return (long long)time(nullptr); }
static bool _timeOk() { return schedulerTimeValid(); }

static int _policyInt(const JsonDocument *doc, const char *section,
                      const char *key, int fallback, int lo, int hi) {
    if (!doc) return fallback;
    JsonVariantConst value = (*doc)["screen"]["device_policy"][section][key];
    if (!value.is<int>() && !value.is<long>()) return fallback;
    long long out = value.as<long long>();
    return (out >= lo && out <= hi) ? (int)out : fallback;
}

static int _minuteValue(const char *text) {
    if (!text || strlen(text) != 5 || text[2] != ':') return -1;
    int h = (text[0] - '0') * 10 + (text[1] - '0');
    int m = (text[3] - '0') * 10 + (text[4] - '0');
    return (h >= 0 && h < 24 && m >= 0 && m < 60) ? h * 60 + m : -1;
}

static P3Mode _modeFromPolicy(const JsonDocument *doc, bool workday, int minuteOfDay) {
    if (!doc) return p3ModeForMinute(workday, minuteOfDay);
    JsonArrayConst rows = (*doc)["screen"]["device_policy"]["activity_windows"]
                              [workday ? "workday" : "restday"].as<JsonArrayConst>();
    for (JsonVariantConst value : rows) {
        JsonObjectConst row = value.as<JsonObjectConst>();
        int start = _minuteValue(row["start"] | "");
        int end = _minuteValue(row["end"] | "");
        const char *mode = row["mode"] | "";
        if (start < 0 || end < 0 || start == end) continue;
        bool inside = start < end ? (minuteOfDay >= start && minuteOfDay < end)
                                  : (minuteOfDay >= start || minuteOfDay < end);
        if (!inside) continue;
        if (strcmp(mode, "active") == 0) return P3Mode::Active;
        if (strcmp(mode, "light") == 0) return P3Mode::Light;
        if (strcmp(mode, "night") == 0) return P3Mode::Night;
    }
    return p3ModeForMinute(workday, minuteOfDay);
}

static int _adaptiveFromPolicy(const JsonDocument *doc, P3Mode mode,
                               bool baselineKnown, bool observationKnown,
                               long long age, long long observeRemaining) {
    int fallback = p3AdaptiveInterval(mode, baselineKnown, observationKnown,
                                      age, observeRemaining);
    if (!doc || !baselineKnown || !observationKnown) return fallback;
    if (observeRemaining > 0)
        return _policyInt(doc, "adaptive_check", "observation_poll_seconds", 60, 10, 3600);
    const char *name = p3ModeName(mode);
    JsonArrayConst rows = (*doc)["screen"]["device_policy"]["adaptive_check"][name]
                              .as<JsonArrayConst>();
    int selected = 0;
    if (age < 0) age = 0;
    for (JsonVariantConst value : rows) {
        JsonObjectConst row = value.as<JsonObjectConst>();
        long long after = row["after_seconds"].as<long long>();
        int interval = row["interval_seconds"] | 0;
        if (after < 0 || interval < 10 || interval > 86400) continue;
        if (age >= after) selected = interval;
    }
    return selected > 0 ? selected : fallback;
}

static int _pageIdleFromPolicy(const JsonDocument *doc, P3Mode mode) {
    if (mode == P3Mode::Night) return 0;
    const char *key = mode == P3Mode::Active ? "active_idle_seconds" : "light_idle_seconds";
    return _policyInt(doc, "page_switch", key, p3IdleSwitchSeconds(mode), 0, 86400);
}

static void _initState(long long nowSec) {
    if (s_p3.magic == P3_RTC_MAGIC) return;
    memset(&s_p3, 0, sizeof(s_p3));
    s_p3.magic = P3_RTC_MAGIC;
    s_p3.nextCheckAt = nowSec;
    s_p3.lastMode = -1;
    s_p3.lastYday = -1;
    Preferences prefs;
    prefs.begin("inksight", false);
    String key = prefs.getString("p3_ai_key", "");
    snprintf(s_p3.activityKey, sizeof(s_p3.activityKey), "%s", key.c_str());
    s_p3.lastChangeAt = prefs.getLong("p3_ai_chg", 0);
    s_p3.aiHoldUntil = prefs.getLong("p3_ai_hold", 0);
    if (s_p3.lastChangeAt > nowSec + 300) {
        s_p3.lastChangeAt = nowSec;
        s_p3.aiHoldUntil = nowSec + PHASE3_AI_HOLD_SECONDS;
        prefs.putLong("p3_ai_chg", s_p3.lastChangeAt);
        prefs.putLong("p3_ai_hold", s_p3.aiHoldUntil);
        Serial.println("[P3][TIME] persisted activity deadline rebased after clock rollback");
    }
    int schema = prefs.getInt("p3_schema", 0);
    if (schema != PHASE3_STATE_SCHEMA) {
        prefs.putInt("p3_schema", PHASE3_STATE_SCHEMA);
        // Preserve page/network state; retire the old BOOT/ACTIVE/SLEEP clock state.
        prefs.remove("v4_mode");
        prefs.remove("v4_boot_at");
        prefs.remove("v4_last_ts");
        prefs.remove("v4_cnt");
        prefs.remove("v4_probe_at");
        Serial.printf("[P3][MIGRATE] schema %d -> %d; page/network state preserved\n",
                      schema, PHASE3_STATE_SCHEMA);
    }
    prefs.end();
}

static int _retryDelayFromPolicy(const JsonDocument *doc, int failures) {
    if (doc) {
        JsonArrayConst rows = (*doc)["screen"]["device_policy"]["network"]
                              ["retry_backoff_seconds"].as<JsonArrayConst>();
        if (!rows.isNull() && rows.size() > 0) {
            int index = min(max(failures, 0), (int)rows.size() - 1);
            int value = rows[index] | 0;
            if (value > 0 && value <= 86400) return value;
        }
    }
    int index = min(max(failures, 0), RETRY_DELAY_COUNT - 1);
    return RETRY_DELAYS[index];
}

static int _recordNetworkFailure(const JsonDocument *doc = nullptr) {
    Preferences prefs;
    prefs.begin("inksight", false);
    int failures = prefs.getInt("net_fail_n", 0);
    int delaySec = _retryDelayFromPolicy(doc, failures);
    failures = min(failures + 1, 32);
    prefs.putInt("net_fail_n", failures);
    prefs.putLong("net_retry_at", _timeOk() ? _nowSec() + delaySec : 0);
    prefs.end();
    Serial.printf("[RECOVERY] failure=%d stage=%s retry=%ds\n", failures,
                  networkFailureStageName(networkLastFailureStage()), delaySec);
    return delaySec;
}

void v4RecordValidData() {
    Preferences prefs;
    prefs.begin("inksight", false);
    int failures = prefs.getInt("net_fail_n", 0);
    if (failures > 0 || prefs.getLong("net_retry_at", 0) != 0) {
        prefs.putInt("net_fail_n", 0);
        prefs.putLong("net_retry_at", 0);
    }
    prefs.end();
    if (failures > 0) Serial.printf("[RECOVERY] valid data; reset failures=%d\n", failures);
}

static void _sleepSec(int sec, const char *reason) {
    sec = max(1, sec);
#if ENABLE_BLE_TRIGGER
    bleStopForSleep();
#endif
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    epdSleep();
    long long wakeAt = _timeOk() ? _nowSec() + sec : 0;
    Serial.printf("[P3][SLEEP] seconds=%d wake_at=%lld reason=%s page=%d\n",
                  sec, wakeAt, reason ? reason : "timer", panelCurrentPage());
    Serial.flush();
#if PIN_EPD_PWR >= 0
    gpio_hold_en((gpio_num_t)PIN_EPD_PWR);
#endif
    esp_sleep_enable_timer_wakeup((uint64_t)sec * 1000000ULL);
    esp_deep_sleep_start();
    while (true) delay(1000);
}

void v4SleepAfterNetworkFailure(int) {
    int delaySec = _recordNetworkFailure();
    _sleepSec(delaySec, "network-retry");
}

static int _networkRetryWaitSeconds(const JsonDocument *doc = nullptr) {
    Preferences prefs;
    prefs.begin("inksight", false);
    int failures = prefs.getInt("net_fail_n", 0);
    long long retryAt = prefs.getLong("net_retry_at", 0);
    long long nowSec = _nowSec();
    if (failures <= 0 || retryAt <= 0 || retryAt <= nowSec) {
        prefs.end();
        return 0;
    }
    long long wait = retryAt - nowSec;
    if (wait > 305) {
        wait = _retryDelayFromPolicy(doc, failures);
        prefs.putLong("net_retry_at", nowSec + wait);
        Serial.printf("[RECOVERY] stale retry timestamp clamped to %llds\n", wait);
    }
    prefs.end();
    return (int)min(wait, (long long)INT_MAX);
}

static int _networkFailureCount() {
    Preferences prefs;
    prefs.begin("inksight", true);
    int failures = prefs.getInt("net_fail_n", 0);
    prefs.end();
    return failures;
}

static bool _workdayFromDoc(const JsonDocument *doc, time_t at,
                            bool *degradedOut) {
    struct tm *t = localtime(&at);
    if (!t) {
        if (degradedOut) *degradedOut = true;
        return true;
    }
    char key[16];
    snprintf(key, sizeof(key), "%04d-%02d-%02d",
             t->tm_year + 1900, t->tm_mon + 1, t->tm_mday);
    if (doc) {
        JsonArrayConst days = (*doc)["screen"]["calendar"]["days"].as<JsonArrayConst>();
        for (JsonVariantConst v : days) {
            JsonObjectConst row = v.as<JsonObjectConst>();
            if (strcmp(row["d"] | "", key) != 0) continue;
            if (!row["workday"].is<bool>()) break;
            if (degradedOut) *degradedOut = row["workday_degraded"] | true;
            return row["workday"].as<bool>();
        }
    }
    if (degradedOut) *degradedOut = true;
    return t->tm_wday >= 1 && t->tm_wday <= 5;
}

static bool _newsAvailable(const JsonDocument &doc) {
    JsonObjectConst news = doc["screen"]["pages"]["news_gold"]["news"].as<JsonObjectConst>();
    if (news.isNull()) return false;
    if ((news["text"] | "")[0]) return true;
    JsonArrayConst items = news["items"].as<JsonArrayConst>();
    for (JsonVariantConst v : items) {
        JsonObjectConst item = v.as<JsonObjectConst>();
        if (!item.isNull() && (item["text"] | "")[0]) return true;
    }
    const char *keys[] = {"general", "tech_ai", "finance"};
    for (const char *key : keys) {
        JsonObjectConst item = news[key].as<JsonObjectConst>();
        if (!item.isNull() && (item["title"] | "")[0]) return true;
    }
    return false;
}

struct ActivityResult {
    bool baselineKnown;
    bool observationKnown;
    bool changed;
    bool established;
};

static void _persistActivity(const char *key, long long changedAt,
                             long long holdUntil) {
    Preferences prefs;
    prefs.begin("inksight", false);
    prefs.putString("p3_ai_key", key);
    prefs.putLong("p3_ai_chg", changedAt);
    prefs.putLong("p3_ai_hold", holdUntil);
    prefs.end();
}

static ActivityResult _applyActivity(const JsonDocument &doc, P3Mode mode,
                                     long long nowSec) {
    ActivityResult out = {s_p3.activityKey[0] != 0, false, false, false};
    JsonObjectConst ver = doc["screen"]["versions"].as<JsonObjectConst>();
    const char *key = ver["activity_key"] | "";
    bool valid = ver["activity_valid"] | false;
    long long observed = ver["activity_observed_at"].as<long long>();
    if (valid && key[0] && observed > 0 && observed <= nowSec + 300) {
        if (observed > nowSec) observed = nowSec;
        if (observed > s_p3.lastSuccessAt) s_p3.lastSuccessAt = observed;
        if (!s_p3.activityKey[0]) {
            snprintf(s_p3.activityKey, sizeof(s_p3.activityKey), "%s", key);
            s_p3.lastChangeAt = nowSec;
            int hold = _policyInt(&doc, "page_switch", "minimum_ai_hold_seconds",
                                  PHASE3_AI_HOLD_SECONDS, 0, 86400);
            s_p3.aiHoldUntil = nowSec + hold;
            _persistActivity(key, s_p3.lastChangeAt, s_p3.aiHoldUntil);
            out.established = true;
            Serial.printf("[P3][ACTIVITY] baseline established observed=%lld\n", observed);
        } else if (strcmp(s_p3.activityKey, key) != 0) {
            snprintf(s_p3.activityKey, sizeof(s_p3.activityKey), "%s", key);
            s_p3.lastChangeAt = nowSec;
            int hold = _policyInt(&doc, "page_switch", "minimum_ai_hold_seconds",
                                  PHASE3_AI_HOLD_SECONDS, 0, 86400);
            int observation = _policyInt(&doc, "adaptive_check", "observation_seconds",
                                         300, 0, 3600);
            s_p3.aiHoldUntil = nowSec + hold;
            if (mode != P3Mode::Active) s_p3.observeUntil = nowSec + observation;
            _persistActivity(key, s_p3.lastChangeAt, s_p3.aiHoldUntil);
            out.changed = true;
            Serial.printf("[P3][ACTIVITY] semantic change observed=%lld hold_until=%lld\n",
                          observed, s_p3.aiHoldUntil);
        } else {
            Serial.printf("[P3][ACTIVITY] same semantic value observed=%lld\n", observed);
        }
    } else {
        Serial.println("[P3][ACTIVITY] incomparable/unknown; stability not advanced");
    }
    out.baselineKnown = s_p3.activityKey[0] != 0;
    int maxAge = _policyInt(&doc, "adaptive_check", "unknown_max_age_seconds",
                            PHASE3_UNKNOWN_MAX_AGE_SECONDS, 60, 86400);
    out.observationKnown = s_p3.lastSuccessAt > 0 && nowSec >= s_p3.lastSuccessAt &&
        nowSec - s_p3.lastSuccessAt <= maxAge;
    return out;
}

static ActivityResult _currentActivity(const JsonDocument *doc, long long nowSec) {
    ActivityResult out = {s_p3.activityKey[0] != 0, false, false, false};
    int maxAge = _policyInt(doc, "adaptive_check", "unknown_max_age_seconds",
                            PHASE3_UNKNOWN_MAX_AGE_SECONDS, 60, 86400);
    out.observationKnown = s_p3.lastSuccessAt > 0 && nowSec >= s_p3.lastSuccessAt &&
        nowSec - s_p3.lastSuccessAt <= maxAge;
    return out;
}

static bool _setPageForPolicy(const JsonDocument &doc, P3Mode mode,
                              const ActivityResult &activity, long long nowSec,
                              const char *reasonPrefix) {
    int current = panelCurrentPage();
    bool forceAi = activity.changed;
    JsonObjectConst switchPolicy = doc["screen"]["device_policy"]["page_switch"].as<JsonObjectConst>();
    bool enabled = switchPolicy.isNull() ? true : (switchPolicy["enabled"] | true);
    int desired = current;
    const char *defaultPage = switchPolicy["default_page"] | "ai";
    if (activity.established || !activity.baselineKnown) {
        desired = strcmp(defaultPage, "news_gold") == 0 && _newsAvailable(doc) ? 1 : 0;
    } else if (!enabled) {
        if (current == 1 && !_newsAvailable(doc)) desired = 0;
    } else if (forceAi || !activity.observationKnown ||
               (current == 1 && !_newsAvailable(doc))) {
        desired = 0;
    } else if (mode == P3Mode::Night) {
        const char *night = switchPolicy["night_behavior"] | "keep_current";
        if (strcmp(night, "force_ai") == 0) desired = 0;
    } else if (current == 0 && _newsAvailable(doc)) {
        int idle = _pageIdleFromPolicy(&doc, mode);
        int hold = _policyInt(&doc, "page_switch", "minimum_ai_hold_seconds",
                              PHASE3_AI_HOLD_SECONDS, 0, 86400);
        long long holdUntil = max(s_p3.aiHoldUntil, s_p3.lastChangeAt + hold);
        if (idle > 0 && nowSec >= holdUntil && s_p3.lastChangeAt > 0 &&
            nowSec - s_p3.lastChangeAt >= idle) desired = 1;
    }
    if (desired == current) return false;
    const char *why = desired == 0
        ? (activity.changed ? "ai-change" :
           (activity.established ? "configured-default" : "news-unavailable"))
        : (mode == P3Mode::Active ? "active-stable-10m" : "light-stable-5m");
    panelSetPage(desired);
    Serial.printf("[P3][PAGE] %d->%d reason=%s%s%s hold_until=%lld\n",
                  current, desired, reasonPrefix ? reasonPrefix : "",
                  reasonPrefix ? ":" : "", why, s_p3.aiHoldUntil);
    return true;
}

static bool _renderDocument(JsonDocument &doc, bool pending, bool force,
                            const char *reason) {
    bool changed = true, displayed = false;
    bool ok = structuredRenderDocument(doc, pending, &changed, &displayed, force);
    if (!ok) {
        Serial.printf("[P3][REFRESH] render failed reason=%s\n", reason);
        return false;
    }
    if (changed && !displayed) {
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
        delay(50);
        smartDisplay(imgBuf);
        cacheSave(imgBuf, IMG_BUF_LEN);
    }
    Serial.printf("[P3][REFRESH] reason=%s changed=%d displayed=%d page=%d\n",
                  reason, changed ? 1 : 0, displayed ? 1 : 0, panelCurrentPage());
    return true;
}

static void _maybeSyncNtp(long long nowSec, const JsonDocument *doc) {
    if (doc && !((*doc)["screen"]["device_policy"]["time_sync"]["enabled"] | true)) return;
    int ntpInterval = _policyInt(doc, "time_sync", "interval_seconds",
                                 PHASE3_NTP_INTERVAL_SECONDS, 300, 7 * 86400);
    if (s_p3.lastNtpAt > 0 && nowSec >= s_p3.lastNtpAt &&
        nowSec - s_p3.lastNtpAt < ntpInterval) return;
    long long before = nowSec;
    const char *s1 = doc ? ((*doc)["screen"]["device_policy"]["time_sync"]["servers"][0] | "") : "";
    const char *s2 = doc ? ((*doc)["screen"]["device_policy"]["time_sync"]["servers"][1] | "") : "";
    const char *s3 = doc ? ((*doc)["screen"]["device_policy"]["time_sync"]["servers"][2] | "") : "";
    syncNTP(s1, s2, s3);
    long long after = _nowSec();
    if (_timeOk()) {
        s_p3.lastNtpAt = after;
        Serial.printf("[P3][NTP] hourly attempt delta=%llds\n", after - before);
        if (after + 60 < before) {
            s_p3.nextCheckAt = after;
            if (s_p3.lastChangeAt > after) s_p3.lastChangeAt = after;
            if (s_p3.aiHoldUntil > after + PHASE3_AI_HOLD_SECONDS)
                s_p3.aiHoldUntil = after + PHASE3_AI_HOLD_SECONDS;
            Serial.println("[P3][TIME] backward jump; short-term deadlines rebased");
        }
    }
}

static int _intervalFor(const JsonDocument *doc, P3Mode mode, long long nowSec,
                        const ActivityResult &activity) {
    long long age = s_p3.lastChangeAt > 0 ? nowSec - s_p3.lastChangeAt : -1;
    long long observe = max(0LL, s_p3.observeUntil - nowSec);
    return _adaptiveFromPolicy(doc, mode, activity.baselineKnown,
                               activity.observationKnown, age, observe);
}

static bool _enterModeIfChanged(const JsonDocument *doc, P3Mode mode, bool workday, bool degraded,
                                long long nowSec, int *entryOverride) {
    if (s_p3.lastMode == (int)mode) return false;
    long long age = s_p3.lastChangeAt > 0 ? nowSec - s_p3.lastChangeAt : LLONG_MAX;
    if (entryOverride) *entryOverride = 0;
    int observation = _policyInt(doc, "adaptive_check", "observation_seconds", 300, 0, 3600);
    if (mode == P3Mode::Light) s_p3.observeUntil = nowSec + observation;
    else if (mode == P3Mode::Night && age < observation) s_p3.observeUntil = nowSec + observation;
    else if (mode == P3Mode::Night && entryOverride) *entryOverride = observation;
    else s_p3.observeUntil = 0;
    s_p3.lastMode = (int)mode;
    s_p3.nextCheckAt = nowSec;
    Serial.printf("[P3][MODE] enter=%s workday=%d degraded=%d\n",
                  p3ModeName(mode), workday ? 1 : 0, degraded ? 1 : 0);
    return true;
}

static int _pageDueSeconds(P3Mode mode, const JsonDocument *doc,
                           const ActivityResult &activity, long long nowSec) {
    if (!doc || panelCurrentPage() != 0 || mode == P3Mode::Night ||
        !activity.baselineKnown || !activity.observationKnown || !_newsAvailable(*doc))
        return 0;
    int idle = _pageIdleFromPolicy(doc, mode);
    long long dueAt = max(s_p3.lastChangeAt + idle, s_p3.aiHoldUntil);
    return dueAt > nowSec ? (int)min(dueAt - nowSec, (long long)INT_MAX) : 1;
}

static const char *_wakeReason(int next, bool failed, int normalDue, int retryDue,
                               int modeDue, int pageDue) {
    if (failed && retryDue > 0 && next == retryDue) return "network-retry";
    if (!failed && normalDue > 0 && next == normalDue) return "normal-check";
    if (pageDue > 0 && next == pageDue) return "page-deadline";
    if (modeDue > 0 && next == modeDue) return "mode-boundary";
    return "local-event";
}

bool schedulerV4Cycle() {
    if (!_timeOk()) return false;
    long long nowSec = _nowSec();
    _initState(nowSec);
    JsonDocument cached;
    bool cacheOk = structuredLoadCachedDocument(cached);
    bool degradedToday = true, degradedTomorrow = true;
    bool workday = _workdayFromDoc(cacheOk ? &cached : nullptr, (time_t)nowSec,
                                   &degradedToday);
    bool tomorrowWorkday = _workdayFromDoc(cacheOk ? &cached : nullptr,
                                           (time_t)(nowSec + 86400),
                                           &degradedTomorrow);
    time_t nowTime = (time_t)nowSec;
    struct tm *tmNow = localtime(&nowTime);
    if (!tmNow) return false;
    int minuteOfDay = tmNow->tm_hour * 60 + tmNow->tm_min;
    P3Mode mode = _modeFromPolicy(cacheOk ? &cached : nullptr, workday, minuteOfDay);
    int entryOverride = 0;
    _enterModeIfChanged(cacheOk ? &cached : nullptr, mode, workday, degradedToday, nowSec, &entryOverride);
    bool dateChanged = s_p3.lastYday >= 0 && s_p3.lastYday != tmNow->tm_yday;
    s_p3.lastYday = tmNow->tm_yday;
    ActivityResult current = _currentActivity(cacheOk ? &cached : nullptr, nowSec);
    int failureCount = _networkFailureCount();
    int retryWait = _networkRetryWaitSeconds(cacheOk ? &cached : nullptr);
    bool failed = retryWait > 0;
    bool normalDueNow = s_p3.nextCheckAt <= nowSec;
    bool retryDueNow = failureCount > 0 && retryWait == 0;
    bool shouldFetch = !failed && (normalDueNow || retryDueNow);
    Serial.printf("[P3][WAKE] cause=%d mode=%s workday=%d calendar=%s page=%d "
                  "normal_due=%d retry_wait=%d\n",
                  (int)esp_sleep_get_wakeup_cause(), p3ModeName(mode), workday ? 1 : 0,
                  degradedToday ? "degraded-weekday" : "official", panelCurrentPage(),
                  normalDueNow ? 1 : 0, retryWait);

    if (shouldFetch) {
        long long checkStarted = nowSec;
        if (!connectWiFi()) {
            int retry = _recordNetworkFailure(cacheOk ? &cached : nullptr);
            nowSec = _nowSec();
            time_t tRaw = (time_t)nowSec;
            struct tm *t2 = localtime(&tRaw);
            int modeDue = t2 ? p3SecondsToModeBoundary(workday, tomorrowWorkday,
                              t2->tm_hour * 60 + t2->tm_min, t2->tm_sec) : retry;
            int pageDue = _pageDueSeconds(mode, cacheOk ? &cached : nullptr, current, nowSec);
            int next = p3NextWakeSeconds(true, 0, retry, modeDue, pageDue);
            _sleepSec(next, _wakeReason(next, true, 0, retry, modeDue, pageDue));
            return true;
        }
        _maybeSyncNtp(nowSec, cacheOk ? &cached : nullptr);
        JsonDocument doc;
        String modeId;
        bool pending = false;
        if (!fetchStructured(doc, &modeId, &pending)) {
            int retry = _recordNetworkFailure(cacheOk ? &cached : nullptr);
            nowSec = _nowSec();
            time_t tRaw = (time_t)nowSec;
            struct tm *t2 = localtime(&tRaw);
            int modeDue = t2 ? p3SecondsToModeBoundary(workday, tomorrowWorkday,
                              t2->tm_hour * 60 + t2->tm_min, t2->tm_sec) : retry;
            int pageDue = _pageDueSeconds(mode, cacheOk ? &cached : nullptr, current, nowSec);
            int next = p3NextWakeSeconds(true, 0, retry, modeDue, pageDue);
            _sleepSec(next, _wakeReason(next, true, 0, retry, modeDue, pageDue));
            return true;
        }
        nowSec = _nowSec();
        bool refetch = requestGoldRefreshForWake(doc);
        bool newsRequested = requestNewsCheckForWake(doc);
        long long newsExpiry = doc["screen"]["pages"]["news_gold"]["news"]
                                   ["device_update_expires_at"].as<long long>();
        if (newsRequested && newsExpiry > nowSec) s_p3.newsFollowUntil = newsExpiry;
        if (!newsCurrentIssueMissing(doc)) s_p3.newsFollowUntil = 0;
        if (refetch) {
            // One bounded refetch lets the direct backend path return the new
            // feed in this wake. Queue mode may still publish on a later wake.
            JsonDocument refreshed;
            String refreshedMode;
            bool refreshedPending = false;
            if (fetchStructured(refreshed, &refreshedMode, &refreshedPending)) {
                doc = refreshed;
                modeId = refreshedMode;
                pending = refreshedPending;
            }
        }
        structuredSaveCachedDocument(doc);
        // The pre-network decision may have used weekday fallback.  Adopt the
        // just-fetched official calendar before activity/page/interval policy.
        bool fetchedDegradedToday = true, fetchedDegradedTomorrow = true;
        bool fetchedWorkday = _workdayFromDoc(&doc, (time_t)nowSec,
                                               &fetchedDegradedToday);
        bool fetchedTomorrowWorkday = _workdayFromDoc(
            &doc, (time_t)(nowSec + 86400), &fetchedDegradedTomorrow);
        time_t fetchedRaw = (time_t)nowSec;
        struct tm *fetchedTm = localtime(&fetchedRaw);
        if (fetchedTm) {
            P3Mode fetchedMode = _modeFromPolicy(
                &doc, fetchedWorkday, fetchedTm->tm_hour * 60 + fetchedTm->tm_min);
            if (fetchedWorkday != workday ||
                fetchedDegradedToday != degradedToday) {
                Serial.printf("[P3][CALENDAR] refreshed workday=%d degraded=%d\n",
                              fetchedWorkday ? 1 : 0,
                              fetchedDegradedToday ? 1 : 0);
            }
            workday = fetchedWorkday;
            tomorrowWorkday = fetchedTomorrowWorkday;
            degradedToday = fetchedDegradedToday;
            degradedTomorrow = fetchedDegradedTomorrow;
            mode = fetchedMode;
            _enterModeIfChanged(&doc, mode, workday, degradedToday, nowSec,
                                &entryOverride);
        }
        ActivityResult activity = _applyActivity(doc, mode, nowSec);
        bool pageSwitched = _setPageForPolicy(doc, mode, activity, nowSec, "network");
        _renderDocument(doc, pending, pageSwitched, pageSwitched ? "page-switch" : "data-check");
        v4RecordValidData();
        int interval = entryOverride > 0 ? entryOverride : _intervalFor(&doc, mode, nowSec, activity);
        s_p3.nextCheckAt = p3AlignedNext(checkStarted, _nowSec(), interval);
        current = activity;
        cached = doc;
        cacheOk = true;
        Serial.printf("[P3][CHECK] interval=%ds started=%lld next=%lld activity_age=%lld\n",
                      interval, checkStarted, s_p3.nextCheckAt,
                      s_p3.lastChangeAt > 0 ? nowSec - s_p3.lastChangeAt : -1);
    } else if (cacheOk) {
        bool pageSwitched = _setPageForPolicy(cached, mode, current, nowSec, "local");
        if (pageSwitched || dateChanged)
            _renderDocument(cached, false, pageSwitched,
                            pageSwitched ? "page-deadline" : "date-change");
    }

    nowSec = _nowSec();
    time_t tRaw = (time_t)nowSec;
    struct tm *t3 = localtime(&tRaw);
    int modeDue = t3 ? p3SecondsToModeBoundary(workday, tomorrowWorkday,
                      t3->tm_hour * 60 + t3->tm_min, t3->tm_sec) : 300;
    current = _currentActivity(cacheOk ? &cached : nullptr, nowSec);
    int intervalNow = _intervalFor(cacheOk ? &cached : nullptr, mode, nowSec, current);
    if (!failed && s_p3.nextCheckAt <= nowSec) s_p3.nextCheckAt = nowSec + intervalNow;
    int normalDue = (int)max(1LL, s_p3.nextCheckAt - nowSec);
    retryWait = _networkRetryWaitSeconds(cacheOk ? &cached : nullptr);
    failed = retryWait > 0;
    int pageDue = _pageDueSeconds(mode, cacheOk ? &cached : nullptr, current, nowSec);
    int next = p3NextWakeSeconds(failed, normalDue, retryWait, modeDue, pageDue);
    if (!failed && cacheOk && newsCurrentIssueMissing(cached)
        && s_p3.newsFollowUntil > nowSec) {
        int followPoll = _policyInt(&cached, "news_check", "followup_poll_seconds",
                                    60, 30, 300);
        int remaining = (int)min((long long)INT_MAX, s_p3.newsFollowUntil - nowSec);
        next = min(next, max(1, min(followPoll, remaining)));
    }
    const char *reason = _wakeReason(next, failed, normalDue, retryWait, modeDue, pageDue);
    Serial.printf("[P3][PLAN] mode=%s interval=%ds normal_in=%d retry_in=%d "
                  "mode_in=%d page_in=%d next=%d reason=%s\n",
                  p3ModeName(mode), intervalNow, normalDue, retryWait,
                  modeDue, pageDue, next, reason);
    _sleepSec(next, reason);
    return true;
}

#endif  // ENABLE_STRUCTURED
