// layout.cpp — 设备端白名单 widget/模块执行器（结构化渲染 v1.1）
// 位约定与 imgBuf 一致：1=白，0=黑；每字节 MSB 为左侧像素。
// v1.1：支持 modules（模块+区域定位，契约 §1.5.2）、bbox 记录、delta 模块局刷、payload_id。
#include "layout.h"
#include "config.h"
#include "display.h"
#include "epd_driver.h"

#ifndef ENABLE_STRUCTURED
bool layoutRenderFromJson(JsonDocument &doc) { (void)doc; return false; }
bool layoutRenderDelta(JsonDocument &doc) { (void)doc; return false; }
bool layoutGetPayloadId(JsonDocument &doc, char *out, size_t cap) { (void)doc; (void)out; (void)cap; return false; }
#else

#include "fonts_misans_16.h"
#include "fonts_misans_24.h"
#include <time.h>
#include <esp32-hal-psram.h>

static const int LAYOUT_MARGIN = 16;

// ── 渲染缓冲间接层（全屏/模块临时缓冲通用）────────────────────
static uint8_t *g_buf = nullptr;
static int g_bufW = 0;
static int g_rgnX = 0, g_rgnY = 0, g_rgnW = 0, g_rgnH = 0;

static void setBlack(int x, int y) {
    int rowBytes = (g_bufW + 7) / 8;
    if (x < 0 || x >= g_bufW || y < 0 || y >= H) return;
    g_buf[y * rowBytes + x / 8] &= ~(0x80 >> (x % 8));
}

// ── Unicode 点阵绘制（MiSans）──────────────────────────────
static void drawCJK(uint32_t cp, int size, int x, int y) {
    int off;
    if (size == 24) {
        off = font_misans_24_lookup(cp);
        if (off < 0) return;
        for (int row = 0; row < 24; row++)
            for (int col = 0; col < 24; col++)
                if (!(pgm_read_byte(&font_misans_24_data[off + row * 3 + col / 8]) & (0x80 >> (col % 8))))
                    setBlack(x + col, y + row);
    } else {
        off = font_misans_16_lookup(cp);
        if (off < 0) return;
        for (int row = 0; row < 16; row++)
            for (int col = 0; col < 16; col++)
                if (!(pgm_read_byte(&font_misans_16_data[off + row * 2 + col / 8]) & (0x80 >> (col % 8))))
                    setBlack(x + col, y + row);
    }
}

static int32_t utf8Next(const uint8_t *s, size_t len, size_t &i) {
    if (i >= len) return -1;
    uint8_t c = s[i];
    if (c < 0x80) { i++; return c; }
    if ((c & 0xE0) == 0xC0 && i + 1 < len) { i += 2; return ((c & 0x1F) << 6) | (s[i - 1] & 0x3F); }
    if ((c & 0xF0) == 0xE0 && i + 2 < len) { i += 3; return ((c & 0x0F) << 12) | ((s[i - 2] & 0x3F) << 6) | (s[i - 1] & 0x3F); }
    i++;
    return -2;
}

static int glyphW(uint32_t cp, int size) {
    if (cp < 0x80) return size * 3 / 4; // ASCII：6*scale（5x7 每字符前进，scale=size/8）
    return size;
}

static void drawUTF8Line(const String &text, int x, int y, int size) {
    const uint8_t *s = (const uint8_t *)text.c_str();
    size_t len = text.length();
    size_t i = 0;
    int cx = x;
    int asciiScale = size / 8;
    if (asciiScale < 1) asciiScale = 1;
    while (true) {
        int32_t cp = utf8Next(s, len, i);
        if (cp == -1) break;
        if (cp == -2) continue;
        if (cp < 0x80) {
            char ch = (char)cp;
            drawText(&ch, cx, y, asciiScale);
        } else {
            drawCJK((uint32_t)cp, size, cx, y);
        }
        cx += glyphW(cp, size);
    }
}

static int measureUTF8Line(const String &text, int size) {
    const uint8_t *s = (const uint8_t *)text.c_str();
    size_t len = text.length();
    size_t i = 0;
    int w = 0;
    while (true) {
        int32_t cp = utf8Next(s, len, i);
        if (cp == -1) break;
        if (cp == -2) continue;
        w += glyphW(cp, size);
    }
    return w;
}

