#ifndef INKSIGHT_V4_MODE_H
#define INKSIGHT_V4_MODE_H

#include <Arduino.h>

// 第三阶段唯一设备调度入口：法定工作日模式、自适应检查、正式切页、
// 网络退避、小时校时与下一有意义事件深睡。
// 返回 false 仅表示可信时间尚未建立，调用方执行冷启动联网校时；
// 返回 true 表示本周期已处理并进入物理深睡。
bool schedulerV4Cycle();

// Cold-boot fallback uses the same deep-sleep-persistent recovery sequence.
// This call does not return (it enters deep sleep).
void v4SleepAfterNetworkFailure(int normalDueSeconds = 0);

// Reset recovery state only after a structurally valid payload was obtained.
void v4RecordValidData();

#endif
