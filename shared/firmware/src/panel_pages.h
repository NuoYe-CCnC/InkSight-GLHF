// panel_pages.h — 双面板页面渲染接口（第二阶段）
#ifndef INKSIGHT_PANEL_PAGES_H
#define INKSIGHT_PANEL_PAGES_H

#include <Arduino.h>
#include <ArduinoJson.h>

#ifdef ENABLE_STRUCTURED

// 返回 true 表示已渲染到 imgBuf
bool renderAiPanel(const JsonDocument &doc);
bool renderNewsGoldPanel(const JsonDocument &doc);

// 页面选择：0=AI 面板（默认），1=资讯+金价。测试入口默认关闭；
// 编译期 -DPANEL_FORCE_PAGE=n 可强制某页（无读图 golden 用）。
int  panelCurrentPage();
void panelSetPage(int page);

#endif
#endif