// ── 文本块（断行 / max_lines / 省略号 / 对齐）──────────────
static void drawTextBlock(const JsonObject &w, int &y) {
    String text = w["text"] | "";
    int size = 16;
    String font = w["font"] | "";
    if (font.indexOf("24") >= 0) size = 24;
    int maxLines = w["max_lines"] | 3;
    String align = w["align"] | "left";
    int maxW = g_rgnW - LAYOUT_MARGIN * 2;
    int lineH = size + 6;

    const uint8_t *s = (const uint8_t *)text.c_str();
    size_t len = text.length();
    size_t i = 0;
    int lineNo = 0;
    String line = "";
    while (lineNo < maxLines) {
        bool eol = false;
        while (true) {
            size_t mark = i;
            int32_t cp = utf8Next(s, len, i);
            if (cp == -1) { eol = true; break; }
            if (cp == -2) continue;
            if (cp == '\n' || cp == '\r') { eol = true; break; }
            String cand = line + String((char *)s + mark, i - mark);
            if (measureUTF8Line(cand, size) > maxW) {
                i = mark;
                if (line.length() == 0) {
                    cand = line + String((char *)s + mark, i - mark);
                    line = cand;
                    i = len;
                }
                eol = true;
                break;
            }
            line = cand;
        }
        if (line.length() > 0) {
            String out = line;
            if (eol && lineNo == maxLines - 1 && i < len && !line.endsWith("…"))
                out = line + "…";
            int lw = measureUTF8Line(out, size);
            int x = g_rgnX + LAYOUT_MARGIN;
            if (align == "center") x = g_rgnX + (g_rgnW - lw) / 2;
            else if (align == "right") x = g_rgnX + g_rgnW - LAYOUT_MARGIN - lw;
            drawUTF8Line(out, x, y, size);
            lineNo++;
            y += lineH;
        }
        line = "";
        if (i >= len || eol && i >= len) break;
    }
}

static void drawBigNumber(const JsonObject &w, int &y) {
    String text = w["text"] | "";
    String unit = w["unit"] | "";
    int scale = 9;
    int charW = 6 * scale;
    int wPix = text.length() * charW + (unit.length() > 0 ? (measureUTF8Line(unit, 16) + 8) : 0);
    int x = g_rgnX + (g_rgnW - wPix) / 2;
    if (x < g_rgnX + LAYOUT_MARGIN) x = g_rgnX + LAYOUT_MARGIN;
    drawUTF8Line(text, x, y, 8 * scale); // ASCII 数字放大（5x7 scale=9）
    if (unit.length() > 0) drawUTF8Line(unit, x + text.length() * charW + 8, y + 5 * scale, 16);
    y += 7 * scale + 12;
}

static void drawSeparator(const JsonObject &w, int &y) {
    int x0 = g_rgnX + LAYOUT_MARGIN, x1 = g_rgnX + g_rgnW - LAYOUT_MARGIN;
    for (int x = x0; x < x1; x++) setBlack(x, y);
    y += 6;
}

static void drawIconText(const JsonObject &w, int &y) {
    int box = 24;
    int x = g_rgnX + LAYOUT_MARGIN;
    for (int dx = 0; dx < box; dx++) { setBlack(x + dx, y); setBlack(x + dx, y + box - 1); }
    for (int dy = 0; dy < box; dy++) { setBlack(x, y + dy); setBlack(x + box - 1, y + dy); }
    drawUTF8Line(w["text"] | "", x + box + 8, y + 4, 16);
    y += box + 8;
}

static void drawForecastRow(const JsonObject &w, int &y) {
    JsonArray items = w["items"].as<JsonArray>();
    int n = items.size();
    if (n <= 0) return;
    int cardW = (g_rgnW - LAYOUT_MARGIN * 2) / n;
    int x = g_rgnX + LAYOUT_MARGIN;
    for (JsonVariant it : items) {
        JsonObject day = it.as<JsonObject>();
        String d = day["day"] | "";
        String hi = day["hi"] | "--";
        String lo = day["lo"] | "--";
        drawUTF8Line(d, x + (cardW - measureUTF8Line(d, 16)) / 2, y, 16);
        String range = hi + "/" + lo;
        drawUTF8Line(range, x + (cardW - measureUTF8Line(range, 16)) / 2, y + 20, 16);
        x += cardW;
    }
    y += 44;
}

