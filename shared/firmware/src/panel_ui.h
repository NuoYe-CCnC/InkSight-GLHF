// panel_ui.h — MiSans 双面板绘制引擎接口
#ifndef INKSIGHT_PANEL_UI_H
#define INKSIGHT_PANEL_UI_H

#include <Arduino.h>
#include "panel_fonts.h"

#ifdef ENABLE_STRUCTURED

extern void panelSetSize(int w, int h);
extern void panelClear();
extern void panelFillRect(int x, int y, int w, int h);
extern void panelHLine(int x0, int x1, int y, int thickness);
extern void panelVLine(int x, int y0, int y1, int thickness);
extern void panelRectOutline(int x, int y, int w, int h, int thickness);

extern int  panelTextWidth(const MiFont *f, const char *s);
extern void panelDrawText(const MiFont *f, const char *s, int x, int baseY);
extern void panelDrawTextRight(const MiFont *f, const char *s, int rightX, int baseY);
extern bool panelDrawTextTrunc(const MiFont *f, const char *s, int x, int baseY, int maxW);
extern uint32_t utf8decode(const char **sp);

// 可见字形边界量宽（1/64px）：文本最后一个有墨字形 ink 右缘相对起点，
// 含 advance 排布、x0 偏移与最后字形 ink 宽度（右对齐/间距检查用真实右缘）。
extern int64_t panelTextInkRight64(const MiFont *f, const char *s);
// 像素版（向上取整到 1px）
extern int  panelTextInkWidth(const MiFont *f, const char *s);
// First actual black bitmap pixel relative to the pen origin. Used when two
// labels with different left bearings must share one optical left edge.
extern int  panelTextVisibleLeftOffset(const MiFont *f, const char *s);

#endif
#endif
