#ifndef INKSIGHT_SCHEDULE_H
#define INKSIGHT_SCHEDULE_H

#include <Arduino.h>

// 昼夜调度（北京时间，NTP_UTC_OFFSET=+8）：
//   07:00–19:00 白天 → 内容模式（按 schedulerDaySleepSeconds = 30s 轮询，有变才刷屏）
//   19:00–07:00 夜间 → 时钟模式（60s 周期渲染分钟 + 每小时 NTP 校准）
// 未启用 ENABLE_STRUCTURED 时全部为空实现（不影响其它面板 env）。
// BLE 触发模块（ENABLE_BLE_TRIGGER）当前保留未启用：代码在位，编译宏关。

// RTC 时间是否已校准（年份 >= 2023）
bool schedulerTimeValid();

// 当前是否白天（[7,19)；时间未知时保守返回 true 让主流程校时）
bool schedulerIsDaytime();

// 日间拉取间隔（秒）：默认 30（config.h DAY_POLL_SECONDS）；由主流程按秒深睡
int schedulerDaySleepSeconds();

// 兼容旧接口（分钟粒度，供非结构化 env 使用）
int schedulerDaySleepMinutes();

// 白天轻唤醒（BLE 保留代码）：启用 ENABLE_BLE_TRIGGER 时生效（60s 周期扫 BLE）；
// 当前编译停用 → 恒 false，日间直接按 schedulerDaySleepSeconds 拉取。
bool schedulerDaylightSkipFetch();

// 夜间时钟周期：渲染/跳过分钟 + 每小时 NTP + 60s 深睡（重启后重进）。
// 返回 true 表示已处理（调用方应 return）。
bool schedulerNightClockBoot();

#endif // INKSIGHT_SCHEDULE_H
