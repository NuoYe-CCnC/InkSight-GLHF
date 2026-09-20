// structured_display.cpp — 结构化渲染与空窗时钟模式
// 2026-09-07 修复版：payload 去重按“页面”持久化并带 layout/font 版本；
// FB_DUMP 验收构建帧标记含 pid/时间/len/CRC。
#include "structured_display.h"
#include "config.h"

long long g_lastPayloadTs = 0;
#include "display.h"
#include "epd_driver.h"
#include "layout.h"
#include "network.h"
#include <time.h>

#include <LittleFS.h>
#include <esp_sleep.h>

#ifndef ENABLE_STRUCTURED
bool structuredFetchAndDisplay(String *renderedModeIdOut, bool *pendingRefreshOut,
                               bool *changedOut, bool *displayedOut) {
    (void)renderedModeIdOut;
    (void)pendingRefreshOut;
    if (changedOut) *changedOut = true;
    if (displayedOut) *displayedOut = false;
    return false;
}
bool structuredRenderDocument(JsonDocument &, bool, bool *changedOut,
                              bool *displayedOut, bool) {
    if (changedOut) *changedOut = true;
    if (displayedOut) *displayedOut = false;
    return false;
}
bool structuredLoadCachedDocument(JsonDocument &) { return false; }
void structuredSaveCachedDocument(const JsonDocument &) {}
void enterClockMode() {}
#else

// extern 桥（v2 页面选择与渲染）
extern int panelCurrentPage();
extern bool renderAiPanel(const JsonDocument &);
extern bool renderNewsGoldPanel(const JsonDocument &);
extern const char *panelStatusWord(const JsonDocument &doc, const char *page);

// ── payload 去重持久化（LittleFS）────────────────────────────
// 存储行格式: "<layout_version>|<font_version>|<payload_id>"
// 旧文件（只有 payload_id、无 '|'）视为不匹配 → 触发一次重绘（向后兼容）。
// v2 双面板按当前页分文件 /payload_p0、/payload_p1 → 切页不被同 payload_id 阻止。
static const char *pidFileFor(int page, bool v2) {
    if (v2) return page == 1 ? "/payload_p1" : "/payload_p0";
    return "/payload_id";
}

static const char *STRUCT_CACHE_FILE = "/structured.json";
static const char *STRUCT_CACHE_TMP = "/structured.tmp";
static const char *STRUCT_CACHE_BAK = "/structured.bak";
static const char *STRUCT_CACHE_PID = "/structured_pid";

static bool loadLastMeta(const char *file, char *out, size_t cap) {
    File f = LittleFS.open(file, "r");
    if (!f) return false;
    size_t n = f.readBytes(out, cap - 1);
    out[n] = 0;
    f.close();
    return n > 0;
}

bool structuredLoadCachedDocument(JsonDocument &doc) {
    File f = LittleFS.open(STRUCT_CACHE_FILE, "r");
    if (!f) return false;
    DeserializationError err = deserializeJson(doc, f);
    f.close();
    bool ok = !err && doc["payload_type"] == "screen" &&
              doc["screen"]["pages"].is<JsonObject>();
    Serial.printf("[CACHE] structured load=%s\n", ok ? "ok" : "invalid");
    return ok;
}

void structuredSaveCachedDocument(const JsonDocument &doc) {
    const char *pid = doc["screen"]["payload_id"] | "";
    if (!pid[0]) return;
    long long ts = doc["ts"].as<long long>();
    char marker[96] = {0};
    snprintf(marker, sizeof(marker), "%s|%lld", pid, ts > 0 ? ts / 3600 : 0);
    char old[96] = {0};
    if (loadLastMeta(STRUCT_CACHE_PID, old, sizeof(old)) && strcmp(old, marker) == 0)
        return;  // 可见内容相同时至多每小时更新一次离线时间基准
    File f = LittleFS.open(STRUCT_CACHE_TMP, "w");
    if (!f) return;
    size_t written = serializeJson(doc, f);
    f.flush();
    f.close();
    if (written == 0) {
        LittleFS.remove(STRUCT_CACHE_TMP);
        return;
    }
    // Keep the previous valid payload until the replacement is in place.  A
    // failed rename rolls back instead of leaving the device without a cache.
    LittleFS.remove(STRUCT_CACHE_BAK);
    bool hadCurrent = LittleFS.exists(STRUCT_CACHE_FILE);
    if (hadCurrent && !LittleFS.rename(STRUCT_CACHE_FILE, STRUCT_CACHE_BAK)) {
        LittleFS.remove(STRUCT_CACHE_TMP);
        return;
    }
    if (!LittleFS.rename(STRUCT_CACHE_TMP, STRUCT_CACHE_FILE)) {
        if (hadCurrent) LittleFS.rename(STRUCT_CACHE_BAK, STRUCT_CACHE_FILE);
        return;
    }
    LittleFS.remove(STRUCT_CACHE_BAK);
    File p = LittleFS.open(STRUCT_CACHE_PID, "w");
    if (p) { p.print(marker); p.flush(); p.close(); }
    Serial.printf("[CACHE] structured saved pid=%s bytes=%u\n", pid, (unsigned)written);
}

