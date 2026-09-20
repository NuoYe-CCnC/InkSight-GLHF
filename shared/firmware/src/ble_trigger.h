#ifndef INKSIGHT_BLE_TRIGGER_H
#define INKSIGHT_BLE_TRIGGER_H

#include <Arduino.h>

// 近场 BLE 触发：发布端"上传成功"后广播 "INK-UPDATE"，设备扫描到即触发拉取。
// 骨架说明：
//   - 路线 A（本文件实现）：唤醒时短扫 BLE（≤2s），匹配即返回 true；
//   - 真·近场即时（路线 B：浅睡常听）需在实机上验证 ESP32-S3 BLE-in-light-sleep
//     的功耗与收包稳定性，接口与调度已预留（见 schedule.cpp），启用时替换为常听方案。
// 未启用 ENABLE_BLE_TRIGGER 时为空实现。
bool bleScanForUpdate(unsigned long timeoutMs);

// 深睡前调用：停止扫描/广播并释放 BLE 资源（未启用时为空实现）。
void bleStopForSleep();

#endif // INKSIGHT_BLE_TRIGGER_H
