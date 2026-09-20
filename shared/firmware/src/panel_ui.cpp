// panel_ui.cpp — MiSans 双面板绘制引擎（第二阶段）
// 画布：imgBuf（1=白 0=黑，MSB-first，800x480）。基线(baseline)为文字 y 锚点。
#include "panel_ui.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "config.h"
#include "display.h"   // extern uint8_t imgBuf[] 经 config.h

#if ENABLE_STRUCTURED

extern uint8_t imgBuf[];

static int PW = 800, PH = 480;

void panelSetSize(int w, int h) { PW = w; PH = h; }

void panelClear() {
    if (imgBuf) memset(imgBuf, 0xFF, (size_t)PW * PH / 8);
}

void panelFillRect(int x, int y, int w, int h) {
    for (int py = y; py < y + h; py++) {
        if (py < 0 || py >= PH) continue;
        for (int px = x; px < x + w; px++) {
            if (px < 0 || px >= PW) continue;
            imgBuf[py * (PW / 8) + px / 8] &= ~(0x80 >> (px % 8));
        }
    }
}

void panelHLine(int x0, int x1, int y, int thickness) {
    if (x1 < x0) { int t = x0; x0 = x1; x1 = t; }
    panelFillRect(x0, y, x1 - x0 + 1, thickness);
}

void panelVLine(int x, int y0, int y1, int thickness) {
    if (y1 < y0) { int t = y0; y0 = y1; y1 = t; }
    panelFillRect(x, y0, thickness, y1 - y0 + 1);
}

void panelRectOutline(int x, int y, int w, int h, int thickness) {
    panelHLine(x, x + w - 1, y, thickness);
    panelHLine(x, x + w - 1, y + h - thickness, thickness);
    panelVLine(x, y, y + h - 1, thickness);
    panelVLine(x + w - thickness, y, y + h - 1, thickness);
}

// ── 字形引擎 ────────────────────────────────────────────────
static int s_missingLogged = 0;

