#ifndef INKSIGHT_LAYOUT_H
#define INKSIGHT_LAYOUT_H

#include <Arduino.h>
#include <ArduinoJson.h>

// 将结构化 payload（schema_ver=1，见 docs/InkSight-接口契约-v1.md）渲染到 imgBuf。
// 支持 widget：text / big_number / separator / spacer / icon_text / forecast_row / progress / footer。
// 返回 false 表示无法渲染（调用方回退 BMP）。
bool layoutRenderFromJson(JsonDocument &doc);
bool layoutRenderFull(JsonDocument &doc);  // 有 modules 走模块化全屏，否则走 widgets

// delta 模块局刷：仅重绘 payload 中列出的模块区域（临时缓冲 + epdPartialDisplayWithOld）。
bool layoutRenderDelta(JsonDocument &doc);

// 提取 screen.payload_id 到 out；用于"值未变化则跳过刷屏"。返回是否有值。
bool layoutGetPayloadId(JsonDocument &doc, char *out, size_t cap);

#endif // INKSIGHT_LAYOUT_H
