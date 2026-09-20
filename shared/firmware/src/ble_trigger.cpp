// ble_trigger.cpp — 近场 BLE 触发（唤醒时短扫，匹配 "INK-UPDATE"）
// NimBLE-Arduino v2 API（2.5.1）。扫描结果遍历，无按扫描器回调。
// 启用宏：-DENABLE_BLE_TRIGGER=1（config.h 缺省 0，值判定 #if ENABLE_BLE_TRIGGER）。
// 加固点：
//  1) init 后延时等 host 稳定（2.5.1 在部分唤醒后 init 即刻崩溃的规避）；
//  2) 结果对象先于 deinit 析构（避免访问已释放扫描对象）；
//  3) 扫描结束即 deinit，保证后续云端拉取/EPD 刷新无 BLE 并发。
#include "ble_trigger.h"
#include "config.h"

#if ENABLE_BLE_TRIGGER
#include <NimBLEDevice.h>

bool bleScanForUpdate(unsigned long timeoutMs) {
    Serial.println("[BLE] init start");
    if (!NimBLEDevice::init("")) {
        Serial.println("[BLE] init FAILED");
        return false;
    }
    Serial.println("[BLE] init ok");
    delay(300);  // host 稳定窗口

    NimBLEScan *scan = NimBLEDevice::getScan();
    scan->setActiveScan(false); // 被动扫描（主动扫描在本库/内核组合会崩）
    scan->setInterval(97);
    scan->setWindow(39);
    scan->start((uint32_t)timeoutMs, false); // v2: 毫秒
    scan->stop();

    bool found = false;
    {
        // 先在本作用域遍历结果，析构后再 deinit。
        // 匹配依据：广播 ADV 包内的服务 UUID FE9F（发布端广播该服务即触发；
        // 广播名在 scan response 中，被动扫描取不到）。
        NimBLEScanResults results = scan->getResults();
        for (int i = 0; i < results.getCount(); i++) {
            const NimBLEAdvertisedDevice *adv = results.getDevice(i);
            if (!adv) continue;
            if (adv->isAdvertisingService(NimBLEUUID("FE9F"))) {
                Serial.printf("[BLE] trigger received from %s\n",
                              adv->getAddress().toString().c_str());
                found = true;
                break;
            }
        }
    }
    if (found) Serial.println("[BLE] refresh requested");

    NimBLEDevice::deinit(true);
    Serial.println("[BLE] released (deinit ok)");
    return found;
}

// 深睡入口防御性兜底（扫描路径已释放；保留供未来广播/常听方案使用）
void bleStopForSleep() {
    Serial.println("[BLE] stopped before sleep");
}
#else // !ENABLE_BLE_TRIGGER
bool bleScanForUpdate(unsigned long timeoutMs) {
    (void)timeoutMs;
    return false;
}
void bleStopForSleep() {}
#endif // ENABLE_BLE_TRIGGER