static void drawProgress(const JsonObject &w, int &y) {
    float pct = w["percent"] | 0.0f;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    String label = w["label"] | "";
    int barH = 10;
    int barW = g_rgnW - LAYOUT_MARGIN * 2;
    int x0 = g_rgnX + LAYOUT_MARGIN;
    int fillW = (int)(barW * pct / 100.0f);
    if (label.length() > 0) {
        int lw = measureUTF8Line(label, 16);
        drawUTF8Line(label, g_rgnX + (g_rgnW - lw) / 2, y, 16);
        y += 22;
    }
    for (int dy = 0; dy < barH; dy++)
        for (int dx = 0; dx < barW; dx++)
            if (dx < fillW) setBlack(x0 + dx, y + dy);
    y += barH + 10;
}

// ── widget 分发（模块/流式共用；region 上下文由调用方设置）────
static void drawWidget(const JsonObject &w, int &y, int rgnBottom) {
    String type = w["type"] | "";
    if (type == "text") drawTextBlock(w, y);
    else if (type == "big_number") drawBigNumber(w, y);
    else if (type == "separator") drawSeparator(w, y);
    else if (type == "spacer") y += (w["height"] | 6);
    else if (type == "icon_text") drawIconText(w, y);
    else if (type == "forecast_row") drawForecastRow(w, y);
    else if (type == "progress") drawProgress(w, y);
    else if (type == "footer") {
        String label = w["label"] | "";
        if (label.length() > 0)
            drawUTF8Line(label, g_rgnX + (g_rgnW - measureUTF8Line(label, 16)) / 2, H - 40, 16);
    }
    if (y > rgnBottom) y = rgnBottom; // 区域截断
}

// ── 全屏流式渲染（v1.0 兼容）───────────────────────────────
bool layoutRenderFromJson(JsonDocument &doc) {
    JsonObject screen = doc["screen"];
    if (screen.isNull()) return false;
    JsonArray widgets = screen["widgets"];
    if (widgets.isNull()) return false;

    g_buf = imgBuf;
    g_bufW = W;
    g_rgnX = 0; g_rgnY = 0; g_rgnW = W; g_rgnH = H;
    memset(imgBuf, 0xFF, IMG_BUF_LEN);
    int y = 20;
    for (JsonVariant v : widgets) {
        drawWidget(v.as<JsonObject>(), y, H - 20);
        if (y >= H - 20) break;
    }
    return true;
}

// ── B 版 AI_USAGE 专用布局 ─────────────────────────────────

static void fillBlackRect(int x, int y, int w, int h) {
    if (w <= 0 || h <= 0) return;
    for (int py = y; py < y + h; py++)
        for (int px = x; px < x + w; px++)
            setBlack(px, py);
}

static void drawHLine(int x0, int x1, int y, int thickness = 3) {
    fillBlackRect(x0, y, x1 - x0, thickness);
}
static void drawVLine(int x, int y0, int y1, int thickness = 3) {
    fillBlackRect(x, y0, thickness, y1 - y0);
}
static void drawRectOutline(int x, int y, int w, int h, int thickness = 3) {
    fillBlackRect(x, y, w, thickness);
    fillBlackRect(x, y + h - thickness, w, thickness);
    fillBlackRect(x, y, thickness, h);
    fillBlackRect(x + w - thickness, y, thickness, h);
}
static void drawCenteredUTF8(const String &value, int centerX, int y, int size) {
    int width = measureUTF8Line(value, size);
    drawUTF8Line(value, centerX - width / 2, y, size);
}
static void drawRightUTF8(const String &value, int rightX, int y, int size) {
    int width = measureUTF8Line(value, size);
    drawUTF8Line(value, rightX - width, y, size);
}

static JsonObject findWidgetById(JsonDocument &doc, const char *wantedId) {
    JsonArray modules = doc["screen"]["modules"].as<JsonArray>();
    for (JsonVariant moduleValue : modules) {
        JsonArray widgets = moduleValue["widgets"].as<JsonArray>();
        for (JsonVariant widgetValue : widgets) {
            JsonObject widget = widgetValue.as<JsonObject>();
            const char *id = widget["id"] | "";
            if (id && strcmp(id, wantedId) == 0) return widget;
        }
    }
    return JsonObject();
}

