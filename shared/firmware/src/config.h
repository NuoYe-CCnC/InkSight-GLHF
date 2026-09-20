#ifndef INKSIGHT_CONFIG_H
#define INKSIGHT_CONFIG_H

#include <Arduino.h>

#ifndef INKSIGHT_BUILD_ID
#define INKSIGHT_BUILD_ID "development"
#endif

#if defined(BOARD_PROFILE_ESP32_C3)
#define PIN_EPD_MOSI   6
#define PIN_EPD_SCK    4
#define PIN_EPD_CS     7
#define PIN_EPD_DC     1
#define PIN_EPD_RST    2
#define PIN_EPD_BUSY   10
#define PIN_BAT_ADC    0
#define PIN_CFG_BTN    9
#define PIN_LED        3
#define PIN_AI_CHAT_SW -1
#elif defined(BOARD_PROFILE_ESP32_C3_WROOM02)
#define PIN_EPD_MOSI   6
#define PIN_EPD_SCK    4
#define PIN_EPD_CS     7
#define PIN_EPD_DC     1
#define PIN_EPD_RST    2
#define PIN_EPD_BUSY   10
#define PIN_BAT_ADC    0
#define PIN_CFG_BTN    9
#define PIN_LED        5
#define PIN_AI_CHAT_SW -1
#elif defined(BOARD_PROFILE_ESP32_WROOM32E)
#define PIN_EPD_MOSI   14
#define PIN_EPD_SCK    13
#define PIN_EPD_CS     15
#define PIN_EPD_DC     27
#define PIN_EPD_RST    26
#define PIN_EPD_BUSY   25
#define PIN_BAT_ADC    35
#define PIN_CFG_BTN    0
#define PIN_LED        2
#define PIN_AI_CHAT_SW 23
#define BOARD_HAS_AUDIO
#elif defined(BOARD_PROFILE_SMT_WROOM32E)
#define PIN_EPD_MOSI   14
#define PIN_EPD_SCK    13
#define PIN_EPD_CS     15
#define PIN_EPD_DC     27
#define PIN_EPD_RST    26
#define PIN_EPD_BUSY   25
#define PIN_BAT_ADC    35
#define PIN_CFG_BTN    0
#define PIN_LED        2
#define PIN_AI_CHAT_SW 4
#define BOARD_HAS_AUDIO
#elif defined(BOARD_PROFILE_SMT_C3)
#define PIN_EPD_MOSI   6
#define PIN_EPD_SCK    4
#define PIN_EPD_CS     7
#define PIN_EPD_DC     1
#define PIN_EPD_RST    2
#define PIN_EPD_BUSY   10
#define PIN_BAT_ADC    0
#ifndef PIN_CFG_BTN
#define PIN_CFG_BTN    9
#endif
#define PIN_LED        5
#define PIN_AI_CHAT_SW -1
#elif defined(BOARD_PROFILE_YD_ESP32_S3_N16R8)
#ifndef PIN_EPD_MOSI
#define PIN_EPD_MOSI   11
#endif
#ifndef PIN_EPD_SCK
#define PIN_EPD_SCK    12
#endif
#ifndef PIN_EPD_CS
#define PIN_EPD_CS     10
#endif
#ifndef PIN_EPD_DC
#define PIN_EPD_DC     9      // 微雪官方 S3 组合用 13
#endif
#ifndef PIN_EPD_RST
#define PIN_EPD_RST    8      // 官方 14
#endif
#ifndef PIN_EPD_BUSY
#define PIN_EPD_BUSY   7      // 官方 4
#endif
#ifndef PIN_EPD_PWR
#define PIN_EPD_PWR    -1     // 供电使能脚（9脚模块）：-1=未用；官方组合用 5
#endif
#define PIN_BAT_ADC    4
#define PIN_CFG_BTN    0
#define PIN_LED        -1
#define PIN_RGB_LED    48
#define PIN_AI_CHAT_SW -1
#else
#error "Unsupported board profile"
#endif

#ifndef PIN_EPD_PWR
#define PIN_EPD_PWR -1
#endif

#ifndef PIN_RGB_LED
#define PIN_RGB_LED -1
#endif

// ── Display constants ────────────────────────────────────────
// Default for 4.2" E-Paper (400x300, 1-bit).
// Override via build flags: -D EPD_WIDTH=800 -D EPD_HEIGHT=480
// Supported configurations:
//   4.2"  (400x300) - default
//   2.9"  (296x128)
//   5.83" (648x480)
//   7.5"  (800x480)
#ifndef EPD_WIDTH
#define EPD_WIDTH  400
#endif
#ifndef EPD_HEIGHT
#define EPD_HEIGHT 300
#endif

