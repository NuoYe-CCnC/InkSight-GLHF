// panel_pages.cpp — 双面板页面渲染（第二阶段修复版 2026-09-07）
// §12 AI 面板 / §13 资讯+金价。按实拍清单 A1–A7 修复：
//  - A1 会员行独立右对齐且含 PLUS；A2 金额/CNY 间距≥12px（ink 边界量宽）；
//  - A3 已用与自动重置左右分列；A4 人民币/美元标签左、数值右分列；
//  - A5 页脚=真实数据状态+更新时间（左）与唯一品牌（右）；A6 顶部恢复 MM-DD HH:mm；
//  - A7 到期列表保持真实缺失提示。
#include "panel_pages.h"
#include <Preferences.h>
#include <string.h>

#if ENABLE_STRUCTURED

#include <ArduinoJson.h>
#include <math.h>
#include <stdio.h>
#include <time.h>

#include "panel_ui.h"

static const int PW = 800, PH = 480;

int panelCurrentPage() {
#ifdef PANEL_FORCE_PAGE
    return PANEL_FORCE_PAGE;
#else
    Preferences prefs;
    prefs.begin("inksight", false);
    int pg = prefs.getInt("panel_page", 0);
    prefs.end();
    return (pg == 1) ? 1 : 0;
#endif
}

void panelSetPage(int page) {
    int target = (page == 1) ? 1 : 0;
    Preferences prefs;
    prefs.begin("inksight", false);
    int current = prefs.getInt("panel_page", 0);
    if (current != target) prefs.putInt("panel_page", target);
    prefs.end();
    if (current != target) Serial.printf("[PANEL] active page -> %d\n", target);
}

// 字号映射（MiSans 点阵；仓库仅有 Regular/Bold → 大数字/正文 Regular，标题 Bold）
static const MiFont *F_TITLE()  { return &kFonts[MF_BOLD_30]; }   // 栏目标题/主标题
static const MiFont *F_TITLE24(){ return &kFonts[MF_BOLD_24]; }   // 期刊名称（digest）
static const MiFont *F_LABEL()  { return &kFonts[MF_REG_24]; }    // 常规正文/标签
static const MiFont *F_SMALL()  { return &kFonts[MF_REG_21]; }    // 小注
static const MiFont *F_TINY()   { return &kFonts[MF_REG_16]; }    // 更小注/报价时间兜底
static const MiFont *F_BIG()    { return &kFonts[MF_REG_82]; }    // 大数值（82px）
static const MiFont *F_MID()    { return &kFonts[MF_REG_62]; }    // 金价主价（62px）
static const MiFont *F_PRICE_SMALL() { return &kFonts[MF_REG_30]; }

static double jnum(JsonVariantConst v, double dflt) {
    if (v.isNull()) return dflt;
    if (v.is<int>() || v.is<long>()) return (double)v.as<long long>();
    if (v.is<float>() || v.is<double>()) return v.as<double>();
    const char *s = v.as<const char *>();
    if (s) return strtod(s, nullptr);
    return dflt;
}

static void fmtMoney(double v, char *out, size_t cap) {
    if (v != v) { snprintf(out, cap, "--"); return; }  // NaN=未知
    snprintf(out, cap, "%.2f", v);                     // 真实 0 → "0.00"
}

static bool nonnegativeNumber(JsonVariantConst value, double *out) {
    if (value.isNull() || value.is<bool>()) return false;
    if (!(value.is<int>() || value.is<long>() || value.is<long long>() ||
          value.is<float>() || value.is<double>())) return false;
    double number = value.as<double>();
    if (!isfinite(number) || number < 0) return false;
    *out = number;
    return true;
}

static bool finiteNumber(JsonVariantConst value, double *out, bool allowNegative) {
    if (value.isNull() || value.is<bool>()) return false;
    double number = NAN;
    if (value.is<int>() || value.is<long>() || value.is<long long>() ||
        value.is<float>() || value.is<double>()) {
        number = value.as<double>();
    } else if (value.is<const char *>()) {
        const char *raw = value.as<const char *>();
        if (!raw || !raw[0]) return false;
        char *end = nullptr;
        number = strtod(raw, &end);
        if (!end || *end) return false;
    } else return false;
    if (!isfinite(number) || (!allowNegative && number < 0)) return false;
    *out = number;
    return true;
}

static void fmtApiBalance(JsonVariantConst value, char *out, size_t cap) {
    double number = NAN;
    if (!finiteNumber(value, &number, true)) { snprintf(out, cap, "--"); return; }
    if (fabs(number) >= 1.0e12) snprintf(out, cap, "约 %.3e USD", number);
    else {
        // Financial display uses decimal half-up semantics instead of the
        // platform printf implementation's binary floating tie behaviour.
        double rounded = number >= 0
            ? floor(number * 100.0 + 0.5) / 100.0
            : ceil(number * 100.0 - 0.5) / 100.0;
        snprintf(out, cap, "%.2f USD", rounded);
    }
}

static void fmtPointBalance(JsonVariantConst value, char *out, size_t cap) {
    if (value.is<const char *>()) {
        const char *raw = value.as<const char *>();
        double parsed = NAN;
        if (!finiteNumber(value, &parsed, false)) { snprintf(out, cap, "--"); return; }
        if (strlen(raw) + 1 > cap) { snprintf(out, cap, "数值过大"); return; }
        snprintf(out, cap, "%s", raw);
        return;
    }
    double number = NAN;
    if (!nonnegativeNumber(value, &number)) { snprintf(out, cap, "--"); return; }
    if (number >= 1.0e15) { snprintf(out, cap, "约 %.3e", number); return; }
    char raw[48];
    bool whole = fabs(number - floor(number + 0.5)) < 0.000001;
    snprintf(raw, sizeof(raw), whole ? "%.0f" : "%.2f", number);
    const char *dot = strchr(raw, '.');
    size_t integerLen = dot ? (size_t)(dot - raw) : strlen(raw);
    size_t commas = integerLen > 0 ? (integerLen - 1) / 3 : 0;
    if (integerLen + commas + (dot ? strlen(dot) : 0) + 1 > cap) {
        snprintf(out, cap, "数值过大");
        return;
    }
    size_t oi = 0;
    for (size_t i = 0; i < integerLen; i++) {
        if (i > 0 && (integerLen - i) % 3 == 0) out[oi++] = ',';
        out[oi++] = raw[i];
    }
    if (dot) {
        while (*dot && oi + 1 < cap) out[oi++] = *dot++;
    }
    out[oi] = 0;
}