static String widgetText(JsonDocument &doc, const char *id, const char *fallback = "--") {
    JsonObject widget = findWidgetById(doc, id);
    if (widget.isNull()) return String(fallback);
    return String(widget["text"] | fallback);
}

static float widgetPercent(JsonDocument &doc, const char *id, float fallback = 0.0f) {
    JsonObject widget = findWidgetById(doc, id);
    if (widget.isNull()) return fallback;
    float value = widget["percent"] | fallback;
    if (value < 0.0f) value = 0.0f;
    if (value > 100.0f) value = 100.0f;
    return value;
}

static String extractResetTime(const String &source) {
    int position = source.indexOf("重置");
    if (position < 0) return "--";
    String result = source.substring(position + 6);
    result.trim();
    return result.length() > 0 ? result : "--";
}

static String currentPayloadTime(JsonDocument &doc) {
    time_t timestamp = (time_t)(doc["ts"] | 0);
    if (timestamp <= 0) return "--:--";
    struct tm localTime;
    localtime_r(&timestamp, &localTime);
    char buffer[8];
    snprintf(buffer, sizeof(buffer), "%02d:%02d", localTime.tm_hour, localTime.tm_min);
    return String(buffer);
}

static void drawBProgressBar(int x, int y, int width, int height, float usedPercent) {
    const int border = 4;
    if (usedPercent < 0.0f) usedPercent = 0.0f;
    if (usedPercent > 100.0f) usedPercent = 100.0f;
    drawRectOutline(x, y, width, height, border);
    int innerWidth = width - border * 2;
    int innerHeight = height - border * 2;
    int fillWidth = (int)(innerWidth * usedPercent / 100.0f);
    if (fillWidth > 0) fillBlackRect(x + border, y + border, fillWidth, innerHeight);
}

static bool isAiUsagePayload(JsonDocument &doc) {
    return !findWidgetById(doc, "ds_balance").isNull() &&
           !findWidgetById(doc, "cx_remaining").isNull();
}

static bool layoutRenderAiUsageB(JsonDocument &doc) {
    g_buf = imgBuf;
    g_bufW = W;
    g_rgnX = 0; g_rgnY = 0; g_rgnW = W; g_rgnH = H;
    memset(imgBuf, 0xFF, IMG_BUF_LEN);

    String deepSeekBalance = widgetText(doc, "ds_balance", "--");
    JsonObject deepSeekWidget = findWidgetById(doc, "ds_balance");
    String currency = deepSeekWidget.isNull() ? "CNY"
                      : String(deepSeekWidget["unit"] | "CNY");

    String codexRemaining = widgetText(doc, "cx_remaining", "--");
    JsonObject codexWidget = findWidgetById(doc, "cx_remaining");
    String codexUnit = codexWidget.isNull() ? "%"
                      : String(codexWidget["unit"] | "%");

    float usedPercent = widgetPercent(doc, "cx_0", 0.0f);
    String codexDescription = widgetText(doc, "cx_cap", "");
    String resetTime = extractResetTime(codexDescription);
    String updateTime = currentPayloadTime(doc);
    String remainingDisplay = codexRemaining + codexUnit;
    String usedText = "7 日已用 " + String((int)(usedPercent + 0.5f)) + "%";

    const int outerLeft = 28, outerRight = 772;
    const int headerLineY = 68, centerLineX = 400;
    const int columnsTop = 88, columnsBottom = 410, footerLineY = 425;
    const int leftCenterX = 200, rightCenterX = 600;

    drawUTF8Line("AI 用量", outerLeft, 20, 24);
    drawRightUTF8(updateTime, outerRight, 27, 24);
    drawHLine(outerLeft, outerRight, headerLineY, 4);

    drawVLine(centerLineX, columnsTop, columnsBottom, 4);

    drawUTF8Line("DEEPSEEK", 44, 91, 24);
    drawCenteredUTF8(deepSeekBalance, leftCenterX, 145, 80);
    drawCenteredUTF8(currency, leftCenterX, 255, 24);
    drawCenteredUTF8("可用余额", leftCenterX, 313, 24);

    drawUTF8Line("CODEX", 424, 91, 24);
    drawCenteredUTF8(remainingDisplay, rightCenterX, 145, 80);
    drawCenteredUTF8("剩余", rightCenterX, 255, 24);
    drawBProgressBar(440, 317, 320, 32, usedPercent);
    drawUTF8Line(usedText, 440, 369, 24);

    drawHLine(outerLeft, outerRight, footerLineY, 4);
    drawUTF8Line("下次重置", outerLeft, 443, 24);
    drawRightUTF8(resetTime, outerRight, 443, 24);

    Serial.println("[LAYOUT] AI_USAGE B layout");
    return true;
}