static void saveLastMeta(const char *file, int lv, const char *fv, const char *pid,
                          const char *dateKey, const char *stateKey) {
    File f = LittleFS.open(file, "w");
    if (f) {
        f.printf("%d|%s|%s|%s|%s", lv, fv ? fv : "", pid ? pid : "",
                 dateKey ? dateKey : "", stateKey ? stateKey : "");
        f.close();
    }
}

// 与当前 payload 的 lv/fv/pid/日期键/状态键 完全一致才算未变化
// （跨日或陈旧状态随时间变化时，即使 pid 未变也会触发重绘 —— §7/§8.5）
static bool metaMatches(const char *line, int lv, const char *fv, const char *pid,
                        const char *dateKey, const char *stateKey) {
    if (!line || !*line) return false;
    if (!strchr(line, '|')) return false;   // 旧格式 → 重绘一次
    char tmp[160];
    snprintf(tmp, sizeof(tmp), "%d|%s|%s|%s|%s", lv, fv ? fv : "", pid ? pid : "",
             dateKey ? dateKey : "", stateKey ? stateKey : "");
    return strcmp(tmp, line) == 0;
}

// 设备“今天”键（可信北京时间 YYYY-MM-DD；未校时给固定值，校时跳变会改变 → 触发重绘）
static void todayKey(char *out, size_t cap) {
    time_t now = time(nullptr);
    struct tm *tm_ = localtime(&now);
    if (tm_ && tm_->tm_year >= 123)
        snprintf(out, cap, "%04d-%02d-%02d", tm_->tm_year + 1900, tm_->tm_mon + 1, tm_->tm_mday);
    else
        snprintf(out, cap, "1970-01-01");
}

// 简单 CRC-32（IEEE，用于验收帧完整性校验）
static uint32_t crc32_bytes(const uint8_t *p, size_t n) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        crc ^= p[i];
        for (int k = 0; k < 8; k++) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