static void fmtSignedDelta(double v, char *out, size_t cap) {
    if (v != v) { snprintf(out, cap, "--"); return; }
    if (fabs(v) < 0.005) { snprintf(out, cap, "0.00"); return; }
    snprintf(out, cap, v > 0 ? "+%.2f" : "%.2f", v);
}

static void fmtTodayTokens(JsonObjectConst ai, char *out, size_t cap) {
    JsonVariantConst value = ai["deepseek_today_tokens"];
    if (value.isNull()) { snprintf(out, cap, "--"); return; }
    long long tokens = value.as<long long>();
    if (tokens < 0) { snprintf(out, cap, "--"); return; }
    bool complete = ai["deepseek_today_tokens_complete"] | false;
    snprintf(out, cap, "%lld%s", tokens, complete ? "" : "+");
}

static void fmtCompactTodayTokens(JsonObjectConst ai, char *out, size_t cap) {
    JsonVariantConst value = ai["deepseek_today_tokens"];
    if (value.isNull()) { snprintf(out, cap, "--"); return; }
    long long tokens = value.as<long long>();
    if (tokens < 0) { snprintf(out, cap, "--"); return; }
    bool complete = ai["deepseek_today_tokens_complete"] | false;
    const char *suffix = complete ? "" : "+";
    if (tokens < 1000) {
        snprintf(out, cap, "%lld%s", tokens, suffix);
        return;
    }

    const long long divisors[] = {1000LL, 1000000LL, 1000000000LL};
    const char units[] = {'k', 'M', 'B'};
    int unit = tokens < divisors[1] ? 0 : (tokens < divisors[2] ? 1 : 2);
    double scaled = (double)tokens / (double)divisors[unit];
    int decimals = scaled < 10.0 ? 2 : (scaled < 100.0 ? 1 : 0);
    double rounded = scaled;
    for (int i = 0; i < 2; i++) {
        double rounding = decimals == 2 ? 100.0 : (decimals == 1 ? 10.0 : 1.0);
        rounded = floor(scaled * rounding + 0.5) / rounding;
        int adjusted = rounded < 10.0 ? 2 : (rounded < 100.0 ? 1 : 0);
        if (adjusted == decimals) break;
        decimals = adjusted;
    }
    if (rounded >= 1000.0 && unit < 2) {
        unit++;
        scaled = (double)tokens / (double)divisors[unit];
        decimals = scaled < 10.0 ? 2 : (scaled < 100.0 ? 1 : 0);
        for (int i = 0; i < 2; i++) {
            double rounding = decimals == 2 ? 100.0 : (decimals == 1 ? 10.0 : 1.0);
            rounded = floor(scaled * rounding + 0.5) / rounding;
            int adjusted = rounded < 10.0 ? 2 : (rounded < 100.0 ? 1 : 0);
            if (adjusted == decimals) break;
            decimals = adjusted;
        }
    }
    snprintf(out, cap, "%.*f%c%s", decimals, rounded, units[unit], suffix);
}

// ── 共享：时间/状态/页脚/自适应字号 ─────────────────────────
static void fmtMdHm(time_t t, char *out, size_t cap) {
    struct tm tm_ = {};
    if (localtime_r(&t, &tm_) && tm_.tm_year >= 123)
        snprintf(out, cap, "%02d-%02d %02d:%02d", tm_.tm_mon + 1, tm_.tm_mday,
                 tm_.tm_hour, tm_.tm_min);
    else snprintf(out, cap, "--");
}

static void fmtHm(time_t t, char *out, size_t cap) {
    struct tm tm_ = {};
    if (localtime_r(&t, &tm_) && tm_.tm_year >= 123) snprintf(out, cap, "%02d:%02d", tm_.tm_hour, tm_.tm_min);
    else snprintf(out, cap, "--:--");
}

// ── 公共区域（§3.1）与日期栏（§4）───────────────────────────
// User-confirmed 2026-09-06 frame: 24..778, header y50, footer rule y427.
enum { PUB_L = 24, PUB_R = 778, PUB_TOP_Y = 39, PUB_HDR_Y = 50,
       PUB_FTR_Y = 427, PUB_FOOT_Y = 461 };
static const char *kWEEK_CN[7] = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};  // tm_wday

static bool timeTrusted(time_t t) {
    struct tm tm_ = {};
    return localtime_r(&t, &tm_) && tm_.tm_year >= 123;   // 已校时（NTP 后 ≥2023）
}

static bool sameLocalDate(time_t a, time_t b) {
    struct tm ta = {}, tb = {};
    return timeTrusted(a) && timeTrusted(b) && localtime_r(&a, &ta) && localtime_r(&b, &tb) &&
           ta.tm_year == tb.tm_year && ta.tm_yday == tb.tm_yday;
}

static void fmtYmd(time_t t, char *out, size_t cap) {
    struct tm tm_ = {};
    if (localtime_r(&t, &tm_) && tm_.tm_year >= 123)
        snprintf(out, cap, "%04d-%02d-%02d", tm_.tm_year + 1900, tm_.tm_mon + 1, tm_.tm_mday);
    else snprintf(out, cap, "");
}

// 右上日期栏：优先取 screen.calendar 当天行（a 完整 / b 去农历，按公共预算选），
// 表缺失/超范围 → 降级“MM-DD 周X”（公历+星期，农历不可用如实不显示）；时间不可信 → “日期待校准”。
static void topDateText(const JsonDocument &doc, char *out, size_t cap) {
    out[0] = 0;
    time_t now = time(nullptr);
    if (!timeTrusted(now)) { snprintf(out, cap, "日期待校准"); return; }
    struct tm tm_ = {};
    if (!localtime_r(&now, &tm_)) { snprintf(out, cap, "日期待校准"); return; }
    char base[24];
    JsonObjectConst prefs = doc["screen"]["display_preferences"].as<JsonObjectConst>();
    bool showWeekday = prefs.isNull() ? true : (prefs["show_weekday"] | true);
    if (showWeekday)
        snprintf(base, sizeof(base), "%02d-%02d %s", tm_.tm_mon + 1, tm_.tm_mday,
                 kWEEK_CN[tm_.tm_wday]);
    else
        snprintf(base, sizeof(base), "%02d-%02d", tm_.tm_mon + 1, tm_.tm_mday);
    char ymd[16];
    fmtYmd(now, ymd, sizeof(ymd));
    long budget = 560;
    JsonObjectConst screen = doc["screen"].as<JsonObjectConst>();
    JsonObjectConst cal = screen["calendar"].as<JsonObjectConst>();
    if (!cal.isNull()) budget = cal["budget"].as<long long>() ? cal["budget"].as<long long>() : 560;
    JsonArrayConst days = cal["days"].as<JsonArrayConst>();
    for (JsonVariantConst v : days) {
        JsonObjectConst row = v.as<JsonObjectConst>();
        if (row.isNull()) continue;
        const char *d = row["d"] | "";
        if (strcmp(d, ymd) == 0) {
            const char *a = row["a"] | "";
            const char *b = row["b"] | "";
            if (a[0] && panelTextInkWidth(F_SMALL(), a) <= (int)budget) { snprintf(out, cap, "%s", a); return; }
            if (b[0] && panelTextInkWidth(F_SMALL(), b) <= (int)budget) { snprintf(out, cap, "%s", b); return; }
            snprintf(out, cap, "%s", base);  // 兜底（极端宽组合）
            return;
        }
    }
    snprintf(out, cap, "%s", base);   // 降级：公历+星期
}