static const int W = EPD_WIDTH;
static const int H = EPD_HEIGHT;
static const int ROW_BYTES   = W / 8;
static const int ROW_STRIDE  = (ROW_BYTES + 3) & ~3;  // BMP row stride (4-byte aligned)
static const int IMG_BUF_LEN = ROW_BYTES * H;
/** Preprocessor image buffer size for #if (IMG_BUF_LEN is not a cpp constant). */
#define INKSIGHT_IMG_BUF_BYTES_MACRO ((EPD_WIDTH / 8) * (EPD_HEIGHT))

#ifndef EPD_BPP
#define EPD_BPP 1
#endif
static const int COLOR_BUF_LEN = (W * H) / 4;  // 2bpp: 4 pixels per byte

// Shared framebuffers (defined in main.cpp)
extern uint8_t imgBuf[];
#if EPD_BPP >= 2
extern uint8_t *colorBuf;
extern bool useColorBuf;
bool ensureColorBuf();
#endif

// ── Refresh strategy ─────────────────────────────────────────
static const int FULL_REFRESH_INTERVAL = 10;  // Full refresh every N updates to clear ghosting

// ── 第三阶段调度常量（Asia/Shanghai 时段 + 自适应检查）──────────
#ifndef PHASE3_STATE_SCHEMA
#define PHASE3_STATE_SCHEMA 2
#endif
#ifndef PHASE3_AI_HOLD_SECONDS
#define PHASE3_AI_HOLD_SECONDS 300
#endif
#ifndef PHASE3_NTP_INTERVAL_SECONDS
#define PHASE3_NTP_INTERVAL_SECONDS 3600
#endif
#ifndef PHASE3_UNKNOWN_MAX_AGE_SECONDS
#define PHASE3_UNKNOWN_MAX_AGE_SECONDS 1800
#endif
// 旧 v4 宏保留给非正式环境兼容；正式结构化云端由 phase3_policy 接管。
#ifndef ACTIVE_POLL_SECONDS
#define ACTIVE_POLL_SECONDS 60     // 活跃态拉取周期
#endif
#ifndef BOOT_WINDOW_SECONDS
#define BOOT_WINDOW_SECONDS 900    // 引导窗（上电/冷启/重连后：显示时钟+每60s拉，期内新鲜即活跃）
#endif
#ifndef STALE_MAX_SECONDS
#define STALE_MAX_SECONDS 1800     // ts 龄阈值：超过视为"发布端离开"
#endif
#ifndef SLEEP_PROBE_SECONDS
#define SLEEP_PROBE_SECONDS 900    // 休眠态云端探测周期
#endif
#ifndef SLEEP_CLOCK_SECONDS
#define SLEEP_CLOCK_SECONDS 60     // 休眠态走字/唤醒周期
#endif
#ifndef SLEEP_BLE_SCAN_MS
#define SLEEP_BLE_SCAN_MS 2000     // 休眠态每唤醒扫 2s（置 0 关闭）
#endif
// 兼容旧宏（保留占位，逐步弃用）
#ifndef DAY_POLL_SECONDS
#define DAY_POLL_SECONDS 60
#endif

// ── Config defaults ─────────────────────────────────────────
// 编译期默认值（一键烧录时经 -D 注入；NVS 无配置时使用；portal 可后续修改）
#ifndef DEFAULT_SSID
#define DEFAULT_SSID ""
#endif
#ifndef DEFAULT_PASS
#define DEFAULT_PASS ""
#endif
// 编译期预置多个备用网络（家 + 公司自动漫游），格式 "SSID2~PASS2^SSID3~PASS3"：
// 仅当 NVS 列表为空或首次安装时并入，总数不超过 MAX_WIFI_NETWORKS。
// 分隔符刻意选用非 shell/C 元字符的 '~'(字段) 与 '^'(记录)。
// 约束：SSID/密码内不要含 '~' '^' '|' ';' '"' 与反斜杠（shell/编译命令行限制）。
#ifndef EXTRA_WIFI
#define EXTRA_WIFI ""
#endif
#ifndef DEFAULT_SERVER
#define DEFAULT_SERVER ""  // 空 = 必须经 captive portal 配置
#endif

// ── 云端模式（坚果云 WebDAV，可选）────────────────────────────
// 设置 CLOUD_BASE_URL 后，设备直接从网盘拉取 <BASE>/<MAC>.json（Basic Auth），不再依赖后端。
#ifndef CLOUD_BASE_URL
#define CLOUD_BASE_URL ""
#endif
#ifndef CLOUD_USER
#define CLOUD_USER ""
#endif
#ifndef CLOUD_PASS
#define CLOUD_PASS ""
#endif
#define INKSIGHT_CLOUD_ENABLED (strlen(CLOUD_BASE_URL) > 0)