bool structuredRenderDocument(JsonDocument &doc, bool pending,
                              bool *changedOut, bool *displayedOut,
                              bool forceRender) {
    if (changedOut) *changedOut = true;
    if (displayedOut) *displayedOut = false;
    g_lastPayloadTs = doc["ts"].as<long long>();

    String ptype = doc["payload_type"] | "";
    if (ptype == "noop") {
        Serial.println("[STRUCT] noop: no module changed, skip refresh");
        if (changedOut) *changedOut = false;
        return true;
    }

    String mode = doc["screen"]["layout_mode"] | "full";
    int lv = doc["screen"]["layout_version"] | 0;
    bool v2panels = (lv >= 2 && doc["screen"]["pages"].is<JsonObject>());
    int curPage = v2panels ? panelCurrentPage() : -1;   // extern 见下
    const char *fv = doc["screen"]["font_version"] | "";
    char pid[64] = {0};
    bool hasPid = layoutGetPayloadId(doc, pid, sizeof(pid));
    char visualPid[80] = {0};
    const char *pageVer = doc["screen"]["versions"]
        [curPage == 1 ? "news_gold_visual_key" : "ai_visual_key"] | "";
    const char *calendarVer = doc["screen"]["versions"]["calendar_key"] | "";
    if (pageVer[0]) snprintf(visualPid, sizeof(visualPid), "%s.%s", pageVer, calendarVer);
    else if (hasPid) snprintf(visualPid, sizeof(visualPid), "%s", pid);
    const char *metaPid = visualPid[0] ? visualPid : pid;

    // 值未变化且无 pending → 跳过刷屏（含 lv/fv/日期键/状态键；按页）
    // 跨日：日期键变化即使 pid 相同也重绘（§7）；状态键变化（陈旧/恢复）同样触发（§8.5）。
    char dk[16] = "";
    todayKey(dk, sizeof(dk));
    char sk[64] = "";
    {
        const char *wa = panelStatusWord(doc, "ai");
        const char *wg = panelStatusWord(doc, "news_gold");
        snprintf(sk, sizeof(sk), "%s/%s", wa ? wa : "?", wg ? wg : "?");
    }
    if (!forceRender && !pending && hasPid && !mode.equalsIgnoreCase("delta")) {
        const char *pf = pidFileFor(curPage, v2panels);
        char last[200] = {0};
        if (loadLastMeta(pf, last, sizeof(last)) && metaMatches(last, lv, fv, metaPid, dk, sk)) {
            Serial.printf("[STRUCT] payload unchanged (page=%d) skip refresh\n", curPage);
            if (changedOut) *changedOut = false;
            return true;
        }
    }

    if (mode.equalsIgnoreCase("delta")) {
        if (!layoutRenderDelta(doc)) return false;
        if (displayedOut) *displayedOut = true;
    } else if (v2panels) {
        bool okp = (curPage == 1) ? renderNewsGoldPanel(doc) : renderAiPanel(doc);
        Serial.printf("[PANEL] v2 page=%d rendered=%s\n", curPage, okp ? "ok" : "fail");
#if defined(FB_DUMP) && FB_DUMP
        {
            // no-image 验收构建：顺次导出 AI(0) 与 资讯+金价(1) 两帧。
            // 标记行带 pid/时间/len/CRC；hex 行结束后给 crc 供校验。
            for (int dumpPage = 0; dumpPage <= 1; dumpPage++) {
                bool dOk = (dumpPage == 0) ? renderAiPanel(doc) : renderNewsGoldPanel(doc);
                if (!dOk) { Serial.printf("[FB] page=%d render fail\n", dumpPage); continue; }
                uint32_t crc = crc32_bytes(imgBuf, (size_t)IMG_BUF_LEN);
                long long tnow = (long long)time(nullptr);
                Serial.printf("[FB] page=%d pid=%s t=%lld len=%d crc=%08lx begin\n",
                              dumpPage, pid, tnow, IMG_BUF_LEN, (unsigned long)crc);
                for (int i = 0; i < IMG_BUF_LEN; i++) {
                    if (imgBuf[i] < 16) Serial.print('0');
                    Serial.print(imgBuf[i], HEX);
                }
                Serial.println();
                Serial.printf("[FB] page=%d end\n", dumpPage);
            }
        }
#endif
        if (!okp) {
            Serial.println("[STRUCT] v2 panel render failed, falling back");
            if (!layoutRenderFull(doc)) { Serial.println("[STRUCT] layout render failed"); return false; }
        }
    } else {
        if (!layoutRenderFull(doc)) {
            Serial.println("[STRUCT] layout render failed, falling back to BMP");
            return false;
        }
    }
    if (hasPid) {
        char dk[16] = "", sk2[64] = "";
        todayKey(dk, sizeof(dk));
        const char *wa = panelStatusWord(doc, "ai");
        const char *wg = panelStatusWord(doc, "news_gold");
        snprintf(sk2, sizeof(sk2), "%s/%s", wa ? wa : "?", wg ? wg : "?");
        saveLastMeta(pidFileFor(curPage, v2panels), lv, fv, metaPid, dk, sk2);
        Serial.printf("[STRUCT] saved meta page=%d pid=%s dk=%s sk=%s\n",
                      curPage, pid, dk, sk2);
    }
    return true;
}

bool structuredFetchAndDisplay(String *renderedModeIdOut, bool *pendingRefreshOut,
                               bool *changedOut, bool *displayedOut) {
    JsonDocument doc;
    bool pending = false;
    if (!fetchStructured(doc, renderedModeIdOut, &pending)) return false;
    if (pendingRefreshOut) *pendingRefreshOut = pending;
    if (requestGoldRefreshForWake(doc)) {
        JsonDocument refreshed;
        String refreshedMode;
        bool refreshedPending = false;
        if (fetchStructured(refreshed, &refreshedMode, &refreshedPending)) {
            doc = refreshed;
            if (renderedModeIdOut) *renderedModeIdOut = refreshedMode;
            pending = refreshedPending;
            if (pendingRefreshOut) *pendingRefreshOut = pending;
        }
    }
    structuredSaveCachedDocument(doc);
    return structuredRenderDocument(doc, pending, changedOut, displayedOut, false);
}

// extern 桥（v2 页面选择）
void enterClockMode() {
    extern int curHour, curMin;
    static uint32_t clockCycle = 0;
    clockCycle++;
    Serial.printf("[CLOCK] entering clock mode, cycle=%u\n", (unsigned)clockCycle);
    showClockMode();
    if (clockCycle % 30 == 0) {
        Serial.println("[CLOCK] full refresh");
        epdDisplay(imgBuf);
    } else {
        epdDisplayFast(imgBuf);
    }
    epdSleep();
    esp_sleep_enable_timer_wakeup(60ULL * 1000000ULL);
    esp_deep_sleep_start();
}

#endif // ENABLE_STRUCTURED