// 页脚左文案：<状态> · 更新 <MM-DD?>HH:mm；ts≤0 → --:--；跨日（非今天）加日期前缀
static void fmtUpdText(char *out, size_t cap, const char *status, long long ts) {
    if (ts > 0) {
        time_t t2 = (time_t)ts;
        struct tm shown = {};
        if (localtime_r(&t2, &shown) && shown.tm_year >= 123) {
            time_t nowT = time(nullptr);
            struct tm current = {};
            if (localtime_r(&nowT, &current) && current.tm_year == shown.tm_year &&
                current.tm_mon == shown.tm_mon && current.tm_mday == shown.tm_mday)
                snprintf(out, cap, "%s · 更新 %02d:%02d", status, shown.tm_hour, shown.tm_min);
            else
                snprintf(out, cap, "%s · 更新 %02d-%02d %02d:%02d", status,
                         shown.tm_mon + 1, shown.tm_mday, shown.tm_hour, shown.tm_min);
            return;
        }
    }
    snprintf(out, cap, "%s · 更新 --:--", status);
}

// 数据更新时间 = 发布端模型更新时间（doc 顶层 ts；非设备钟、非供应商报价时间）
static long long payloadTsOf(const JsonDocument &doc) {
    return doc["ts"].as<long long>();
}

// 字段级状态词：missing=缺失/坏值，stale=存在但来源陈旧（last 距今超 staleAfter）
static const char *srcWord(bool missing, long long last, long long nowSec, long long staleAfter) {
    if (missing) return "missing";
    if (last > 0 && staleAfter > 0 && (nowSec - last) > staleAfter) return "stale";
    return "ok";
}

// 统一页脚状态（AI 页/资讯页各自计算，共用聚合规则 §8.5）。
// 依据整份 ts 语义（§8.1）与各来源字段；fresh 子对象（发布端可选下发 last_success）缺省时
// 按“存在即 ok”，缺失即 missing；AI 来源阈值由 freshness_policy 下发，
// 默认按真实采集策略连续 3 个周期：Codex 3x600s，DeepSeek CNY/USD 各 3x60s。
static long long sourceStaleAfter(JsonObjectConst ai, const char *source, long long fallback) {
    long long value = ai["freshness_policy"][source]["stale_after_s"].as<long long>();
    return (value > 0 && value <= 7LL * 86400) ? value : fallback;
}

static const char *pageStatusText(const JsonDocument &doc, const char *page, int *anomalyOut) {
    time_t now = time(nullptr);
    if (!timeTrusted(now)) { if (anomalyOut) *anomalyOut = 1; return "时间待校准"; }
    long long nowS = (long long)now;
    const long long A2H = 7200;
    JsonObjectConst screen = doc["screen"].as<JsonObjectConst>();
    JsonObjectConst ai = screen["pages"]["ai"].as<JsonObjectConst>();
    JsonObjectConst ng = screen["pages"]["news_gold"].as<JsonObjectConst>();
    bool digestMode = false, wholeMissing = false, wholeStale = false, wholeFuture = false;
    bool newsStale = false;   // digest 期次错过（§8.4）
    long long ts = payloadTsOf(doc);
    if (ts <= 0) wholeMissing = true;
    else if (ts > nowS + 300) wholeFuture = true;
    else if ((nowS - ts) > A2H) wholeStale = true;

    // 各来源原始判定
    bool miss[8] = {false,false,false,false,false,false,false,false};
    long long last[8] = {0,0,0,0,0,0,0,0};
    const long long ST[8] = {sourceStaleAfter(ai, "codex", 1800),
                             sourceStaleAfter(ai, "ds_cny", 180),
                             sourceStaleAfter(ai, "ds_usd", 180),
                             0, 0, 0, 0, 24LL*3600};
    // idx: 0 codex,1 ds_cny,2 ds_usd,3 member,4 news(内容),5 news(期次),6 备用,7 gold
    JsonObjectConst fr = ai["fresh"].as<JsonObjectConst>();
    JsonObjectConst gd = ng["gold"].as<JsonObjectConst>();
    JsonObjectConst nw = ng["news"].as<JsonObjectConst>();

    // Codex
    if (ai["codex_7d_used"].isNull()) miss[0] = true;
    else last[0] = fr["codex"].as<long long>();
    // DeepSeek CNY/USD 独立
    if (ai["deepseek"]["CNY"].isNull()) miss[1] = true; else last[1] = fr["ds_cny"].as<long long>();
    if (ai["deepseek"]["USD"].isNull()) miss[2] = true; else last[2] = fr["ds_usd"].as<long long>();
    // 会员（手动确认语义：存在即有效；fresh 可选）
    const char *plan = ai["plan"] | "";
    const char *valid = ai["valid_until_date"] | "";
    if (!plan[0] || !valid[0]) miss[3] = true; else last[3] = fr["member"].as<long long>();
    // 新闻（资讯页）
    const char *nmode = nw["mode"] | "";
    if (strcmp(nmode, "digest") == 0 || strcmp(nmode, "daily_message") == 0 ||
        strcmp(nmode, "status") == 0) {
        digestMode = true;
        const char *dt = nw["text"] | "";
        const char *nfr = nw["freshness"] | "";
        if (!dt[0]) miss[4] = true;
        else if (strcmp(nfr, "error") == 0) miss[4] = true;
        else if (strcmp(nfr, "stale") == 0) newsStale = true;   // 期次错过：存在但陈旧
        long long gen = nw["generated_at"].as<long long>();
        if (gen > 0) last[4] = gen;
        if (gen <= 0) miss[5] = true;
    } else {
        JsonArrayConst briefs = nw["items"].as<JsonArrayConst>();
        if (!briefs.isNull()) {
            int n = briefs.size();
            if (n == 0) miss[4] = true;
            else { for (JsonVariantConst v : briefs) { JsonObjectConst it = v.as<JsonObjectConst>();
                       const char *tx = it.isNull() ? "" : (it["text"] | "");
                       if (!tx[0]) miss[4] = true; } }
        } else {
            static const char *keys[3] = {"general", "tech_ai", "finance"};
            for (int i = 0; i < 3; i++) { JsonObjectConst it = nw[keys[i]].as<JsonObjectConst>();
                if (it.isNull()) { miss[4] = true; continue; }
                if (!((it["title"] | "")[0])) miss[4] = true; }
        }
    }
    // XAUS 三指标必须成组有效；来源标记 stale 时页面状态报告陈旧。
    if (!gd.isNull()) {
        if (gd["price_gram_cny"].isNull() || gd["spot_usd_oz"].isNull() ||
            gd["fx_rate"].isNull() || gd["price_as_of"].isNull()) miss[7] = true;
        else last[7] = gd["price_as_of"].as<long long>();
        const char *goldState = gd["data_state"]["status"] | "";
        if ((gd["stale"] | false) || (gd["fx_stale"] | false) ||
            (goldState[0] && strcmp(goldState, "fresh") != 0) ||
            !sameLocalDate(now, (time_t)last[7])) last[7] = 1;
    } else miss[7] = true;

    int any = 0;
    bool hasMissing = false, hasStale = false;
    // 每页只统计其显示的必要来源：AI 页=Codex/DS CNY/USD/会员；资讯页=再加新闻(内容/期次)与金价
    bool use[8] = {true, true, true, true, false, false, false, false};
    if (strcmp(page, "news_gold") == 0) { use[4] = use[5] = use[7] = true; }
    if (newsStale && use[4]) { hasStale = true; any++; newsStale = false; }
    for (int i = 0; i < 8; i++) {
        if (!use[i]) continue;
        if (miss[i]) { hasMissing = true; any++; }
        else {
            const char *w = srcWord(false, last[i], nowS, ST[i]);
            if (strcmp(w, "stale") == 0) { hasStale = true; any++; }
        }
    }
    if (anomalyOut) *anomalyOut = any;
    if (strcmp(page, "news_gold") == 0) {
        const char *localUpdate = nw["device_update_state"] | "";
        long long localUntil = nw["device_update_expires_at"].as<long long>();
        if (localUntil >= nowS && strcmp(localUpdate, "requested") == 0)
            return "早报待更新";
        if (localUntil >= nowS && strcmp(localUpdate, "unreachable") == 0)
            return "资讯暂未更新";
        const char *updateState = nw["update_state"] | "";
        if (strcmp(updateState, "due") == 0) return "早报待更新";
        if (strcmp(updateState, "not-updated") == 0) return "资讯暂未更新";
    }
    // 聚合优先级（§8.5）：时间待校准 > 数据陈旧(整份) > 部分数据陈旧 > 部分数据缺失 > 数据正常
    if (wholeMissing) return hasMissing ? "部分数据缺失" : "部分数据陈旧";
    if (wholeFuture) return hasStale ? "数据陈旧" : "部分数据陈旧";
    if (hasStale && hasMissing) return "部分数据陈旧";
    if (wholeStale) return "数据陈旧";
    if (hasStale) return "部分数据陈旧";
    if (hasMissing) return "部分数据缺失";
    (void)digestMode; (void)plan; (void)valid; (void)fr; (void)gd; (void)last; (void)miss;
    return "数据正常";
}

