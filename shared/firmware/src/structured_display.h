#ifndef INKSIGHT_STRUCTURED_DISPLAY_H
#define INKSIGHT_STRUCTURED_DISPLAY_H

#include <Arduino.h>
#include <ArduinoJson.h>

// 尝试结构化链路：fetchStructured → 渲染。返回 true 表示链路成功（可能已显示或可显示）。
//   renderedModeIdOut : X-Mode-Id
//   pendingRefreshOut : X-Pending-Refresh（变化触发刷新标志）
//   changedOut        : false = payload_id 未变且非 pending → 调用方可跳过刷屏
//   displayedOut      : true  = delta 已执行局刷（调用方不应再 smartDisplay）
// 返回 false 表示链路失败（调用方回退 BMP）。
bool structuredFetchAndDisplay(String *renderedModeIdOut, bool *pendingRefreshOut,
                               bool *changedOut = nullptr, bool *displayedOut = nullptr);

// 第三阶段：传输与渲染分离，让调度器先依据 activity_key 决定当前页。
// forceRender 用于同一 payload_id 的正式切页，必须穿透按页去重。
bool structuredRenderDocument(JsonDocument &doc, bool pendingRefresh,
                              bool *changedOut = nullptr,
                              bool *displayedOut = nullptr,
                              bool forceRender = false);
bool structuredLoadCachedDocument(JsonDocument &doc);
void structuredSaveCachedDocument(const JsonDocument &doc);

// 空窗时钟模式：渲染时钟并进入 60s 深睡（深睡=重启，下次唤醒重新评估后端可达性）。
void enterClockMode();

// v4：最近一次成功拉取的 payload.ts（0=无）。
extern long long g_lastPayloadTs;

#endif // INKSIGHT_STRUCTURED_DISPLAY_H
