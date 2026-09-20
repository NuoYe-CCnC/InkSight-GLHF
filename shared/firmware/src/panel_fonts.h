// panel_fonts.h — MiSans 度量字库聚合（第二阶段双面板）
// 每个字形：unicode / advance(1/64px) / x0(左bearing px) / inkTop(基线以上px)
//          / iw / ih / 数据偏移(32位) ；位约定 1=白 0=黑，MSB-first（ink 行裁剪）
#ifndef INKSIGHT_PANEL_FONTS_H
#define INKSIGHT_PANEL_FONTS_H

#include <stdint.h>

#include "fonts_gen/misans_reg_16.h"
#include "fonts_gen/misans_reg_21.h"
#include "fonts_gen/misans_reg_24.h"
#include "fonts_gen/misans_reg_30.h"
#include "fonts_gen/misans_reg_62.h"
#include "fonts_gen/misans_reg_82.h"
#include "fonts_gen/misans_bold_24.h"
#include "fonts_gen/misans_bold_30.h"
#include "fonts_gen/misans_bold_62.h"
#include "fonts_gen/misans_bold_82.h"

typedef struct {
    const uint32_t *unicode;
    const uint16_t *adv;    // 1/64 px
    const int16_t  *x0;
    const int16_t  *top;    // baseline 到 ink 顶（负=超出基线以上/以下）
    const uint16_t *iw;
    const uint16_t *ih;
    const uint32_t *off;
    const uint8_t  *data;
    int count;
    const char    *name;
} MiFont;

#define _MF(name) { font_##name##_unicode, font_##name##_adv, font_##name##_x0, \
    font_##name##_top, font_##name##_iw, font_##name##_ih, font_##name##_off, \
    font_##name##_data, (int)(sizeof(font_##name##_unicode) / sizeof(uint32_t)), #name }

enum {
    MF_REG_16 = 0, MF_REG_21, MF_REG_24, MF_REG_30, MF_REG_62, MF_REG_82,
    MF_BOLD_24, MF_BOLD_30, MF_BOLD_62, MF_BOLD_82, MF_COUNT
};

static const MiFont kFonts[MF_COUNT] = {
    _MF(misans_reg_16), _MF(misans_reg_21), _MF(misans_reg_24), _MF(misans_reg_30),
    _MF(misans_reg_62), _MF(misans_reg_82),
    _MF(misans_bold_24), _MF(misans_bold_30), _MF(misans_bold_62), _MF(misans_bold_82),
};

// 缺字替换策略：记录前 N 个缺失（一次性日志）；渲染时用 '□'(若缺则空格)。
extern void panelFontMissingLog(uint32_t cp);
extern int32_t panelGlyphIndex(const MiFont *f, uint32_t cp);

#endif