// ── 模块化渲染（v1.1）──────────────────────────────────────
static bool layoutRenderModules(JsonDocument &doc, bool delta) {
    JsonObject screen = doc["screen"];
    if (screen.isNull()) return false;
    JsonArray modules = screen["modules"];
    if (modules.isNull()) return false;

    g_buf = imgBuf;
    g_bufW = W;
    if (!delta) memset(imgBuf, 0xFF, IMG_BUF_LEN);

    for (JsonVariant mv : modules) {
        JsonObject mod = mv.as<JsonObject>();
        JsonObject region = mod["region"];
        JsonArray ws = mod["widgets"];
        if (region.isNull() || ws.isNull()) continue;
        int rx = region["x"] | 0;
        int ry = region["y"] | 0;
        int rw = region["w"] | W;
        int rh = region["h"] | H;
        if (rw <= 0 || rh <= 0) continue;

        if (delta) {
            // 模块级局刷：临时白底缓冲渲染该模块 → epdPartialDisplayWithOld → 回写 imgBuf
            int rowBytes = (g_bufW + 7) / 8;
            int regW = ((rw + 7) / 8) * 8;
            int regBytes = regW / 8 * rh;
            uint8_t *tmp = (uint8_t *)ps_malloc(regBytes);
            uint8_t *old = (uint8_t *)ps_malloc(regBytes);
            if (!tmp || !old) {
                if (tmp) free(tmp);
                if (old) free(old);
                continue;
            }
            g_buf = tmp;
            g_bufW = regW;
            memset(tmp, 0xFF, regBytes);
            g_rgnX = 0; g_rgnY = 0; g_rgnW = rw; g_rgnH = rh;
            int y = 0;
            for (JsonVariant wv : ws) {
                drawWidget(wv.as<JsonObject>(), y, rh);
                if (y >= rh) break;
            }
            // old = imgBuf 中对应区域
            for (int row = 0; row < rh; row++)
                memcpy(old + row * (regW / 8), imgBuf + (ry + row) * rowBytes + rx / 8, regW / 8);
            epdPartialDisplayWithOld(tmp, old, rx, ry, rx + regW, ry + rh);
            // 回写 imgBuf
            for (int row = 0; row < rh; row++)
                memcpy(imgBuf + (ry + row) * rowBytes + rx / 8, tmp + row * (regW / 8), regW / 8);
            free(tmp);
            free(old);
        } else {
            g_rgnX = rx; g_rgnY = ry; g_rgnW = rw; g_rgnH = rh;
            int y = ry + 4;
            for (JsonVariant wv : ws) {
                drawWidget(wv.as<JsonObject>(), y, ry + rh);
                if (y >= ry + rh) break;
            }
        }
    }
    g_buf = imgBuf;
    g_bufW = W;
    return true;
}

// 全屏渲染：payload 带 modules → 模块化全屏；否则兼容旧 widgets
bool layoutRenderFull(JsonDocument &doc) {
    JsonObject screen = doc["screen"];
    if (screen.isNull()) return false;
    // 同时存在 DeepSeek 和 Codex 数据时使用 B 版布局
    if (isAiUsagePayload(doc)) {
        return layoutRenderAiUsageB(doc);
    }
    // 其他结构化页面继续使用原来的渲染流程
    if (screen["modules"].is<JsonArray>()) {
        return layoutRenderModules(doc, false);
    }
    return layoutRenderFromJson(doc);
}

// delta：仅重绘 payload 中列出的模块（区域局刷，条与文字同区）
bool layoutRenderDelta(JsonDocument &doc) {
    return layoutRenderModules(doc, true);
}

// 提取 payload_id（稳定标识：值变化才变，用于"无变化跳过刷屏"）
bool layoutGetPayloadId(JsonDocument &doc, char *out, size_t cap) {
    const char *pid = doc["screen"]["payload_id"] | "";
    if (!pid[0] || cap == 0) return false;
    strncpy(out, pid, cap - 1);
    out[cap - 1] = 0;
    return true;
}

#endif // ENABLE_STRUCTURED