// ── 近场 BLE 触发（可选，骨架）────────────────────────────────
#ifndef ENABLE_BLE_TRIGGER
#define ENABLE_BLE_TRIGGER 0
#endif
#ifndef INKSIGHT_HTTP_TIMEOUT_MS
#define INKSIGHT_HTTP_TIMEOUT_MS 15000
#endif
#ifndef INKSIGHT_WIFI_ASSOCIATION_TIMEOUT_MS
#define INKSIGHT_WIFI_ASSOCIATION_TIMEOUT_MS 9000
#endif
#ifndef INKSIGHT_WIFI_CONNECT_ROUND_MS
#define INKSIGHT_WIFI_CONNECT_ROUND_MS 25000
#endif
#ifndef INKSIGHT_WIFI_FALLBACK_TRIES
#define INKSIGHT_WIFI_FALLBACK_TRIES 1
#endif
#ifndef INKSIGHT_NETWORK_FETCH_ROUND_MS
#define INKSIGHT_NETWORK_FETCH_ROUND_MS 60000
#endif
static const int   WIFI_TIMEOUT    = INKSIGHT_HTTP_TIMEOUT_MS;   // ms
static const int   MAX_WIFI_NETWORKS = 5;     // Max saved WiFi credentials (tried in order on boot)
static const int   WIFI_CONNECT_ATTEMPT_MS = INKSIGHT_WIFI_ASSOCIATION_TIMEOUT_MS;
static const int   WIFI_CONNECT_ROUND_MS   = INKSIGHT_WIFI_CONNECT_ROUND_MS;
static const int   WIFI_MAX_FALLBACK_TRIES = INKSIGHT_WIFI_FALLBACK_TRIES;
static const int   NETWORK_FETCH_ROUND_MS  = INKSIGHT_NETWORK_FETCH_ROUND_MS;
static const int   HTTP_TIMEOUT    = INKSIGHT_HTTP_TIMEOUT_MS;
static const int   CFG_BTN_HOLD_MS = 2000;    // Long press duration to trigger config mode
static const int   AI_CHAT_BTN_HOLD_MS = 3000; // Long press duration to enter AI chat mode
static const int   VOCAB_ENTER_HOLD_MS = 2000; // Long press duration to enter vocab review
static const int   VOCAB_BTN_HOLD_MS = 1500;  // Long press duration to submit vocab rating
static const int   VOCAB_EXIT_HOLD_MS = 5000; // Long press duration to exit vocab review
static const int   SHORT_PRESS_MIN_MS = 50;   // Minimum short press duration (debounce)
static const int   LIVE_POLL_MS = 5000;       // Poll interval for pending remote actions
static const int   LIVE_WIFI_RETRY_MS = 5000; // Retry interval when WiFi is disconnected
static const unsigned long TEMP_ONLINE_WINDOW_MS = 10UL * 60UL * 1000UL;
static const unsigned long PORTAL_AUTO_TIMEOUT_MS = 3UL * 60UL * 1000UL;
static const unsigned long PORTAL_MANUAL_TIMEOUT_MS = 10UL * 60UL * 1000UL;
static const unsigned long HEARTBEAT_INTERVAL_MS = 10UL * 60UL * 1000UL;
static const int   MAX_RETRY_COUNT = 5;       // Max retries before deep sleep
// WiFi -> captive portal fallback: when ALL saved networks fail to connect,
// do this many quick in-place retry sweeps (no reboot) before opening the AP.
// Keeps the portal fast to appear (user is likely waiting to reconfigure)
// while still riding out a brief blip such as a router rebooting.
static const int           WIFI_PORTAL_RETRY_SWEEPS   = 1;
static const unsigned long WIFI_PORTAL_RETRY_DELAY_MS = 3000;
// Progressive retry delays in seconds: 5s, 15s, 30s, 60s, 120s
static const int   RETRY_DELAYS[] = {30, 60, 120, 300};
static const int   RETRY_DELAY_COUNT = sizeof(RETRY_DELAYS) / sizeof(RETRY_DELAYS[0]);

// ── Time zone ───────────────────────────────────────────────
#define NTP_UTC_OFFSET  (8 * 3600)  // UTC+8 (China Standard Time), adjust for your region

// ── Debug mode ──────────────────────────────────────────────
#define DEBUG_MODE 0  // Set to 1 for fast refresh (1 min), 0 for user config
#if DEBUG_MODE
static const int DEBUG_REFRESH_MIN = 1;  // 1 minute for debugging
#endif

// ── Time display region (partial refresh area) ──────────────
// Proportional to screen size (scales across 2.9"/4.2"/7.5")
#define TIME_RGN_X0   (0)
#define TIME_RGN_X1   ((W * 14 / 100) & ~7)
#define TIME_RGN_Y0   (H * 2 / 100)
#define TIME_RGN_Y1   (H * 8 / 100)

#define TIME_TEXT_X   (W * 1 / 100)
#define TIME_TEXT_Y   (H * 4 / 100)

#ifndef AUTO_BOOT_AI_CHAT
#define AUTO_BOOT_AI_CHAT 0
#endif
#ifndef VOCAB_REVIEW_BUILD
#define VOCAB_REVIEW_BUILD 0
#endif

#endif // INKSIGHT_CONFIG_H