// 供 structured_display 状态键使用
const char *panelStatusWord(const JsonDocument &doc, const char *page) {
    int n = 0;
    return pageStatusText(doc, page, &n);
}

// 候选字档里取首个 ink 宽 <= maxW 的档下标（无匹配取末档）
static int pickFitFont(const MiFont *cands[], int n, const char *txt, int maxW) {
    for (int i = 0; i < n; i++) {
        if (panelTextInkWidth(cands[i], txt) <= maxW) return i;
    }
    return n - 1;
}

static void drawRightFit(const char *txt, int rightX, int baseline, int maxW,
                         const MiFont *preferred) {
    const MiFont *cands[] = {preferred, F_SMALL(), F_TINY()};
    int start = preferred == F_LABEL() ? 0 : 1;
    int fi = pickFitFont(cands + start, 3 - start, txt, maxW) + start;
    if (panelTextInkWidth(cands[fi], txt) <= maxW) {
        panelDrawTextRight(cands[fi], txt, rightX, baseline);
    } else {
        panelDrawTextRight(F_TINY(), "数值过大", rightX, baseline);
    }
}

static int drawOpticalLeftLabel(const char *txt, int visibleLeft, int baseline) {
    int penX = visibleLeft - panelTextVisibleLeftOffset(F_SMALL(), txt);
    panelDrawText(F_SMALL(), txt, penX, baseline);
    return penX + panelTextInkWidth(F_SMALL(), txt);
}

// 金价与涨跌按实际 ink 边界排布。正常值保持 Reg30；仅异常长值降到 Reg16，
// 再极端时截断主价，始终给 Reg16 涨跌保留 12px 可见间距。
static void drawGoldPriceDelta(const char *price, const char *delta,
                               int leftX, int rightLimit, int baseline) {
    const MiFont *priceCands[] = {F_PRICE_SMALL(), F_TINY()};
    int deltaW = panelTextInkWidth(F_TINY(), delta);
    int priceMaxW = rightLimit - leftX - deltaW - 12;
    if (priceMaxW < 1) return;
    int fi = pickFitFont(priceCands, 2, price, priceMaxW);
    int priceW = panelTextInkWidth(priceCands[fi], price);
    if (priceW <= priceMaxW) {
        panelDrawText(priceCands[fi], price, leftX, baseline);
        panelDrawText(F_TINY(), delta, leftX + priceW + 12, baseline);
        return;
    }
    panelDrawTextTrunc(priceCands[fi], price, leftX, baseline, priceMaxW);
    panelDrawText(F_TINY(), delta, rightLimit - deltaW, baseline);
}