int32_t panelGlyphIndex(const MiFont *f, uint32_t cp) {
    int32_t lo = 0, hi = f->count - 1;
    while (lo <= hi) {
        int32_t mid = (lo + hi) / 2;
        uint32_t v = pgm_read_dword(&f->unicode[mid]);
        if (v == cp) return mid;
        if (v < cp) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
}

void panelFontMissingLog(uint32_t cp) {
    if (s_missingLogged < 20) {
        Serial.printf("[FONT] missing glyph U+%04X -> replacement policy\n", (unsigned)cp);
        s_missingLogged++;
    }
}

// 绘制单字符：pen 起点 x，基线 y。返回前进量(px,1/64)。
static int64_t blitGlyph(const MiFont *f, int32_t idx, int penX, int baseY) {
    int x0px = (int16_t)pgm_read_word(&f->x0[idx]);
    int topPx = (int16_t)pgm_read_word(&f->top[idx]);
    int iw = pgm_read_word(&f->iw[idx]);
    int ih = pgm_read_word(&f->ih[idx]);
    uint32_t off = pgm_read_dword(&f->off[idx]);
    if (iw <= 0 || ih <= 0) return (int64_t)pgm_read_word(&f->adv[idx]);
    int rowBytes = (iw + 7) / 8;
    int startX = penX + x0px;
    int startY = baseY - topPx;
    for (int r = 0; r < ih; r++) {
        int yy = startY + r;
        if (yy < 0 || yy >= PH) continue;
        const uint8_t *row = &f->data[off + (uint32_t)r * rowBytes];
        for (int c = 0; c < iw; c++) {
            int xx = startX + c;
            if (xx < 0 || xx >= PW) continue;
            if (!(pgm_read_byte(&row[c / 8]) & (0x80 >> (c % 8)))) {
                imgBuf[yy * (PW / 8) + xx / 8] &= ~(0x80 >> (xx % 8));  // 黑
            }
        }
    }
    return (int64_t)pgm_read_word(&f->adv[idx]);
}

// 测量字符串像素宽（advance/64 求和，取整进 1px 精确到 1/64 返回 *64）
static int64_t measurePx64(const MiFont *f, const char *s) {
    int64_t w = 0;
    while (*s) {
        uint32_t cp = utf8decode(&s);
        if (cp == 0xFFFD) { w += 12 * 64; continue; }
        int32_t idx = panelGlyphIndex(f, cp);
        if (idx < 0) { panelFontMissingLog(cp); idx = panelGlyphIndex(f, 0x25A0); }
        w += (idx >= 0) ? (int64_t)pgm_read_word(&f->adv[idx]) : 12 * 64;
    }
    return w;
}

int panelTextWidth(const MiFont *f, const char *s) {
    return (int)((measurePx64(f, s) + 31) / 32);  // 四舍五入到半px 保守
}

// 最后一个有墨字形 ink 右缘（相对起点，1/64px）：
// 右对齐与间距检查以此为准（advance 排布 + 每字形 x0/iw + 字形间前进量）。
int64_t panelTextInkRight64(const MiFont *f, const char *s) {
    int64_t pen = 0;
    int64_t right = 0;
    while (*s) {
        uint32_t cp = utf8decode(&s);
        if (cp == 0xFFFD) { pen += 12 * 64; continue; }
        int32_t idx = panelGlyphIndex(f, cp);
        if (idx < 0) {
            panelFontMissingLog(cp);
            idx = panelGlyphIndex(f, 0x25A0);
        }
        if (idx < 0) { pen += 12 * 64; continue; }
        int iw = pgm_read_word(&f->iw[idx]);
        if (iw > 0) {
            int x0px = (int16_t)pgm_read_word(&f->x0[idx]);
            right = pen + ((int64_t)x0px + iw) * 64;
        }
        pen += (int64_t)pgm_read_word(&f->adv[idx]);
    }
    return right;
}

int panelTextInkWidth(const MiFont *f, const char *s) {
    return (int)((panelTextInkRight64(f, s) + 63) / 64);
}

int panelTextVisibleLeftOffset(const MiFont *f, const char *s) {
    int64_t pen = 0;
    int left = INT_MAX;
    while (*s) {
        uint32_t cp = utf8decode(&s);
        if (cp == 0xFFFD) { pen += 12 * 64; continue; }
        int32_t idx = panelGlyphIndex(f, cp);
        if (idx < 0) idx = panelGlyphIndex(f, 0x25A0);
        if (idx < 0) { pen += 12 * 64; continue; }
        int x0px = (int16_t)pgm_read_word(&f->x0[idx]);
        int iw = pgm_read_word(&f->iw[idx]);
        int ih = pgm_read_word(&f->ih[idx]);
        uint32_t off = pgm_read_dword(&f->off[idx]);
        int rowBytes = (iw + 7) / 8;
        for (int row = 0; row < ih; row++) {
            const uint8_t *bits = &f->data[off + (uint32_t)row * rowBytes];
            for (int col = 0; col < iw; col++) {
                if (!(pgm_read_byte(&bits[col / 8]) & (0x80 >> (col % 8)))) {
                    int actual = (int)(pen / 64) + x0px + col;
                    if (actual < left) left = actual;
                    break;
                }
            }
        }
        pen += (int64_t)pgm_read_word(&f->adv[idx]);
    }
    return left == INT_MAX ? 0 : left;
}

void panelDrawText(const MiFont *f, const char *s, int x, int baseY) {
    int64_t pen = (int64_t)x * 64;
    while (*s) {
        uint32_t cp = utf8decode(&s);
        if (cp == 0xFFFD) { pen += 12 * 64; continue; }
        int32_t idx = panelGlyphIndex(f, cp);
        if (idx < 0) {
            panelFontMissingLog(cp);
            idx = panelGlyphIndex(f, 0x25A0);
            if (idx < 0) { pen += 12 * 64; continue; }
        }
        pen += blitGlyph(f, idx, (int)(pen / 64), baseY);
    }
}

// 右对齐：文本最后字形 ink 右缘落在 rightX（而非 advance 右缘，避免可见留白）
void panelDrawTextRight(const MiFont *f, const char *s, int rightX, int baseY) {
    panelDrawText(f, s, rightX - panelTextInkWidth(f, s), baseY);
}

// 截断到 maxW 像素并加省略号；返回是否截断。
// 宽度测量统一用 ink 可见边界口径（panelTextInkWidth，与右对齐一致），
// 避免 adv 口径在设备端出现偏差导致截断过短（2026-09-07 修复）。
bool panelDrawTextTrunc(const MiFont *f, const char *s, int x, int baseY, int maxW) {
    if (panelTextInkWidth(f, s) <= maxW) { panelDrawText(f, s, x, baseY); return false; }
    int ellW = panelTextInkWidth(f, "…");
    if (ellW < 0 || ellW > maxW) ellW = maxW;
    // 缓冲须容纳整行可放字（636px/16px 摘要 ~39 字 × 3B + 省略号）
    char tmp[192] = {0};
    const char *p = s;
    while (*p) {
        const char *save = p;
        uint32_t cp = utf8decode(&p);
        if (cp == 0xFFFD) continue;
        int n = (int)(p - save);
        if ((int)strlen(tmp) + n >= (int)sizeof(tmp) - 4) break;
        memcpy(tmp + strlen(tmp), save, (size_t)n);
        tmp[strlen(tmp) + n] = 0;
        if (panelTextInkWidth(f, tmp) > maxW - ellW) {
            tmp[strlen(tmp) - n] = 0;   // 回退该字
            break;
        }
    }
    strcat(tmp, "…");
    panelDrawText(f, tmp, x, baseY);
    return true;
}

uint32_t utf8decode(const char **sp) {
    const uint8_t *s = (const uint8_t *)*sp;
    uint8_t c = s[0];
    if (c < 0x80) { (*sp) += 1; return c; }
    if ((c & 0xE0) == 0xC0 && s[1]) { (*sp) += 2; return ((c & 0x1F) << 6) | (s[1] & 0x3F); }
    if ((c & 0xF0) == 0xE0 && s[1] && s[2]) { (*sp) += 3; return ((c & 0x0F) << 12) | ((s[1] & 0x3F) << 6) | (s[2] & 0x3F); }
    (*sp) += 1;
    return 0xFFFD;
}

#endif // ENABLE_STRUCTURED
