// schedule.cpp — 昼夜调度：白天内容（默认 30s 轮询，有变才刷屏）/ 夜间时钟（60s + 每小时 NTP）
#include "schedule.h"
#include "config.h"
#include "display.h"
#include "epd_driver.h"
#include "network.h"
#include "storage.h"

#include <Preferences.h>
#include <WiFi.h>
#include <esp_sleep.h>
#include <time.h>
#if ENABLE_BLE_TRIGGER
#include "ble_trigger.h"
#endif

#ifndef ENABLE_STRUCTURED
bool schedulerTimeValid() { return false; }
bool schedulerIsDaytime() { return true; }
int schedulerDaySleepSeconds() { return 60; }
int schedulerDaySleepMinutes() { return 1; }
bool schedulerNightClockBoot() { return false; }
bool schedulerDaylightSkipFetch() { return false; }
#else

static bool _timeValid() {
    time_t now = time(nullptr);
    struct tm *t = localtime(&now);
    return t && t->tm_year >= 123; // 2023+
}

static bool _isDaytime(int hour) { return hour >= 7 && hour < 19; }

static void _refreshClockVars() {
    time_t now = time(nullptr);
    struct tm *t = localtime(&now);
    if (!t) return;
    extern int curHour, curMin, curSec;
    curHour = t->tm_hour;
    curMin = t->tm_min;
    curSec = t->tm_sec;
}

// 最小联网校时：直连主 WiFi（不触发 token/heartbeat 等完整流程），校完即断
static bool _ntpOnce() {
    if (cfgSSID.length() == 0) return false;
    WiFi.mode(WIFI_STA);
    WiFi.begin(cfgSSID.c_str(), cfgPass.c_str());
    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000UL) {
        delay(300);
    }
    bool ok = (WiFi.status() == WL_CONNECTED);
    if (ok) {
        syncNTP();
        _refreshClockVars();
        Serial.println("[CLOCK] NTP synced");
    }
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    return ok;
}

bool schedulerTimeValid() { return _timeValid(); }

bool schedulerIsDaytime() {
    if (!_timeValid()) return true; // 未知时间：按白天走，主流程会校时
    time_t now = time(nullptr);
    struct tm *t = localtime(&now);
    return t && _isDaytime(t->tm_hour);
}

// 日间拉取间隔：当前运行模式 = 30 秒一次完整拉取（config.h DAY_POLL_SECONDS）
int schedulerDaySleepSeconds() {
    return DAY_POLL_SECONDS;
}

int schedulerDaySleepMinutes() {
    int s = DAY_POLL_SECONDS;
    return s < 60 ? 1 : s / 60;  // 兼容旧接口（秒粒度请用 schedulerDaySleepSeconds）
}

// 白天轻唤醒（BLE 触发模块，当前编译停用 → 恒 false，主流程按秒拉取）。
// BLE 重新启用时：每 60s 醒一次扫 2s；未到 5min 拉取点且无触发 → 内部深睡 60s。
bool schedulerDaylightSkipFetch() {
    if (!_timeValid()) return false;
    time_t now = time(nullptr);
    struct tm *t = localtime(&now);
    if (!t || !_isDaytime(t->tm_hour)) return false;
#if ENABLE_BLE_TRIGGER
    int minuteOfDay = t->tm_hour * 60 + t->tm_min;
    Preferences prefs;
    prefs.begin("inksight", false);
    int lastPoll = prefs.getInt("day_last_poll", -1);
    bool dueByTime = (lastPoll < 0) || (minuteOfDay - lastPoll >= 5) || (minuteOfDay < lastPoll);
    prefs.end();

    if (!dueByTime) {
        // 未到拉取时刻：短扫 BLE，无触发则轻睡 60s
        bool trig = bleScanForUpdate(2000);
        if (!trig) {
            Serial.println("[DAY] light wake, no BLE trigger, sleep 60s");
            epdSleep();
            esp_sleep_enable_timer_wakeup(60ULL * 1000000ULL);
            esp_deep_sleep_start();
            return true; // 不返回
        }
        Serial.println("[DAY] BLE trigger -> fetch now");
    } else {
        // 到点：更新拉取时刻标记（约 12h/5min=144 次写入/天，NVS 磨损可接受）
        prefs.begin("inksight", false);
        prefs.putInt("day_last_poll", minuteOfDay);
        prefs.end();
    }
#endif
    return false;
}

bool schedulerNightClockBoot() {
    if (!_timeValid()) return false;   // 断电后首次无时间：走主流程（校时后下次生效）

    time_t now = time(nullptr);
    struct tm *t = localtime(&now);
    if (!t || _isDaytime(t->tm_hour)) return false; // 白天/边界 → 主流程

    int minuteOfDay = t->tm_hour * 60 + t->tm_min;

    Preferences prefs;
    prefs.begin("inksight", false);
    int lastMin = prefs.getInt("clk_last_min", -1);
    int lastFull = prefs.getInt("clk_last_full", -1);
    int lastNtpHour = prefs.getInt("clk_last_ntp_hour", -1);

    // 每小时 NTP 校准（按小时变化触发，约 24 次/天写入）
    if (t->tm_hour != lastNtpHour) {
        prefs.putInt("clk_last_ntp_hour", t->tm_hour);
        _ntpOnce();
        now = time(nullptr);
        t = localtime(&now);
        if (t) minuteOfDay = t->tm_hour * 60 + t->tm_min;
    }
    _refreshClockVars();
    extern int curHour, curMin;
    Serial.printf("[CLOCK] night %02d:%02d\n", curHour, curMin);

    // 分钟变化才绘制；每 30 分钟一次全刷清残影
    if (minuteOfDay != lastMin) {
        showClockMode();
        bool full = (lastFull < 0 || minuteOfDay - lastFull >= 30 || minuteOfDay < lastFull);
        if (full) {
            epdDisplay(imgBuf);
            prefs.putInt("clk_last_full", minuteOfDay);
        } else {
            epdDisplayFast(imgBuf);
        }
        prefs.putInt("clk_last_min", minuteOfDay);
    }
    prefs.end();

    epdSleep();
    esp_sleep_enable_timer_wakeup(60ULL * 1000000ULL); // 60s 周期
    esp_deep_sleep_start();
    return true; // 不返回（深睡后重启）
}

#endif // ENABLE_STRUCTURED