// ── AI 面板（§12 修复版坐标）────────────────────────────────
bool renderAiPanel(const JsonDocument &doc) {
    JsonObjectConst screen = doc["screen"].as<JsonObjectConst>();
    JsonObjectConst ai = screen["pages"]["ai"].as<JsonObjectConst>();
    JsonObjectConst displayPrefs = screen["display_preferences"].as<JsonObjectConst>();
    bool showReset = displayPrefs.isNull() ? true : (displayPrefs["show_reset_opportunities"] | true);
    bool showCodex = displayPrefs.isNull() ? false : (displayPrefs["show_codex_credits"] | false);
    bool showApi = displayPrefs.isNull() ? false : (displayPrefs["show_openai_api_info"] | false);

    char s[160];
    const int cnyX0 = 24, codexRight = 461, dsLeft = 500, pageRight = 778;

    panelClear();

    // 用户确认版：右上显示日期、星期、农历，节气/节日当天一并显示。
    char dbar[96];
    topDateText(doc, dbar, sizeof(dbar));
    panelDrawText(F_TITLE(), "AI 用量", PUB_L, PUB_TOP_Y);
    panelDrawTextRight(F_SMALL(), dbar, PUB_R, PUB_TOP_Y);
    panelHLine(PUB_L, PUB_R, PUB_HDR_Y, 2);

    // 栏目标题
    panelDrawText(F_TITLE(), "CODEX", cnyX0, 93);
    panelDrawText(F_TITLE(), "DEEPSEEK", dsLeft, 93);

    // A1 会员行：整行“PLUS · 有效至 MM-DD”右对齐 461（含空格；CODEX 间距 ≥16 由布局保证）
    const char *plan = ai["plan"] | "";
    const char *valid = ai["valid_until_date"] | "";
    const char *mmdd = (strlen(valid) >= 10) ? (valid + 5) : valid;
    snprintf(s, sizeof(s), "%s · 有效至 %.5s", plan[0] ? plan : "会员", mmdd);
    if (panelTextInkWidth(F_SMALL(), s) <= (codexRight - cnyX0 - 16)) {
        panelDrawTextRight(F_SMALL(), s, codexRight, 93);
    } else {
        panelDrawTextRight(F_TINY(), s, codexRight, 93);   // 空间不足只缩会员行
    }

    // 主数值（A2：金额与 CNY 间距 ≥12px；两主值同字号档）
    double used = jnum(ai["codex_7d_used"], -1);
    bool usedOk = (used >= 0 && used <= 100.0);   // 未知/非数值/越界 → 不绘误导比例
    char cxTxt[24];
    if (usedOk) snprintf(cxTxt, sizeof(cxTxt), "%.0f%%", 100.0 - used);
    else snprintf(cxTxt, sizeof(cxTxt), "--%%");
    double dsCny = jnum(ai["deepseek"]["CNY"], NAN);
    char dsTxt[40];
    if (dsCny != dsCny) snprintf(dsTxt, sizeof(dsTxt), "--");
    else fmtMoney(dsCny, dsTxt, sizeof(dsTxt));
    const MiFont *mainCands[] = {F_BIG(), &kFonts[MF_REG_62], &kFonts[MF_REG_30]};
    int cnyW = panelTextInkWidth(F_LABEL(), "CNY");
    int dsLimit = (pageRight - cnyW - 12) - dsLeft;   // 金额可用宽（右缘 ≤ CNY 左缘-12）
    int cxLimit = codexRight - cnyX0;
    int fi = 0;
    while (fi < 2) {
        bool cxOk = panelTextInkWidth(mainCands[fi], cxTxt) <= cxLimit;
        bool dsOk = panelTextInkWidth(mainCands[fi], dsTxt) <= dsLimit;
        if (cxOk && dsOk) break;
        fi++;
    }
    panelDrawText(mainCands[fi], cxTxt, cnyX0, 191);
    panelDrawText(mainCands[fi], dsTxt, dsLeft, 191);
    panelDrawTextRight(F_LABEL(), "CNY", pageRight, 191);

    // User-selected automatic account rows keep the approved right-side layout.
    // API is always above points when both are enabled; missing stays explicit.
    const char *accountLabels[2] = {nullptr, nullptr};
    char accountValues[2][64] = {{0}, {0}};
    int accountCount = 0;
    if (showApi) {
        accountLabels[accountCount] = "API 本月消费";
        fmtApiBalance(ai["openai_api_month_spend_usd"], accountValues[accountCount],
                      sizeof(accountValues[accountCount]));
        if (strcmp(accountValues[accountCount], "--") == 0)
            snprintf(accountValues[accountCount], sizeof(accountValues[accountCount]), "-- USD");
        accountCount++;
    }
    if (showCodex) {
        accountLabels[accountCount] = "点数余额";
        char points[48];
        if (ai["codex_credit_unlimited"] | false)
            snprintf(points, sizeof(points), "不限量");
        else
            fmtPointBalance(ai["codex_credit_balance"], points, sizeof(points));
        if (strcmp(points, "数值过大") == 0)
            snprintf(accountValues[accountCount], sizeof(accountValues[accountCount]), "%s", points);
        else
            snprintf(accountValues[accountCount], sizeof(accountValues[accountCount]), "%s", points);
        accountCount++;
    }
    if (accountCount == 1) {
        panelDrawTextRight(F_LABEL(), accountLabels[0], codexRight, 225);
        drawRightFit(accountValues[0], codexRight, 257, codexRight - cnyX0, F_LABEL());
    } else if (accountCount == 2) {
        const int labelY[2] = {161, 225};
        const int valueY[2] = {192, 257};
        for (int i = 0; i < 2; i++) {
            panelDrawTextRight(F_LABEL(), accountLabels[i], codexRight, labelY[i]);
            drawRightFit(accountValues[i], codexRight, valueY[i], codexRight - cnyX0, F_LABEL());
        }
    }

    // 标签行
    panelDrawText(F_LABEL(), "剩余", cnyX0, 225);
    panelDrawText(F_LABEL(), "可用余额", dsLeft, 240);
    panelDrawText(F_LABEL(), "7 日窗口", cnyX0, 256);

    // 分隔：竖线相对上下公共横线各留 12px；右栏横线 y270；左栏横线 y343
    panelVLine(480, 62, 415, 2);
    panelHLine(dsLeft, pageRight, 270, 2);
    panelHLine(cnyX0, codexRight, 343, 2);

    // 进度条 x24..461 y270..296（两页统一：黑色=剩余=100−已用；内缘填充不盖边框）
    panelRectOutline(cnyX0, 270, codexRight - cnyX0, 26, 2);
    if (usedOk) {
        double rem = 100.0 - used;
        if (rem < 0) rem = 0;
        if (rem > 100) rem = 100;
        int fill = (int)((codexRight - cnyX0 - 4) * rem / 100.0);
        if (fill > 0) panelFillRect(cnyX0 + 2, 272, fill, 22);
    }

    // A3 已用（左24,323）与自动重置（右461,323）分列，间距≥16
    char usedTxt[48];
    if (usedOk) snprintf(usedTxt, sizeof(usedTxt), "已用 %.0f%%", used);
    else snprintf(usedTxt, sizeof(usedTxt), "已用 --%%");
    panelDrawText(F_LABEL(), usedTxt, cnyX0, 328);
    long long ra = ai["codex_resets_at"].as<long long>();
    char resetTxt[48];
    if (ra > 0) {
        char md[24];
        fmtMdHm((time_t)ra, md, sizeof(md));
        snprintf(resetTxt, sizeof(resetTxt), "自动重置 %s", md);
    } else snprintf(resetTxt, sizeof(resetTxt), "自动重置 --");
    {
        int usedRight = cnyX0 + panelTextInkWidth(F_LABEL(), usedTxt);
        if (codexRight - panelTextInkWidth(F_SMALL(), resetTxt) - usedRight >= 16) {
            panelDrawTextRight(F_SMALL(), resetTxt, codexRight, 328);
        } else {
            panelDrawTextRight(F_TINY(), resetTxt, codexRight, 328);
        }
    }

    // A4 人民币/美元余额：标签左500，数值“金额 CNY/USD”右778（40px 行距）
    double dsUsd = jnum(ai["deepseek"]["USD"], NAN);
    char m1[40], m2[40];
    fmtMoney(dsCny, m1, sizeof(m1));
    fmtMoney(dsUsd, m2, sizeof(m2));
    const char *cur1 = (dsCny != dsCny) ? "" : " CNY";   // 未知→“--”不带币种，不冒充
    const char *cur2 = (dsUsd != dsUsd) ? "" : " USD";
    snprintf(s, sizeof(s), "%s%s", m1, cur1);
    int valW = panelTextInkWidth(F_LABEL(), s);
    panelDrawText(F_LABEL(), "人民币余额", dsLeft, 315);
    if (dsLeft + panelTextInkWidth(F_LABEL(), "人民币余额") + 12 <= pageRight - valW) {
        panelDrawTextRight(F_LABEL(), s, pageRight, 315);
    } else {
        panelDrawTextRight(F_SMALL(), s, pageRight, 315);
    }
    snprintf(s, sizeof(s), "%s%s", m2, cur2);
    panelDrawText(F_LABEL(), "美元余额", dsLeft, 355);
    if (dsLeft + panelTextInkWidth(F_LABEL(), "美元余额") + 12 <= pageRight - panelTextInkWidth(F_LABEL(), s)) {
        panelDrawTextRight(F_LABEL(), s, pageRight, 355);
    } else {
        panelDrawTextRight(F_SMALL(), s, pageRight, 355);
    }
    char tokenTxt[32];
    fmtCompactTodayTokens(ai, tokenTxt, sizeof(tokenTxt));
    const char *tokenLabel = "今日 Token";
    panelDrawText(F_LABEL(), tokenLabel, dsLeft, 395);
    const MiFont *tokenFonts[] = {F_LABEL(), F_SMALL(), F_TINY()};
    int tokenMaxW = pageRight - (dsLeft + panelTextInkWidth(F_LABEL(), tokenLabel) + 12);
    int tokenFi = pickFitFont(tokenFonts, 3, tokenTxt, tokenMaxW);
    panelDrawTextRight(tokenFonts[tokenFi], tokenTxt, pageRight, 395);

    // A7 手动重置：次数与到期列表分开（次数实时；列表按状态显示）。
    // 显示开关只隐藏对应行，不改变已确认的其它坐标。
    JsonVariantConst creditsValue = ai["reset_credits_available"];
    double creditsNumber = NAN;
    bool creditsKnown = nonnegativeNumber(creditsValue, &creditsNumber) &&
                        floor(creditsNumber) == creditsNumber && creditsNumber <= 2147483647.0;
    int credits = creditsKnown ? (int)creditsNumber : -1;
    if (creditsKnown) snprintf(s, sizeof(s), "手动重置 %d 次可用", credits);
    else snprintf(s, sizeof(s), "手动重置 -- 次可用");
    bool resetCreditsConfirmed = ai["reset_credits_confirmed"].is<bool>() &&
                                 ai["reset_credits_confirmed"].as<bool>();
    bool lowerBalances = creditsKnown && credits == 0 && resetCreditsConfirmed &&
                         !showCodex && !showApi;
    if (showReset && !lowerBalances) panelDrawText(F_SMALL(), s, cnyX0, 380);
    // 到期行：None=未接通(暂不可用) / []=明确0次(到期 --) / 列表=升序显示 MM-DD HH:mm
    // 注意 v7：const 变体须用 as<JsonArrayConst>() 判断（is<JsonArray>() 对 const 恒 false）
    JsonVariantConst expv = ai["reset_expiry_list"];
    JsonArrayConst expArr = expv.as<JsonArrayConst>();
    char expTxt[96] = {0};
    if (expArr.isNull()) {
        snprintf(expTxt, sizeof(expTxt), "到期时间暂不可用");
    } else if (expArr.size() == 0) {
        snprintf(expTxt, sizeof(expTxt), "到期 --");
    } else {
        int shown = 0;
        bool more = false;
        time_t nowT2 = time(nullptr);
        for (JsonVariantConst v : expArr) {
            long long e = v.as<long long>();
            if (e <= 0 || e <= (long long)nowT2) continue;  // 过期机会不再可用，不显示
            char md[24];
            fmtMdHm((time_t)e, md, sizeof(md));
            char item[40];
            snprintf(item, sizeof(item), "%s%s", shown ? " / " : "", md);
            if ((int)strlen(expTxt) + (int)strlen(item) >= (int)sizeof(expTxt) - 4) { more = true; break; }
            strcat(expTxt, item);
            shown++;
        }
        if (shown == 0) {
            snprintf(expTxt, sizeof(expTxt), "到期 --");
        } else {
            char full[100];
            snprintf(full, sizeof(full), "到期 %s%s", expTxt, more ? " …" : "");
            snprintf(expTxt, sizeof(expTxt), "%s", full);
        }
    }
    if (showReset && !lowerBalances) {
        if (panelTextInkWidth(F_SMALL(), expTxt) <= pageRight - cnyX0) {
            panelDrawText(F_SMALL(), expTxt, cnyX0, 407);
        } else {
            panelDrawText(F_TINY(), expTxt, cnyX0, 407);
        }
    }
    if (lowerBalances) {
        char apiTxt[64], pointTxt[64];
        fmtApiBalance(ai["openai_api_month_spend_usd"], apiTxt, sizeof(apiTxt));
        if (ai["codex_credit_unlimited"] | false)
            snprintf(pointTxt, sizeof(pointTxt), "不限量");
        else
            fmtPointBalance(ai["codex_credit_balance"], pointTxt, sizeof(pointTxt));
        int apiRight = drawOpticalLeftLabel("API 本月消费", cnyX0, 377);
        drawRightFit(apiTxt, codexRight, 377, codexRight - apiRight - 16, F_SMALL());
        int pointsRight = drawOpticalLeftLabel("点数余额", cnyX0, 407);
        drawRightFit(pointTxt, codexRight, 407, codexRight - pointsRight - 16, F_SMALL());
    }
    // 用户确认版页脚：分隔线 y427；文字仍保留真实状态与更新时间。
    panelHLine(PUB_L, PUB_R, PUB_FTR_Y, 2);
    int miss = 0;
    const char *st = pageStatusText(doc, "ai", &miss);
    fmtUpdText(s, sizeof(s), st, payloadTsOf(doc));
    panelDrawText(F_SMALL(), s, PUB_L, PUB_FOOT_Y);
    panelDrawTextRight(F_SMALL(), "INKSIGHT", PUB_R, PUB_FOOT_Y);
    return true;
}

// ── 资讯＋金价面板（§13；共享修复同步）────────────────────────
bool renderNewsGoldPanel(const JsonDocument &doc) {
    JsonObjectConst screen = doc["screen"].as<JsonObjectConst>();
    JsonObjectConst ng = screen["pages"]["news_gold"].as<JsonObjectConst>();
    JsonObjectConst gold = ng["gold"].as<JsonObjectConst>();
    JsonObjectConst news = ng["news"].as<JsonObjectConst>();

    char s[256];
    const int L = PUB_L, R = PUB_R;

    time_t now = time(nullptr);

    panelClear();
    char dbar[96];
    topDateText(doc, dbar, sizeof(dbar));
    panelDrawText(F_TITLE(), "今日关注", PUB_L, PUB_TOP_Y);
    panelDrawTextRight(F_SMALL(), dbar, PUB_R, PUB_TOP_Y);
    panelHLine(PUB_L, PUB_R, PUB_HDR_Y, 2);

    // XAUS 三指标区：USD 比较北京零点；CNY 缺零点时明确比较当天首次有效报价。
    double usdOz = jnum(gold["spot_usd_oz"], NAN);
    double cnyGram = jnum(gold["price_gram_cny"], NAN);
    double fxRate = jnum(gold["fx_rate"], NAN);
    time_t priceAt = (time_t)(long long)(gold["price_as_of"] | 0LL);
    bool quoteToday = timeTrusted(now) && sameLocalDate(now, priceAt);
    bool goldStale = (gold["stale"] | false) ||
                     strcmp(gold["data_state"]["status"] | "fresh", "fresh") != 0 ||
                     (timeTrusted(now) && !quoteToday);
    panelDrawText(F_SMALL(), goldStale ? "伦敦金 较旧" : "伦敦金", L, 83);
    const int CNY_X = 330;
    panelDrawText(F_SMALL(), "人民币金价", CNY_X, 83);
    panelDrawTextRight(F_TINY(), "美元人民币汇率", R, 83);

    // Keep every displayed metric in its own buffer. Reusing one buffer here
    // previously let the later FX formatting overwrite the CNY/g text while
    // its independently formatted delta stayed correct.
    char usdValue[40], cnyValue[40], fxValue[40], delta[40], hm[16];
    fmtMoney(usdOz, usdValue, sizeof(usdValue));
    time_t usdBaseAt = (time_t)(long long)(gold["baseline_price_as_of"] | 0LL);
    double usdDelta = quoteToday && sameLocalDate(now, usdBaseAt) ?
        jnum(gold["change_usd_oz_since_bj_midnight"], NAN) : NAN;
    fmtSignedDelta(usdDelta, delta, sizeof(delta));
    drawGoldPriceDelta(usdValue, delta, L, 282, 124);

    fmtMoney(cnyGram, cnyValue, sizeof(cnyValue));
    double cnyDelta = jnum(gold["change_cny_g_since_reference"], NAN);

    fmtMoney(fxRate, fxValue, sizeof(fxValue));
    const MiFont *fxCands[] = {F_SMALL(), F_TINY()};
    int fxFont = pickFitFont(fxCands, 2, fxValue, 112);
    panelDrawTextRight(fxCands[fxFont], fxValue, R, 124);

    panelDrawText(F_TINY(), "USD/oz", L, 149);
    panelDrawText(F_TINY(), "较北京00:00", 96, 149);
    panelDrawText(F_TINY(), "CNY/g", CNY_X, 149);
    const char *cnyKind = gold["baseline_cny_kind"] | "";
    time_t cnyBaseAt = (time_t)(long long)(gold["baseline_cny_price_as_of"] |
        0LL);
    bool cnyToday = quoteToday && strcmp(cnyKind, "first_valid") == 0 &&
                    sameLocalDate(now, cnyBaseAt);
    if (!cnyToday) cnyDelta = NAN;
    fmtSignedDelta(cnyDelta, delta, sizeof(delta));
    drawGoldPriceDelta(cnyValue, delta, CNY_X, 620, 124);
    char cnyNote[32];
    if (cnyToday) {
        fmtHm(cnyBaseAt, hm, sizeof(hm));
        snprintf(cnyNote, sizeof(cnyNote), "较今日 %s", hm);
    } else {
        snprintf(cnyNote, sizeof(cnyNote), "基准待建立");
    }
    panelDrawText(F_TINY(), cnyNote, CNY_X + 64, 149);
    panelDrawTextRight(F_TINY(), "USD/CNY", R, 149);

    char quoteText[40], baselineText[32];
    if (quoteToday) {
        fmtHm(priceAt, hm, sizeof(hm));
        snprintf(quoteText, sizeof(quoteText), "报价 %s", hm);
    } else if (timeTrusted(priceAt)) {
        char mdhm[24];
        fmtMdHm(priceAt, mdhm, sizeof(mdhm));
        snprintf(quoteText, sizeof(quoteText), "报价 %s", mdhm);
    } else snprintf(quoteText, sizeof(quoteText), "报价 --:--");
    panelDrawText(F_TINY(), quoteText, L, 169);
    if (cnyToday) {
        fmtHm(cnyBaseAt, hm, sizeof(hm));
        snprintf(baselineText, sizeof(baselineText), "首笔 %s", hm);
    } else snprintf(baselineText, sizeof(baselineText), "首笔 --:--");
    panelDrawText(F_TINY(), baselineText, CNY_X, 169);
    bool fxCached = (gold["fx_stale"] | false) || goldStale;
    panelDrawTextRight(F_TINY(), fxCached ? "汇率缓存" : "参考汇率", R, 169);
    panelHLine(L, R, 174, 2);

    // 资讯中部（三级兼容：digest 段落版 → brief items 三条 → 旧三分类）
    const char *newsMode = news["mode"] | "";
    bool isDigest = (strcmp(newsMode, "digest") == 0 ||
                     strcmp(newsMode, "daily_message") == 0 ||
                     strcmp(newsMode, "status") == 0);
    if (isDigest) {
        const char *dtxt = news["text"] | "";
        if (dtxt[0]) {
            const char *dper = news["period"] | "";
            const char *jname = news["issue_title"] | "";
            if (!jname[0]) {
                jname = "科技 / AI 早报";
                if (strcmp(dper, "noon") == 0) jname = "科技 / AI 中报";
                else if (strcmp(dper, "evening") == 0) jname = "科技 / AI 晚报";
            }
            panelDrawTextTrunc(F_TITLE24(), jname, L, 208, 560);
            long long gts = news["generated_at"].as<long long>();
            char issue[28];
            if (gts > 0) {
                time_t g2 = (time_t)gts;
                struct tm generated = {};
                time_t nowE = time(nullptr);
                struct tm current = {};
                bool generatedOk = localtime_r(&g2, &generated) && generated.tm_year >= 123;
                bool currentOk = localtime_r(&nowE, &current) != nullptr;
                if (generatedOk && currentOk && generated.tm_year == current.tm_year &&
                    generated.tm_mon == current.tm_mon && generated.tm_mday == current.tm_mday)
                    snprintf(issue, sizeof(issue), "%02d:%02d 出刊", generated.tm_hour, generated.tm_min);
                else if (generatedOk)
                    snprintf(issue, sizeof(issue), "%02d-%02d %02d:%02d 出刊",
                             generated.tm_mon + 1, generated.tm_mday, generated.tm_hour, generated.tm_min);
                else snprintf(issue, sizeof(issue), "--:-- 出刊");
            } else snprintf(issue, sizeof(issue), "--:-- 出刊");
            panelDrawTextRight(F_TINY(), issue, R, 208);        // 出刊标识 Reg16 右缘 772 y208
            // 正文：发布端已按可见边界折行（≤4 行、无行首闭合标点、行 ink ≤744）；此处逐行绘制
            const int bodyBase[4] = {241, 273, 305, 337};
            const char *p = dtxt;
            int li = 0;
            while (*p && li < 4) {
                const char *nl = strchr(p, '\n');
                int len = nl ? (int)(nl - p) : (int)strlen(p);
                char ln[128];
                if (len >= (int)sizeof(ln)) len = (int)sizeof(ln) - 1;
                memcpy(ln, p, (size_t)len);
                ln[len] = 0;
                if (ln[0]) panelDrawText(F_SMALL(), ln, L, bodyBase[li]);
                li++;
                p = nl ? (nl + 1) : (p + strlen(p));
            }
        }
        // digest 无正文：中部留空（不显示旧内容冒充；页脚状态提示缺失）
    } else {
        JsonArrayConst briefs = news["items"].as<JsonArrayConst>();
        bool hasBriefs = !briefs.isNull();
        static const char *legacyKeys[3] = {"general", "tech_ai", "finance"};
        static const char *legacyTags[3] = {"综合", "科技AI", "财经"};
        int rowBase[3] = {216, 270, 324};
        for (int i = 0; i < 3; i++) {
            const char *tag = "";
            const char *text = "";
            if (hasBriefs && i < (int)briefs.size()) {
                JsonObjectConst it = briefs[i].as<JsonObjectConst>();
                if (!it.isNull()) {
                    tag = it["tag"] | "";
                    text = it["text"] | "";
                }
            } else if (!hasBriefs) {
                JsonObjectConst old = news[legacyKeys[i]].as<JsonObjectConst>();
                tag = legacyTags[i];
                if (!old.isNull()) text = old["title"] | "";
            }
            if (!text[0]) continue;  // 缺数据行留空
            panelDrawText(F_SMALL(), tag, L, rowBase[i]);
            panelDrawTextTrunc(F_SMALL(), text, 136, rowBase[i], R - 136);
        }
    }

    // 底部全宽双行摘要；两行在 y350..427 之间按字形高度光学均分。
    panelHLine(L, R, 350, 2);
    JsonObjectConst ai = screen["pages"]["ai"].as<JsonObjectConst>();
    double used = jnum(ai["codex_7d_used"], -1);
    bool usedOk = (used >= 0 && used <= 100.0);   // 未知/非数值/越界 → “剩余 --%”且不绘误导比例
    char line[200];
    panelDrawText(F_TITLE24(), "CODEX", L, 381);
    if (usedOk) snprintf(line, sizeof(line), "剩余 %.0f%%", 100.0 - used);
    else snprintf(line, sizeof(line), "剩余 --%%");
    panelDrawText(F_LABEL(), line, 210, 381);
    long long ra = ai["codex_resets_at"].as<long long>();
    if (ra > 0) {
        char md[24];
        fmtMdHm((time_t)ra, md, sizeof(md));
        snprintf(line, sizeof(line), "7日 · 重置 %s", md);
    } else snprintf(line, sizeof(line), "7日 · 重置 --");
    panelDrawTextRight(F_SMALL(), line, R, 381);

    panelDrawText(F_TITLE24(), "DEEPSEEK", L, 414);
    double cny = jnum(ai["deepseek"]["CNY"], NAN);
    char mc[32];
    fmtMoney(cny, mc, sizeof(mc));
    snprintf(line, sizeof(line), "%s CNY", mc);
    panelDrawText(F_LABEL(), line, 210, 414);
    char todayTxt[32];
    fmtCompactTodayTokens(ai, todayTxt, sizeof(todayTxt));
    snprintf(line, sizeof(line), "今日 Token · %s", todayTxt);
    int amountRight = 210 + panelTextInkWidth(F_LABEL(), mc) + panelTextInkWidth(F_LABEL(), " CNY");
    if (R - panelTextInkWidth(F_SMALL(), line) - amountRight >= 16)
        panelDrawTextRight(F_SMALL(), line, R, 414);
    else
        panelDrawTextRight(F_TINY(), line, R, 414);

    // 与 AI 用量页共用用户确认版页眉、页脚和左右边界。
    panelHLine(PUB_L, PUB_R, PUB_FTR_Y, 2);
    int miss = 0;
    const char *st = pageStatusText(doc, "news_gold", &miss);
    fmtUpdText(line, sizeof(line), st, payloadTsOf(doc));
    panelDrawText(F_SMALL(), line, PUB_L, PUB_FOOT_Y);
    panelDrawTextRight(F_SMALL(), "INKSIGHT", PUB_R, PUB_FOOT_Y);
    return true;
}

#endif // ENABLE_STRUCTURED
