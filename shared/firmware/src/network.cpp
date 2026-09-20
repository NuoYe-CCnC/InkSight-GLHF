#include "network.h"
#include "config.h"
#include "storage.h"
#include "certs.h"
#include "audio.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <LittleFS.h>
#include <mbedtls/base64.h>
#include <time.h>
#ifdef ENABLE_STRUCTURED
#include <esp32-hal-psram.h>
#include <Preferences.h>
#include <esp_sleep.h>
#include <esp_system.h>
#endif
#if defined(BOARD_PROFILE_ESP32_C3_WROOM02) || defined(BOARD_PROFILE_SMT_WROOM32E) || defined(BOARD_PROFILE_YD_ESP32_S3_N16R8)
#include <esp_adc_cal.h>
#endif

// ── Time state ──────────────────────────────────────────────
int curHour, curMin, curSec;
static unsigned long lastHeartbeatAt = 0;
bool g_userAborted = false;
bool g_suppressAbortCheck = false;
static NetworkFailureStage g_networkFailureStage = NetworkFailureStage::None;
static unsigned long g_networkRoundStartedAt = 0;

const char *networkFailureStageName(NetworkFailureStage stage) {
    switch (stage) {
        case NetworkFailureStage::Scan: return "wifi-scan";
        case NetworkFailureStage::Association: return "wifi-association";
        case NetworkFailureStage::Authentication: return "wifi-authentication";
        case NetworkFailureStage::IP: return "ip-dhcp";
        case NetworkFailureStage::DNS: return "dns";
        case NetworkFailureStage::TLS: return "tls";
        case NetworkFailureStage::HTTP: return "http";
        case NetworkFailureStage::JSON: return "json";
        default: return "none";
    }
}

NetworkFailureStage networkLastFailureStage() { return g_networkFailureStage; }

static void markNetworkFailure(NetworkFailureStage stage, const char *detail = nullptr) {
    g_networkFailureStage = stage;
    Serial.printf("[NET][FAIL] stage=%s", networkFailureStageName(stage));
    if (detail && *detail) Serial.printf(" detail=%s", detail);
    Serial.println();
}

static bool networkRoundExpired() {
    return g_networkRoundStartedAt != 0
        && (millis() - g_networkRoundStartedAt) >= (unsigned long)NETWORK_FETCH_ROUND_MS;
}

static bool checkAbort() {
    if (g_suppressAbortCheck) return false;
    if (digitalRead(PIN_CFG_BTN) == LOW) {
        delay(50);
        if (digitalRead(PIN_CFG_BTN) == LOW) {
            g_userAborted = true;
            return true;
        }
    }
    return false;
}

static bool beginHttpForUrl(HTTPClient &http, WiFiClient &plainClient, WiFiClientSecure &secClient, const String &url);
static bool recoverDeviceTokenIfUnauthorized(int code);
static String extractJsonStringField(const String &body, const char *key);
static String extractJsonBoolField(const String &body, const char *key);
static int extractJsonIntField(const String &body, const char *key, int defaultValue = 0);
static bool parseWsServerUrl(const String &baseUrl, bool &useSSL, String &hostOut, uint16_t &portOut, String &pathOut);
static bool decodeBase64ToHeap(const String &encoded, uint8_t **dataOut, size_t *dataLenOut);
static String encodeBase64(const uint8_t *data, size_t len);
static void handleVoiceWsEvent(VoiceWsEvent &event);
static void voiceWsSocketEvent(WStype_t type, uint8_t *payload, size_t length);

static WebSocketsClient gVoiceWsClient;
static bool gVoiceWsConnected = false;
static unsigned long gVoiceWsOpenStartedAt = 0;
static unsigned long gVoiceWsConnectedAt = 0;
static bool gVoiceWsDisconnected = false;
static bool gVoiceWsConfigured = false;
static bool gVoiceWsBinaryAudioEnabled = false;
static bool gVoiceWsOpusEnabled = false;
static bool gVoiceWsServerVadEnabled = false;
static QueueHandle_t gVoiceWsEventQueue = nullptr;

// ── WiFi connection ─────────────────────────────────────────

// Associate with a single AP and wait up to timeoutMs. Returns true on
// success. Returns false on timeout/abort (g_userAborted set on abort).
static bool tryAssociate(const String &ssid, const String &pass, unsigned long timeoutMs,
                         bool scanKnown, bool visible) {
    Serial.printf("[NET][ASSOC] ssid=%s visible=%s timeout=%lums ", ssid.c_str(),
                  scanKnown ? (visible ? "yes" : "no-or-hidden") : "unknown", timeoutMs);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (checkAbort()) return false;
        if (millis() - t0 > timeoutMs) {
            wl_status_t status = WiFi.status();
            Serial.printf("TIMEOUT status=%d\n", (int)status);
            if (status == WL_CONNECT_FAILED) {
                markNetworkFailure(NetworkFailureStage::Authentication, "connect-failed");
            } else if (status == WL_NO_SSID_AVAIL) {
                markNetworkFailure(NetworkFailureStage::Scan, "ssid-unavailable");
            } else {
                markNetworkFailure(NetworkFailureStage::Association, "timeout");
            }
            return false;
        }
        delay(300);
        Serial.print(".");
    }
    IPAddress ip = WiFi.localIP();
    if ((uint32_t)ip == 0) {
        markNetworkFailure(NetworkFailureStage::IP, "dhcp-no-address");
        WiFi.disconnect();
        return false;
    }
    Serial.printf("OK ip=%s rssi=%d channel=%d\n", ip.toString().c_str(),
                  WiFi.RSSI(), WiFi.channel());
    return true;
}

bool connectWiFi() {
    g_userAborted = false;
    g_networkFailureStage = NetworkFailureStage::None;
    g_networkRoundStartedAt = millis();

    // Scan once for observability. Hidden SSIDs are still attempted.
    int count = getWiFiCount();
    bool visible[MAX_WIFI_NETWORKS] = {false, false, false, false, false};
    bool scanKnown = false;
    WiFi.mode(WIFI_STA);
    int scanCount = WiFi.scanNetworks(false, true);
    if (scanCount >= 0) {
        scanKnown = true;
        Serial.printf("[NET][SCAN] found=%d configured=%d\n", scanCount, count);
        for (int i = 0; i < count; i++) {
            String configured, ignored;
            if (!getWiFiAt(i, configured, ignored)) continue;
            for (int j = 0; j < scanCount; j++) {
                if (WiFi.SSID(j) == configured) {
                    visible[i] = true;
                    Serial.printf("[NET][SCAN] slot=%d ssid=%s visible=yes rssi=%d channel=%d\n",
                                  i, configured.c_str(), WiFi.RSSI(j), WiFi.channel(j));
                    break;
                }
            }
            if (!visible[i]) {
                Serial.printf("[NET][SCAN] slot=%d ssid=%s visible=no-or-hidden\n",
                              i, configured.c_str());
            }
        }
    } else {
        Serial.printf("[NET][SCAN] failed code=%d; association will still be attempted\n", scanCount);
    }
    WiFi.scanDelete();

    // Try preferred plus one fallback only; connection and complete sweep are bounded.
    if (count <= 0) {
        if (cfgSSID.length() == 0) return false;
        if (!tryAssociate(cfgSSID, cfgPass, WIFI_CONNECT_ATTEMPT_MS, scanKnown, false)) return false;
    } else {
        bool associated = false;
        int tries = min(count, 1 + WIFI_MAX_FALLBACK_TRIES);
        for (int i = 0; i < tries; i++) {
            String ssid, pass;
            if (!getWiFiAt(i, ssid, pass)) continue;
            if (g_userAborted) return false;
            unsigned long elapsed = millis() - g_networkRoundStartedAt;
            if (elapsed >= (unsigned long)WIFI_CONNECT_ROUND_MS) {
                markNetworkFailure(NetworkFailureStage::Association, "round-limit");
                break;
            }
            unsigned long remaining = (unsigned long)WIFI_CONNECT_ROUND_MS - elapsed;
            unsigned long attemptLimit = min((unsigned long)WIFI_CONNECT_ATTEMPT_MS, remaining);
            Serial.printf("[WIFI] Trying network %d/%d (bounded total=%d)\n", i + 1, tries, count);
            if (tryAssociate(ssid, pass, attemptLimit, scanKnown, visible[i])) {
                // Promote the connected network to primary so cfgSSID/cfgPass
                // reflect this connection only; saved preference order is unchanged.
                cfgSSID = ssid;
                cfgPass = pass;
                associated = true;
                break;
            }
            if (g_userAborted) return false;
        }
        if (!associated) {
            if (g_networkFailureStage == NetworkFailureStage::None) {
                markNetworkFailure(scanKnown ? NetworkFailureStage::Scan
                                             : NetworkFailureStage::Association,
                                   "preferred-and-fallback-failed");
            }
            Serial.printf("[WIFI] bounded sweep failed elapsed=%lums\n",
                          millis() - g_networkRoundStartedAt);
            return false;
        }
    }

    // 云端模式：无本机后端，跳过设备注册/token/配对（否则 cfgServer 为空必然失败，
    // 导致 connectWiFi 误判 WiFi 不可用 → 启动误入 portal）
    if (!INKSIGHT_CLOUD_ENABLED) {
        if (!ensureDeviceToken()) {
            markNetworkFailure(NetworkFailureStage::HTTP, "device-token");
            return false;
        }
    }
    if (cfgPendingPairCode.length() > 0 && !INKSIGHT_CLOUD_ENABLED) {
        String mac = WiFi.macAddress();
        String url = cfgServer + "/api/device/" + mac + "/claim-token";
        String body = String("{\"pair_code\":\"") + cfgPendingPairCode + "\"}";
        for (int attempt = 0; attempt < 3; attempt++) {
            if (checkAbort()) return false;
            Serial.printf("[PAIR] POST %s (attempt %d/3)\n", url.c_str(), attempt + 1);
            WiFiClient plainClient;
            WiFiClientSecure secClient;
            HTTPClient http;
            if (!beginHttpForUrl(http, plainClient, secClient, url)) {
                Serial.println("[PAIR] begin failed");
                delay(800);
                continue;
            }
            http.addHeader("Content-Type", "application/json");
            if (cfgDeviceToken.length() > 0) {
                http.addHeader("X-Device-Token", cfgDeviceToken);
            }
            http.setTimeout(HTTP_TIMEOUT);

            int code = http.POST(body);
            Serial.printf("[PAIR] HTTP code: %d\n", code);
            if (code >= 200 && code < 300) {
                String resp = http.getString();
                String savedPairCode = extractJsonStringField(resp, "pair_code");
                http.end();
                if (savedPairCode == cfgPendingPairCode) {
                    clearPendingPairCode();
                    Serial.println("[PAIR] pair code registered");
                    break;
                }
                Serial.printf(
                    "[PAIR] pair code mismatch: local=%s remote=%s\n",
                    cfgPendingPairCode.c_str(),
                    savedPairCode.length() > 0 ? savedPairCode.c_str() : "empty"
                );
                delay(800);
                continue;
            }
            if (code < 0) {
                Serial.printf("[PAIR] error: %s\n", http.errorToString(code).c_str());
            } else {
                String resp = http.getString();
                Serial.printf("[PAIR] response: %s\n", resp.substring(0, 300).c_str());
            }
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) {
                delay(800);
            }
        }
    }
    postHeartbeat(true);
    g_networkFailureStage = NetworkFailureStage::None;
    Serial.printf("[NET][READY] wifi+ip elapsed=%lums\n", millis() - g_networkRoundStartedAt);
    return true;
}

// ── Battery voltage ─────────────────────────────────────────

float readBatteryVoltage() {
#if defined(PIN_EPD_BUSY) && PIN_EPD_BUSY >= 0 && PIN_BAT_ADC == PIN_EPD_BUSY
    // 本板 PIN_BAT_ADC=4=GPIO4(BUSY)：analogRead 会把 BUSY 切成 ADC 模式，
    // 导致电子纸 BUSY 读取失效（假刷新）。无独立电池脚时直接返回 0。
    return 0.0f;
#else
    const int SAMPLES = 16;
    const int DISCARD = 2;  // Discard highest and lowest outliers
    int readings[SAMPLES];

    for (int i = 0; i < SAMPLES; i++) {
        readings[i] = analogRead(PIN_BAT_ADC);
        delayMicroseconds(100);
    }
#endif

    // Sort for outlier removal
    for (int i = 0; i < SAMPLES - 1; i++)
        for (int j = i + 1; j < SAMPLES; j++)
            if (readings[i] > readings[j]) {
                int tmp = readings[i];
                readings[i] = readings[j];
                readings[j] = tmp;
            }

    // Average middle readings (discard DISCARD highest and lowest)
    long sum = 0;
    for (int i = DISCARD; i < SAMPLES - DISCARD; i++)
        sum += readings[i];

    float avgRaw = (float)sum / (SAMPLES - 2 * DISCARD);
#if defined(BOARD_PROFILE_ESP32_C3_WROOM02) || defined(BOARD_PROFILE_SMT_WROOM32E)
    static esp_adc_cal_characteristics_t adcChars;
    static bool calibrated = false;
    if (!calibrated) {
        esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_12, ADC_WIDTH_BIT_12, 1100, &adcChars);
        calibrated = true;
    }

    uint32_t mv = esp_adc_cal_raw_to_voltage((uint32_t)avgRaw, &adcChars);
    float realBatteryVoltage = (mv / 1000.0f) * 2.0f; // R1=10k, R2=10k
    Serial.printf("[BAT] raw=%.1f adc=%umV vbat=%.2fV\n", avgRaw, (unsigned int)mv, realBatteryVoltage);
    return realBatteryVoltage;
#else
    float realBatteryVoltage = avgRaw * (3.3f / 4095.0f) * 2.0f;
    Serial.printf("[BAT] raw=%.1f vbat=%.2fV\n", avgRaw, realBatteryVoltage);
    return realBatteryVoltage;
#endif
}

// ── Stream helper ───────────────────────────────────────────

static bool readExact(WiFiClient *s, uint8_t *buf, int len) {
    int got = 0;
    unsigned long t0 = millis();
    while (got < len) {
        if (millis() - t0 > 10000) {
            Serial.printf(
                "readExact: timeout %d/%d connected=%d available=%d wifi=%d\n",
                got,
                len,
                s->connected() ? 1 : 0,
                s->available(),
                WiFi.status()
            );
            return false;
        }
        int avail = s->available();
        if (avail > 0) {
            int r = s->readBytes(buf + got, min(avail, len - got));
            got += r;
            t0 = millis();  // Reset timeout on progress
        } else {
            delay(1);
        }
    }
    return true;
}

static bool beginHttpForUrl(HTTPClient &http, WiFiClient &plainClient, WiFiClientSecure &secClient, const String &url) {
    if (url.startsWith("https://")) {
        secClient.setCACert(ROOT_CA);
        return http.begin(secClient, url);
    }
    return http.begin(plainClient, url);
}

static bool resolveUrlHost(const String &url, IPAddress &resolved) {
    int scheme = url.indexOf("://");
    int start = scheme >= 0 ? scheme + 3 : 0;
    int end = url.indexOf('/', start);
    String authority = end >= 0 ? url.substring(start, end) : url.substring(start);
    int at = authority.lastIndexOf('@');
    if (at >= 0) authority = authority.substring(at + 1);
    int colon = authority.indexOf(':');
    String host = colon >= 0 ? authority.substring(0, colon) : authority;
    host.trim();
    if (host.length() == 0) {
        markNetworkFailure(NetworkFailureStage::DNS, "empty-host");
        return false;
    }
    if (resolved.fromString(host)) {
        Serial.printf("[NET][DNS] host=%s literal=%s\n", host.c_str(), resolved.toString().c_str());
        return true;
    }
    if (WiFi.hostByName(host.c_str(), resolved) != 1 || (uint32_t)resolved == 0) {
        markNetworkFailure(NetworkFailureStage::DNS, host.c_str());
        return false;
    }
    Serial.printf("[NET][DNS] host=%s ip=%s\n", host.c_str(), resolved.toString().c_str());
    return true;
}

static String extractJsonStringField(const String &body, const char *key) {
    String needle = String("\"") + key + "\"";
    int start = body.indexOf(needle);
    if (start < 0) return "";
    start += needle.length();
    while (start < body.length() && (body[start] == ' ' || body[start] == '\t' || body[start] == '\r' || body[start] == '\n')) {
        start++;
    }
    if (start >= body.length() || body[start] != ':') return "";
    start++;
    while (start < body.length() && (body[start] == ' ' || body[start] == '\t' || body[start] == '\r' || body[start] == '\n')) {
        start++;
    }
    if (start >= body.length() || body[start] != '"') return "";
    start++;
    int end = body.indexOf('"', start);
    if (end < 0) return "";
    return body.substring(start, end);
}

static String extractJsonBoolField(const String &body, const char *key) {
    String needle = String("\"") + key + "\":";
    int start = body.indexOf(needle);
    if (start < 0) return "";
    start += needle.length();
    while (start < body.length() && body[start] == ' ') {
        start++;
    }
    if (body.startsWith("true", start)) return "true";
    if (body.startsWith("false", start)) return "false";
    if (body.startsWith("1", start)) return "1";
    if (body.startsWith("0", start)) return "0";
    return "";
}

static int extractJsonIntField(const String &body, const char *key, int defaultValue) {
    String needle = String("\"") + key + "\":";
    int start = body.indexOf(needle);
    if (start < 0) return defaultValue;
    start += needle.length();
    while (start < body.length() && body[start] == ' ') {
        start++;
    }
    int end = start;
    while (end < body.length() && (body[end] == '-' || (body[end] >= '0' && body[end] <= '9'))) {
        end++;
    }
    if (end <= start) return defaultValue;
    return body.substring(start, end).toInt();
}

static bool parseWsServerUrl(const String &baseUrl, bool &useSSL, String &hostOut, uint16_t &portOut, String &pathOut) {
    String url = baseUrl;
    useSSL = false;
    if (url.startsWith("https://")) {
        useSSL = true;
        url = url.substring(8);
        portOut = 443;
    } else if (url.startsWith("http://")) {
        url = url.substring(7);
        portOut = 80;
    } else {
        return false;
    }

    int slash = url.indexOf('/');
    String hostPort = slash >= 0 ? url.substring(0, slash) : url;
    pathOut = slash >= 0 ? url.substring(slash) : "";
    if (pathOut.length() == 0) {
        pathOut = "";
    }

    int colon = hostPort.indexOf(':');
    if (colon >= 0) {
        hostOut = hostPort.substring(0, colon);
        portOut = (uint16_t)hostPort.substring(colon + 1).toInt();
    } else {
        hostOut = hostPort;
    }
    if (hostOut.length() == 0) {
        return false;
    }
    return true;
}

static bool decodeBase64ToHeap(const String &encoded, uint8_t **dataOut, size_t *dataLenOut) {
    if (dataOut == nullptr || dataLenOut == nullptr) return false;
    *dataOut = nullptr;
    *dataLenOut = 0;
    if (encoded.length() == 0) {
        return true;
    }
    size_t maxLen = ((encoded.length() + 3) / 4) * 3 + 4;
    uint8_t *buffer = (uint8_t *)malloc(maxLen);
    if (buffer == nullptr) {
        return false;
    }
    size_t outputLen = 0;
    int rc = mbedtls_base64_decode(buffer, maxLen, &outputLen, (const unsigned char *)encoded.c_str(), encoded.length());
    if (rc != 0) {
        free(buffer);
        return false;
    }
    *dataOut = buffer;
    *dataLenOut = outputLen;
    return true;
}

static String encodeBase64(const uint8_t *data, size_t len) {
    if (data == nullptr || len == 0) return "";
    size_t outputLen = 0;
    size_t capacity = ((len + 2) / 3) * 4 + 4;
    unsigned char *buffer = (unsigned char *)malloc(capacity);
    if (buffer == nullptr) {
        return "";
    }
    int rc = mbedtls_base64_encode(buffer, capacity, &outputLen, data, len);
    if (rc != 0) {
        free(buffer);
        return "";
    }
    buffer[outputLen] = '\0';
    String encoded = String((const char *)buffer);
    free(buffer);
    return encoded;
}

static bool recoverDeviceTokenIfUnauthorized(int code) {
    if (code != 401 || cfgDeviceToken.length() == 0) return false;
    Serial.println("[AUTH] 401 unauthorized, resetting cached device token");
    clearDeviceToken();
    return ensureDeviceToken();
}

bool postHeartbeat(bool force) {
    if (WiFi.status() != WL_CONNECTED) return false;
    unsigned long now = millis();
    if (!force && lastHeartbeatAt != 0 && now - lastHeartbeatAt < HEARTBEAT_INTERVAL_MS) {
        return true;
    }
    if (!ensureDeviceToken()) return false;

    float v = readBatteryVoltage();
    int rssi = WiFi.RSSI();
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/heartbeat";
    String body = String("{\"battery_voltage\":") + String(v, 2)
        + ",\"wifi_rssi\":" + String(rssi)
        + ",\"firmware_build_id\":\"" + String(INKSIGHT_BUILD_ID) + "\"}";
    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (!beginHttpForUrl(http, plainClient, secClient, url)) return false;
        http.addHeader("Content-Type", "application/json");
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }
        http.setTimeout(HTTP_TIMEOUT);

        int code = http.POST(body);
        if (code >= 200 && code < 300) {
            String response = http.getString();
            String issuedToken = extractJsonStringField(response, "device_token");
            if (issuedToken.length() > 0) {
                saveDeviceToken(issuedToken);
                Serial.println("[HEARTBEAT] device token installed");
            }
            Serial.printf("[HEARTBEAT] POST -> %d\n", code);
            http.end();
            lastHeartbeatAt = now;
            return true;
        }
        if (code < 0) {
            Serial.printf("[HEARTBEAT] error: %s\n", http.errorToString(code).c_str());
        } else {
            Serial.printf("[HEARTBEAT] POST -> %d\n", code);
        }
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return false;
        }
    }
    return false;
}

bool ensureDeviceToken() {
    if (INKSIGHT_CLOUD_ENABLED) return true;  // 云端模式不依赖本机后端 token
    if (cfgDeviceToken.length() > 0) return true;
    if (WiFi.status() != WL_CONNECTED) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/token";
    delay(1200);
    for (int attempt = 0; attempt < 3; attempt++) {
        if (checkAbort()) return false;
        Serial.printf("[TOKEN] POST %s (attempt %d/3)\n", url.c_str(), attempt + 1);
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (!beginHttpForUrl(http, plainClient, secClient, url)) {
            Serial.println("[TOKEN] begin failed");
            delay(800);
            continue;
        }
        http.addHeader("Content-Type", "application/json");
        http.setTimeout(HTTP_TIMEOUT);

        int code = http.POST("{}");
        Serial.printf("[TOKEN] HTTP code: %d\n", code);
        if (code >= 200 && code < 300) {
            String body = http.getString();
            http.end();
            String token = extractJsonStringField(body, "token");
            if (token.length() == 0) {
                Serial.println("[TOKEN] token field empty");
                delay(800);
                continue;
            }
            saveDeviceToken(token);
            Serial.println("[TOKEN] token saved");
            return true;
        }
        if (code < 0) {
            Serial.printf("[TOKEN] error: %s\n", http.errorToString(code).c_str());
        } else {
            String body = http.getString();
            Serial.printf("[TOKEN] response: %s\n", body.substring(0, 300).c_str());
        }
        http.end();
        delay(800);
    }
    Serial.println("[TOKEN] failed to obtain device token");
    return false;
}

bool fetchFocusListeningFlag(bool *outEnabled, bool *outAlwaysActive) {
    if (!outEnabled) return false;
    *outEnabled = false;
    if (outAlwaysActive) *outAlwaysActive = false;
    if (WiFi.status() != WL_CONNECTED) return false;
    if (!ensureDeviceToken()) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/config/" + mac;
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) return false;
            continue;
        }

        String body = http.getString();
        http.end();
        bool enabled =
            body.indexOf("\"is_focus_listening\":true") >= 0 ||
            body.indexOf("\"is_focus_listening\": true") >= 0 ||
            body.indexOf("\"focus_listening\":1") >= 0 ||
            body.indexOf("\"focus_listening\": 1") >= 0;
        bool alwaysActive =
            body.indexOf("\"is_always_active\":true") >= 0 ||
            body.indexOf("\"is_always_active\": true") >= 0 ||
            body.indexOf("\"always_active\":1") >= 0 ||
            body.indexOf("\"always_active\": 1") >= 0 ||
            body.indexOf("\"always_active\":true") >= 0 ||
            body.indexOf("\"always_active\": true") >= 0;
        *outEnabled = enabled;
        if (outAlwaysActive) *outAlwaysActive = alwaysActive;
        Serial.printf("[CONFIG] is_focus_listening=%s always_active=%s\n",
                      enabled ? "true" : "false",
                      alwaysActive ? "true" : "false");
        return true;
    }
    return false;
}

bool fetchFocusAlertBMP() {
    if (WiFi.status() != WL_CONNECTED) return false;
    if (!ensureDeviceToken()) return false;
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/alert-bmp"
               + "?w=" + String(W) + "&h=" + String(H);
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        Serial.printf("[FOCUS] alert-bmp HTTP code: %d\n", code);
        if (code == 204) {
            http.end();
            return false;
        }
        if (code != 200) {
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) return false;
            continue;
        }

        WiFiClient *stream = http.getStreamPtr();
        uint8_t fileHeader[14];
        if (!readExact(stream, fileHeader, 14)) {
            http.end();
            return false;
        }
        uint32_t pixelOffset = fileHeader[10]
                             | ((uint32_t)fileHeader[11] << 8)
                             | ((uint32_t)fileHeader[12] << 16)
                             | ((uint32_t)fileHeader[13] << 24);
        int toSkip = (int)pixelOffset - 14;
        while (toSkip > 0 && stream->connected()) {
            if (stream->available()) { stream->read(); toSkip--; }
        }

        uint8_t rowBuf[ROW_STRIDE];
        for (int bmpY = 0; bmpY < H; bmpY++) {
            if (!readExact(stream, rowBuf, ROW_STRIDE)) {
                http.end();
                return false;
            }
            int dispY = H - 1 - bmpY;
            memcpy(imgBuf + dispY * ROW_BYTES, rowBuf, ROW_BYTES);
        }
        http.end();
        return true;
    }
    return false;
}

// ── Fetch BMP from backend ──────────────────────────────────

bool fetchBMP(bool nextMode, bool *isFallback, String *renderedModeIdOut) {
    if (isFallback) *isFallback = false;
    if (renderedModeIdOut) *renderedModeIdOut = "";
    if (!ensureDeviceToken()) return false;
    float v = readBatteryVoltage();
    String mac = WiFi.macAddress();
    int rssi = WiFi.RSSI();
#if DEBUG_MODE
    int effectiveRefreshMin = DEBUG_REFRESH_MIN;
#else
    int effectiveRefreshMin = cfgSleepMin;
#endif
#if EPD_BPP >= 2
    const int colorCapability = 4;
#else
    const int colorCapability = 2;
#endif
    String url = cfgServer + "/api/render?v=" + String(v, 2)
               + "&mac=" + mac + "&rssi=" + String(rssi)
               + "&refresh_min=" + String(effectiveRefreshMin)
               + "&w=" + String(W) + "&h=" + String(H)
               + "&bpp=" + String(EPD_BPP)
               + "&colors=" + String(colorCapability);
    if (nextMode) {
        url += "&next=1";
    }
    Serial.printf("GET %s (RSSI=%d)\n", url.c_str(), rssi);

    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort()) {
            Serial.println("[RENDER] fetchBMP aborted before HTTP");
            return false;
        }
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        bool begun = false;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            begun = http.begin(secClient, url);
        } else {
            begun = http.begin(plainClient, url);
        }
        if (!begun) {
            Serial.println("[RENDER] http.begin failed");
            http.end();
            return false;
        }
        http.setReuse(false);
        http.setTimeout(HTTP_TIMEOUT);
        http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
        const char *headerKeys[] = {"X-Content-Fallback", "X-Refresh-Minutes", "X-Mode-Id"};
        http.collectHeaders(headerKeys, 3);

        http.addHeader("Accept-Encoding", "identity");
        http.addHeader("Connection", "close");
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        Serial.printf("Free heap: %d\n", ESP.getFreeHeap());
        int code = http.GET();
        Serial.printf("HTTP code: %d\n", code);
        if (renderedModeIdOut && code >= 200 && code < 300) {
            *renderedModeIdOut = http.header("X-Mode-Id");
        }
        if (isFallback) {
            String fallbackHeader = http.header("X-Content-Fallback");
            *isFallback = (fallbackHeader == "1" || fallbackHeader == "true");
            if (*isFallback) {
                Serial.println("[RENDER] Received fallback content");
            }
        }
        String refreshHeader = http.header("X-Refresh-Minutes");
        int serverRefreshMin = refreshHeader.toInt();
        if (serverRefreshMin >= 10 && serverRefreshMin <= 1440 && serverRefreshMin != cfgSleepMin) {
            saveSleepMin(serverRefreshMin);
            Serial.printf("[RENDER] Applied refresh interval: %d min\n", serverRefreshMin);
        }

        if (code != 200) {
            if (code < 0) {
                Serial.printf("HTTP error: %s\n", http.errorToString(code).c_str());
            } else {
                String body = http.getString();
                Serial.printf("Response: %s\n", body.substring(0, 500).c_str());
            }
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) {
                return false;
            }
            continue;
        }

        int contentLen = http.getSize();
        Serial.printf("Content-Length: %d\n", contentLen);

        WiFiClient *stream = http.getStreamPtr();

#if EPD_BPP >= 2
        if (contentLen == COLOR_BUF_LEN) {
            if (!ensureColorBuf()) {
                Serial.println("colorBuf alloc failed, skip 2bpp");
                http.end();
                return false;
            }
            if (!readExact(stream, colorBuf, COLOR_BUF_LEN)) {
                Serial.println("Failed to read 2bpp data");
                http.end();
                return false;
            }
            useColorBuf = true;
            http.end();
            Serial.printf("2BPP OK  %d bytes\n", COLOR_BUF_LEN);
            lastHeartbeatAt = millis();
            return true;
        }
        Serial.printf("Not raw 2bpp (%d bytes), fallback to BMP\n", contentLen);
        useColorBuf = false;
#endif

        uint8_t fileHeader[14];
        if (!readExact(stream, fileHeader, 14)) {
            Serial.println("Failed to read BMP header");
            http.end();
            return false;
        }

        uint32_t pixelOffset = fileHeader[10]
                             | ((uint32_t)fileHeader[11] << 8)
                             | ((uint32_t)fileHeader[12] << 16)
                             | ((uint32_t)fileHeader[13] << 24);
        Serial.printf("BMP pixel offset: %u\n", pixelOffset);

        // Read info header to get bit count (biBitCount at offset 28 = 14+14)
        uint8_t infoHeader[16];
        if (!readExact(stream, infoHeader, 16)) {
            Serial.println("Failed to read BMP info header");
            http.end();
            return false;
        }
        int bmpBits = infoHeader[14] | ((int)infoHeader[15] << 8);
        Serial.printf("BMP bit count: %d\n", bmpBits);

        int toSkip = pixelOffset - 14 - 16;  // skip remaining header+palette
        while (toSkip > 0 && stream->connected()) {
            if (stream->available()) { stream->read(); toSkip--; }
        }

        memset(imgBuf, 0xFF, IMG_BUF_LEN);

        if (bmpBits <= 1) {
            // 1-bit BMP: each row is ROW_STRIDE bytes (padded)
            uint8_t rowBuf[ROW_STRIDE];
            for (int bmpY = 0; bmpY < H; bmpY++) {
                if (!readExact(stream, rowBuf, ROW_STRIDE)) {
                    Serial.printf("Failed to read row %d\n", bmpY);
                    http.end();
                    return false;
                }
                int dispY = H - 1 - bmpY;
                memcpy(imgBuf + dispY * ROW_BYTES, rowBuf, ROW_BYTES);
            }
        } else {
            // 8-bit (or 24-bit) BMP: convert each pixel to 1 bit
            int srcRowBytes = (W * bmpBits + 31) / 32 * 4;
            uint8_t *srcRow = (uint8_t *)malloc(srcRowBytes);
            if (!srcRow) {
                Serial.println("Failed to alloc srcRow");
                http.end();
                return false;
            }
            for (int bmpY = 0; bmpY < H; bmpY++) {
                if (!readExact(stream, srcRow, srcRowBytes)) {
                    Serial.printf("Failed to read row %d\n", bmpY);
                    free(srcRow);
                    http.end();
                    return false;
                }
                int dispY = H - 1 - bmpY;
                for (int x = 0; x < W; x++) {
                    uint8_t pixel;
                    if (bmpBits == 8) {
                        pixel = srcRow[x];
                    } else {
                        pixel = srcRow[x * 3];  // 24-bit: use blue channel
                    }
                    if (pixel < 128) {
                        imgBuf[dispY * ROW_BYTES + x / 8] &= ~(0x80 >> (x % 8));
                    }
                }
            }
            free(srcRow);
        }

        http.end();
        Serial.printf("BMP OK  %d bytes\n", IMG_BUF_LEN);
        lastHeartbeatAt = millis();
        return true;
    }
    return false;
}

#ifdef ENABLE_STRUCTURED
// ── Fetch structured payload (JSON v1) from backend ─────────
// 与 fetchBMP 同构：URL/鉴权/TLS/重试；响应体读入 PSRAM 后解析进 JsonDocument。
bool fetchStructured(JsonDocument &doc, String *modeIdOut, bool *pendingRefreshOut) {
    if (modeIdOut) *modeIdOut = "";
    if (pendingRefreshOut) *pendingRefreshOut = false;
    String mac = WiFi.macAddress();
    bool cloud = INKSIGHT_CLOUD_ENABLED;
    if (!cloud && !ensureDeviceToken()) return false;
    float v = readBatteryVoltage();
    int rssi = WiFi.RSSI();

    String url;
    if (cloud) {
        // 云端模式：坚果云 WebDAV 文件 <BASE>/<MAC去冒号>.json（Basic Auth）
        // ⚠️ 坚果云拒绝文件名含 ':'（HTTP 400），必须去冒号：AA:BB:CC:DD:EE:FF → AABBCCDDEEFF
        String macNoColon = mac;
        macNoColon.replace(":", "");
        url = String(CLOUD_BASE_URL) + "/" + macNoColon + ".json";
    } else {
        url = cfgServer + "/api/render?v=" + String(v, 2)
            + "&mac=" + mac + "&rssi=" + String(rssi)
            + "&refresh_min=" + String(cfgSleepMin)
            + "&w=" + String(W) + "&h=" + String(H)
            + "&bpp=1&colors=2&fmt=structured&delta=1";
    }
    Serial.printf("GET %s\n", url.c_str());

    bool useSSL = url.startsWith("https://");
    IPAddress resolved;
    if (!resolveUrlHost(url, resolved)) return false;
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort()) return false;
        if (networkRoundExpired()) {
            markNetworkFailure(NetworkFailureStage::HTTP, "whole-round-limit");
            return false;
        }
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        bool begun = false;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            begun = http.begin(secClient, url);
        } else {
            begun = http.begin(plainClient, url);
        }
        if (!begun) {
            http.end();
            markNetworkFailure(useSSL ? NetworkFailureStage::TLS : NetworkFailureStage::HTTP,
                               "begin-failed");
            return false;
        }
        http.setReuse(false);
        http.setTimeout(HTTP_TIMEOUT);
        const char *headerKeys[] = {"X-Mode-Id", "X-Pending-Refresh", "X-Refresh-Minutes", "X-Content-Fallback"};
        http.collectHeaders(headerKeys, 4);
        http.addHeader("Accept-Encoding", "identity");
        http.addHeader("Connection", "close");
        if (cloud) {
            if (strlen(CLOUD_USER) > 0) {
                String cred = String(CLOUD_USER) + ":" + CLOUD_PASS;
                http.addHeader("Authorization", "Basic " + encodeBase64((const uint8_t *)cred.c_str(), cred.length()));
            }
        } else {
            if (cfgDeviceToken.length() > 0) http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            if (code < 0 && useSSL) {
                char tlsError[96] = {0};
                int tlsCode = secClient.lastError(tlsError, sizeof(tlsError));
                if (tlsCode != 0) {
                    markNetworkFailure(NetworkFailureStage::TLS, tlsError);
                } else {
                    markNetworkFailure(NetworkFailureStage::HTTP, http.errorToString(code).c_str());
                }
            } else {
                char detail[64];
                if (code < 0) snprintf(detail, sizeof(detail), "%s", http.errorToString(code).c_str());
                else snprintf(detail, sizeof(detail), "status=%d", code);
                markNetworkFailure(NetworkFailureStage::HTTP, detail);
            }
            http.end();
            if (cloud || !recoverDeviceTokenIfUnauthorized(code)) return false;
            continue;
        }
        if (modeIdOut) *modeIdOut = http.header("X-Mode-Id");
        if (pendingRefreshOut) *pendingRefreshOut = (http.header("X-Pending-Refresh") == "1");
        String refreshHeader = http.header("X-Refresh-Minutes");
        int serverRefreshMin = refreshHeader.toInt();
        if (serverRefreshMin >= 10 && serverRefreshMin <= 1440 && serverRefreshMin != cfgSleepMin) {
            saveSleepMin(serverRefreshMin);
        }

        int contentLen = http.getSize();
        if (contentLen <= 0 || contentLen > 65536) {
            Serial.printf("[STRUCT] bad content length %d\n", contentLen);
            http.end();
            markNetworkFailure(NetworkFailureStage::JSON, "invalid-content-length");
            return false;
        }
        uint8_t *buf = (uint8_t *)ps_malloc(contentLen + 1);
        if (!buf) {
            Serial.println("[STRUCT] PSRAM alloc failed");
            http.end();
            markNetworkFailure(NetworkFailureStage::JSON, "buffer-allocation");
            return false;
        }
        WiFiClient *stream = http.getStreamPtr();
        bool okRead = readExact(stream, buf, contentLen);
        http.end();
        if (!okRead) {
            free(buf);
            Serial.println("[STRUCT] body read failed");
            markNetworkFailure(NetworkFailureStage::HTTP, "body-read");
            return false;
        }
        buf[contentLen] = 0;
        DeserializationError err = deserializeJson(doc, buf, contentLen);
        free(buf);
        if (err) {
            Serial.printf("[STRUCT] JSON parse error: %s\n", err.c_str());
            markNetworkFailure(NetworkFailureStage::JSON, err.c_str());
            return false;
        }
        if (!doc["screen"]["widgets"].is<JsonArray>()) {
            Serial.println("[STRUCT] missing widgets array");
            markNetworkFailure(NetworkFailureStage::JSON, "schema-widgets");
            return false;
        }
        lastHeartbeatAt = millis();
        g_networkFailureStage = NetworkFailureStage::None;
        Serial.printf("[NET][DATA] valid-json elapsed=%lums\n", millis() - g_networkRoundStartedAt);
        return true;
    }
    return false;
}

// ── 真实冷启动且报价超过 24h 时请求 XAUS 补拉 ───────────────
// 深睡定时/GPIO 唤醒、软件复位、看门狗和欠压复位都不能创建
// 新请求。后端还会独立复验报价年龄与一小时冷却，避免设备端
// 状态异常时放大为供应商请求。
bool requestGoldRefreshForWake(const JsonDocument &doc) {
    if (WiFi.status() != WL_CONNECTED) return false;
    bool enabled = doc["screen"]["device_policy"]["gold_refresh"]["wake_enabled"] | true;
    if (!enabled) return false;

    esp_reset_reason_t resetReason = esp_reset_reason();
    esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
    static bool s_reasonLogged = false;
    if (!s_reasonLogged) {
        Serial.printf("[CATCHUP] reason: reset_reason=%d wake_cause=%d\n",
                      (int)resetReason, (int)wakeCause);
        s_reasonLogged = true;
    }
    bool truePowerOn = resetReason == ESP_RST_POWERON
                    && wakeCause == ESP_SLEEP_WAKEUP_UNDEFINED;
    if (!truePowerOn) return false;

    static bool s_wakeAttemptDone = false;
    if (s_wakeAttemptDone) return false;
    time_t now = time(nullptr);
    if (now < 1600000000LL) return false;

    JsonVariantConst gv = doc["screen"]["pages"]["news_gold"]["gold"];
    long long quoteTime = 0;
    bool staleOrMissing = false;
    if (gv.isNull() || !gv.is<JsonObjectConst>()) {
        staleOrMissing = true;
    } else {
        JsonObjectConst gold = gv.as<JsonObjectConst>();
        double price = gold["price_gram_cny"] | NAN;
        quoteTime = gold["quote_time"].as<long long>();
        bool validPrice = price == price && price > 0;
        staleOrMissing = !validPrice || quoteTime <= 0
                      || (long long)now - quoteTime > 86400;
    }
    if (!staleOrMissing) return false;

    Preferences prefs;
    prefs.begin("inksight", false);
    long long lastRequest = prefs.getLong("v4_cu_req", 0);
    prefs.end();
    if (lastRequest > 0 && (long long)now - lastRequest < 3600) {
        Serial.printf("[CATCHUP] device cooldown active (last request %llds ago), skip\n",
                      (long long)now - lastRequest);
        return false;
    }

    String mac = WiFi.macAddress();
    String macNoColon = mac;
    macNoColon.replace(":", "");
    static String s_requestId;
    if (s_requestId.length() == 0) {
        char rid[80];
        snprintf(rid, sizeof(rid), "wake-%lld-%08lx", (long long)now,
                 (unsigned long)esp_random());
        s_requestId = rid;
    }
    bool cloud = INKSIGHT_CLOUD_ENABLED;
    String url = cloud ? (String(CLOUD_BASE_URL) + "/requests/" + macNoColon + ".json")
                       : (cfgServer + "/api/device/" + mac + "/gold/refresh");
    JsonDocument body;
    body["type"] = "gold_refresh";
    body["request_id"] = s_requestId;
    body["requested_at"] = (long long)now;
    body["device_quote_time"] = quoteTime;
    String payload;
    serializeJson(body, payload);
    bool useSSL = url.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort() || networkRoundExpired()) return false;
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        bool begun = false;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            begun = http.begin(secClient, url);
        } else {
            begun = http.begin(plainClient, url);
        }
        if (!begun) {
            http.end();
            return false;
        }
        http.setReuse(false);
        http.setTimeout(HTTP_TIMEOUT);
        if (cloud && strlen(CLOUD_USER) > 0) {
            String cred = String(CLOUD_USER) + ":" + CLOUD_PASS;
            http.addHeader("Authorization", "Basic " + encodeBase64((const uint8_t *)cred.c_str(), cred.length()));
        } else if (!cloud && cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }
        http.addHeader("Content-Type", "application/json");
        http.addHeader("Connection", "close");
        int code = cloud ? http.PUT(payload.c_str()) : http.POST(payload.c_str());
        http.end();
        if (code == 200 || code == 201 || code == 204) {
            Preferences saved;
            saved.begin("inksight", false);
            saved.putLong("v4_cu_req", (long long)now);
            saved.end();
            s_wakeAttemptDone = true;
            Serial.printf("[CATCHUP] stale cold-start request accepted channel=%s request=%s\n",
                          cloud ? "queue" : "direct", s_requestId.c_str());
            return true;
        }
        if (code == 401 || code == 403) {
            Serial.printf("[CATCHUP] auth fail HTTP %d\n", code);
            return false;
        }
        Serial.printf("[CATCHUP] HTTP %d, bounded retry\n", code);
    }
    return false;
}

bool requestGoldCatchupIfStale(const JsonDocument &doc) {
    return requestGoldRefreshForWake(doc);
}

struct DueNewsIssue {
    bool due = false;
    String day;
    String id;
};

static bool _newsScheduleApplies(JsonArrayConst dayTypes, bool workday) {
    for (JsonVariantConst value : dayTypes) {
        const char *kind = value | "";
        if (strcmp(kind, "all") == 0) return true;
        if (workday && strcmp(kind, "workday") == 0) return true;
        if (!workday && strcmp(kind, "restday") == 0) return true;
    }
    return false;
}

static int _newsMinute(const char *value) {
    if (!value || strlen(value) != 5 || value[2] != ':') return -1;
    if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9'
        || value[3] < '0' || value[3] > '9' || value[4] < '0' || value[4] > '9') return -1;
    int hour = (value[0] - '0') * 10 + value[1] - '0';
    int minute = (value[3] - '0') * 10 + value[4] - '0';
    return hour < 24 && minute < 60 ? hour * 60 + minute : -1;
}

static DueNewsIssue _dueNewsIssue(const JsonDocument &doc) {
    DueNewsIssue out;
    if (!(doc["screen"]["device_policy"]["news_check"]["enabled"] | true)) return out;
    time_t now = time(nullptr);
    if (now < 1600000000LL) return out;
    struct tm *local = localtime(&now);
    if (!local) return out;
    char day[16];
    snprintf(day, sizeof(day), "%04d-%02d-%02d", local->tm_year + 1900,
             local->tm_mon + 1, local->tm_mday);
    bool workday = local->tm_wday >= 1 && local->tm_wday <= 5;
    JsonArrayConst days = doc["screen"]["calendar"]["days"].as<JsonArrayConst>();
    for (JsonVariantConst value : days) {
        JsonObjectConst row = value.as<JsonObjectConst>();
        if (strcmp(row["d"] | "", day) == 0 && row["workday"].is<bool>()) {
            workday = row["workday"].as<bool>();
            break;
        }
    }
    int nowMinute = local->tm_hour * 60 + local->tm_min;
    int bestMinute = -1;
    String bestId;
    JsonArrayConst schedules = doc["screen"]["device_policy"]["news_check"]
                                  ["schedules"].as<JsonArrayConst>();
    for (JsonVariantConst value : schedules) {
        JsonObjectConst row = value.as<JsonObjectConst>();
        if (!(row["enabled"] | false)) continue;
        const char *id = row["id"] | "";
        int at = _newsMinute(row["time"] | "");
        if (!id[0] || at < 0 || at > nowMinute) continue;
        if (!_newsScheduleApplies(row["day_types"].as<JsonArrayConst>(), workday)) continue;
        if (at > bestMinute || (at == bestMinute && String(id) > bestId)) {
            bestMinute = at;
            bestId = id;
        }
    }
    if (bestMinute >= 0 && bestId.length() > 0) {
        out.due = true;
        out.day = day;
        out.id = bestId;
    }
    return out;
}

bool newsCurrentIssueMissing(const JsonDocument &doc) {
    DueNewsIssue due = _dueNewsIssue(doc);
    if (!due.due) return false;
    JsonObjectConst news = doc["screen"]["pages"]["news_gold"]["news"].as<JsonObjectConst>();
    if (news.isNull() || strcmp(news["date"] | "", due.day.c_str()) != 0) return true;
    const char *actual = news["schedule_id"] | (news["period"] | "");
    return strcmp(actual, due.id.c_str()) != 0;
}

static String _safeNewsId(const String &value) {
    String out;
    for (size_t i = 0; i < value.length() && out.length() < 32; i++) {
        char c = (char)tolower((unsigned char)value[i]);
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-') out += c;
    }
    return out;
}

bool requestNewsCheckForWake(JsonDocument &doc) {
    if (WiFi.status() != WL_CONNECTED || !newsCurrentIssueMissing(doc)) return false;
    time_t now = time(nullptr);
    if (now < 1600000000LL) return false;
    DueNewsIssue due = _dueNewsIssue(doc);
    String safeId = _safeNewsId(due.id);
    if (!due.due || safeId.length() == 0) return false;
    String compactDay = due.day;
    compactDay.replace("-", "");
    String requestId = "news-" + compactDay + "-" + safeId;
    int followup = doc["screen"]["device_policy"]["news_check"]["followup_seconds"] | 300;
    followup = max(0, min(followup, 900));

    Preferences prefs;
    prefs.begin("inksight", false);
    String savedId = prefs.getString("news_req_id", "");
    long long savedAt = prefs.getLong("news_req_at", 0);
    prefs.end();
    if (savedId == requestId) {
        if (savedAt > 0 && (long long)now <= savedAt + followup) {
            doc["screen"]["pages"]["news_gold"]["news"]["device_update_state"] = "requested";
            doc["screen"]["pages"]["news_gold"]["news"]["device_update_expires_at"] = savedAt + followup;
            return true;
        }
        return false;
    }

    String mac = WiFi.macAddress();
    String macNoColon = mac;
    macNoColon.replace(":", "");
    bool cloud = INKSIGHT_CLOUD_ENABLED;
    String url = cloud ? (String(CLOUD_BASE_URL) + "/news-requests/" + macNoColon + ".json")
                       : (cfgServer + "/api/device/" + mac + "/news/check");
    JsonDocument body;
    body["type"] = "news_due_check";
    body["request_id"] = requestId;
    body["requested_at"] = (long long)now;
    body["observed_date"] = doc["screen"]["pages"]["news_gold"]["news"]["date"] | "";
    body["observed_schedule_id"] = doc["screen"]["pages"]["news_gold"]["news"]["schedule_id"] |
                                    (doc["screen"]["pages"]["news_gold"]["news"]["period"] | "");
    String payload;
    serializeJson(body, payload);
    bool useSSL = url.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort() || networkRoundExpired()) return false;
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        bool begun = useSSL ? (secClient.setCACert(ROOT_CA), http.begin(secClient, url))
                            : http.begin(plainClient, url);
        if (!begun) { http.end(); return false; }
        http.setReuse(false);
        http.setTimeout(HTTP_TIMEOUT);
        if (cloud && strlen(CLOUD_USER) > 0) {
            String cred = String(CLOUD_USER) + ":" + CLOUD_PASS;
            http.addHeader("Authorization", "Basic " + encodeBase64(
                (const uint8_t *)cred.c_str(), cred.length()));
        } else if (!cloud && cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }
        http.addHeader("Content-Type", "application/json");
        http.addHeader("Connection", "close");
        int code = cloud ? http.PUT(payload.c_str()) : http.POST(payload.c_str());
        http.end();
        if (code == 200 || code == 201 || code == 204) {
            Preferences saved;
            saved.begin("inksight", false);
            saved.putString("news_req_id", requestId);
            saved.putLong("news_req_at", (long long)now);
            saved.end();
            doc["screen"]["pages"]["news_gold"]["news"]["device_update_state"] = "requested";
            doc["screen"]["pages"]["news_gold"]["news"]["device_update_expires_at"] = (long long)now + followup;
            Serial.printf("[NEWS][CHECK] accepted channel=%s request=%s\n",
                          cloud ? "queue" : "direct", requestId.c_str());
            return true;
        }
        if (code == 401 || code == 403) {
            Serial.printf("[NEWS][CHECK] auth fail HTTP %d\n", code);
            break;
        }
        Serial.printf("[NEWS][CHECK] HTTP %d, bounded retry\n", code);
    }
    doc["screen"]["pages"]["news_gold"]["news"]["device_update_state"] = "unreachable";
    doc["screen"]["pages"]["news_gold"]["news"]["device_update_expires_at"] = (long long)now + 120;
    return false;
}
#endif // ENABLE_STRUCTURED

bool hasPendingRemoteAction(bool *shouldExitLive) {
    if (WiFi.status() != WL_CONNECTED) return false;
    if (!ensureDeviceToken()) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/state";

    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort()) return false;
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) {
                return false;
            }
            continue;
        }

        String body = http.getString();
        http.end();

        if (shouldExitLive) {
            bool intervalRequested =
                body.indexOf("\"runtime_mode\":\"interval\"") >= 0 ||
                body.indexOf("\"runtime_mode\": \"interval\"") >= 0;
            *shouldExitLive = intervalRequested;
        }

        bool pendingRefresh =
            body.indexOf("\"pending_refresh\":1") >= 0 ||
            body.indexOf("\"pending_refresh\": 1") >= 0 ||
            body.indexOf("\"pending_refresh\":true") >= 0 ||
            body.indexOf("\"pending_refresh\": true") >= 0;

        bool pendingMode =
            (body.indexOf("\"pending_mode\":\"") >= 0 || body.indexOf("\"pending_mode\": \"") >= 0) &&
            body.indexOf("\"pending_mode\":\"\"") < 0 &&
            body.indexOf("\"pending_mode\": \"\"") < 0;

        return pendingRefresh || pendingMode;
    }
    return false;
}

bool peekPendingMode(String &pendingModeOut) {
    pendingModeOut = "";
    if (WiFi.status() != WL_CONNECTED) return false;
    if (!ensureDeviceToken()) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/state";
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }

        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) return false;
            continue;
        }

        String body = http.getString();
        http.end();
        pendingModeOut = extractJsonStringField(body, "pending_mode");
        return pendingModeOut.length() > 0;
    }

    return false;
}

// ── Post config to backend ──────────────────────────────────

void postConfigToBackend() {
    if (cfgConfigJson.length() == 0) return;
    if (!ensureDeviceToken()) return;

    // Inject MAC address into the config JSON
    String mac = WiFi.macAddress();
    String body = cfgConfigJson;
    if (body.startsWith("{")) {
        body = "{\"mac\":\"" + mac + "\"," + body.substring(1);
    }

    String url = cfgServer + "/api/config";
    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        if (checkAbort()) return;
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.addHeader("Content-Type", "application/json");
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.POST(body);
        Serial.printf("POST /api/config -> %d\n", code);
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return;
        }
    }
}

// ── Post runtime mode to backend ────────────────────────────

bool postRuntimeMode(const char *mode) {
    if (INKSIGHT_CLOUD_ENABLED) return true;  // 云端模式无需向本机后端上报运行态
    if (!ensureDeviceToken()) return false;
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/runtime";
    bool useSSL = cfgServer.startsWith("https://");
    String body = String("{\"mode\":\"") + mode + "\"}";
    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.addHeader("Content-Type", "application/json");
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.POST(body);
        http.end();

        if (code == 404) {
            return true;
        }
        if (code >= 200 && code < 300) {
            return true;
        }
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return false;
        }
    }
    return false;
}

bool postVocabEvent(const char *action, const char *rating) {
    if (!ensureDeviceToken()) return false;
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/vocab/event";
    bool useSSL = cfgServer.startsWith("https://");
    String body = String("{\"action\":\"") + (action ? action : "") + "\"";
    if (rating && strlen(rating) > 0) {
        body += String(",\"rating\":\"") + rating + "\"";
    }
    body += "}";

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.addHeader("Content-Type", "application/json");
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.POST(body);
        http.end();
        Serial.printf("[VOCAB] POST %s -> %d\n", action ? action : "", code);
        if (code >= 200 && code < 300) {
            return true;
        }
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return false;
        }
    }
    return false;
}

static uint16_t readLe16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t readLe32(const uint8_t *p) {
    return (uint32_t)p[0]
        | ((uint32_t)p[1] << 8)
        | ((uint32_t)p[2] << 16)
        | ((uint32_t)p[3] << 24);
}

bool fetchVocabReviewPack(uint8_t *ratingParts, size_t partLen, int yStart, int yEnd) {
    if (!ratingParts || partLen == 0) return false;
    if (!ensureDeviceToken()) return false;

    float v = readBatteryVoltage();
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/vocab/review-pack"
               + "?v=" + String(v, 2)
               + "&w=" + String(W)
               + "&h=" + String(H)
               + "&y_start=" + String(yStart)
               + "&y_end=" + String(yEnd);
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        http.addHeader("Accept-Encoding", "identity");
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        Serial.printf("[VOCAB] GET review-pack -> %d\n", code);
        if (code != 200) {
            if (code < 0) {
                Serial.printf("[VOCAB] review-pack error: %s\n", http.errorToString(code).c_str());
            } else {
                String body = http.getString();
                Serial.printf("[VOCAB] review-pack response: %s\n", body.substring(0, 200).c_str());
            }
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) return false;
            continue;
        }

        WiFiClient *stream = http.getStreamPtr();
        uint8_t header[24];
        if (!readExact(stream, header, sizeof(header))) {
            http.end();
            return false;
        }
        if (memcmp(header, "IVP1", 4) != 0) {
            Serial.println("[VOCAB] review-pack bad magic");
            http.end();
            return false;
        }
        uint16_t packW = readLe16(header + 4);
        uint16_t packH = readLe16(header + 6);
        uint16_t packYStart = readLe16(header + 8);
        uint16_t packYEnd = readLe16(header + 10);
        uint32_t fullLen = readLe32(header + 12);
        uint32_t packPartLen = readLe32(header + 16);
        uint8_t ratingCount = header[20];
        if (packW != W || packH != H || packYStart != yStart || packYEnd != yEnd ||
            fullLen != IMG_BUF_LEN || packPartLen != partLen || ratingCount != 3) {
            Serial.printf("[VOCAB] review-pack mismatch w=%u h=%u y=%u-%u full=%u part=%u count=%u\n",
                          packW, packH, packYStart, packYEnd, fullLen, packPartLen, ratingCount);
            http.end();
            return false;
        }
        if (!readExact(stream, imgBuf, IMG_BUF_LEN)) {
            http.end();
            return false;
        }
        if (!readExact(stream, ratingParts, partLen * 3)) {
            http.end();
            return false;
        }
        http.end();
        Serial.printf("[VOCAB] review-pack OK front=%d parts=%u\n", IMG_BUF_LEN, (unsigned)(partLen * 3));
        lastHeartbeatAt = millis();
        return true;
    }
    return false;
}

bool fetchVocabAudio(AudioChunkCallback onChunk, void *userData) {
    if (!onChunk) return false;
    if (!ensureDeviceToken()) return false;
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/vocab/audio";
    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(60000);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        int contentLen = http.getSize();
        if (code == 204) {
            Serial.println("[VOCAB] audio -> 204 no content");
            http.end();
            return true;
        }
        if (code == 200) {
            WiFiClient *stream = http.getStreamPtr();
            uint8_t buffer[1024];
            size_t totalRead = 0;
            unsigned long lastDataAt = millis();
            while (http.connected() || stream->available()) {
                int available = stream->available();
                if (available <= 0) {
                    if (contentLen >= 0 && totalRead >= (size_t)contentLen) {
                        break;
                    }
                    if (millis() - lastDataAt > 3000) {
                        Serial.printf("[VOCAB] audio read timeout total=%u expected=%d connected=%d\n",
                                      (unsigned int)totalRead, contentLen, http.connected() ? 1 : 0);
                        break;
                    }
                    delay(1);
                    continue;
                }
                int readLen = stream->readBytes(buffer, min(available, (int)sizeof(buffer)));
                if (readLen > 0) {
                    totalRead += (size_t)readLen;
                    lastDataAt = millis();
                    onChunk(buffer, (size_t)readLen, userData);
                }
                if (contentLen >= 0 && totalRead >= (size_t)contentLen) {
                    break;
                }
            }
            http.end();
            return true;
        }
        if (code < 0) {
            Serial.printf("[VOCAB] audio error: %s\n", http.errorToString(code).c_str());
        } else {
            String body = http.getString();
            Serial.printf("[VOCAB] audio -> %d %s\n", code, body.substring(0, 200).c_str());
        }
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return false;
        }
    }
    return false;
}

static bool parseVoiceTurnResponse(const String &body, String &turnId, String &replyText, String &transcript, bool &exitConversation) {
    turnId = extractJsonStringField(body, "turn_id");
    replyText = extractJsonStringField(body, "reply_text");
    transcript = extractJsonStringField(body, "transcript");
    String exitStr = extractJsonStringField(body, "exit_conversation");
    exitConversation = false;
    if (exitStr == "true" || exitStr == "1") {
        exitConversation = true;
    } else if (body.indexOf("\"exit_conversation\":true") >= 0) {
        exitConversation = true;
    }
    return turnId.length() > 0;
}

bool submitVoiceTurn(const char *pcmPath, int sampleRate, int screenW, int screenH, String &turnId, String &replyText, String &transcript, bool &exitConversation) {
    turnId = "";
    replyText = "";
    transcript = "";
    exitConversation = false;
    if (!ensureDeviceToken()) return false;

    File file = LittleFS.open(pcmPath, "r");
    if (!file) {
        Serial.println("[VOICE] failed to open PCM file");
        return false;
    }

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/voice/turn"
               + "?sample_rate=" + String(sampleRate)
               + "&w=" + String(screenW)
               + "&h=" + String(screenH);
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        file.seek(0);
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.addHeader("Content-Type", "application/octet-stream");
        http.setTimeout(60000);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.sendRequest("POST", &file, file.size());
        if (code >= 200 && code < 300) {
            String body = http.getString();
            http.end();
            file.close();
            return parseVoiceTurnResponse(body, turnId, replyText, transcript, exitConversation);
        }
        if (code < 0) {
            Serial.printf("[VOICE] submit error: %s\n", http.errorToString(code).c_str());
        } else {
            String body = http.getString();
            Serial.printf("[VOICE] submit -> %d %s\n", code, body.substring(0, 300).c_str());
        }
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            file.close();
            return false;
        }
    }

    file.close();
    return false;
}

bool submitVoiceTurnBytes(const uint8_t *pcmBytes, size_t pcmSize, int sampleRate, int screenW, int screenH, String &turnId, String &replyText, String &transcript, bool &exitConversation) {
    turnId = "";
    replyText = "";
    transcript = "";
    exitConversation = false;
    if (!ensureDeviceToken()) return false;
    if (pcmBytes == NULL || pcmSize == 0) {
        Serial.println("[VOICE] empty PCM payload");
        return false;
    }

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/voice/turn"
               + "?sample_rate=" + String(sampleRate)
               + "&w=" + String(screenW)
               + "&h=" + String(screenH);
    bool useSSL = cfgServer.startsWith("https://");

    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.addHeader("Content-Type", "application/octet-stream");
        http.setTimeout(60000);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.sendRequest("POST", (uint8_t *)pcmBytes, pcmSize);
        if (code >= 200 && code < 300) {
            String body = http.getString();
            http.end();
            return parseVoiceTurnResponse(body, turnId, replyText, transcript, exitConversation);
        }
        if (code < 0) {
            Serial.printf("[VOICE] submit-bytes error: %s\n", http.errorToString(code).c_str());
        } else {
            String body = http.getString();
            Serial.printf("[VOICE] submit-bytes -> %d %s\n", code, body.substring(0, 300).c_str());
        }
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            return false;
        }
    }

    return false;
}

bool fetchVoiceAudio(const String &turnId, const char *path) {
    if (!ensureDeviceToken()) return false;
    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/voice/" + turnId + "/audio";
    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        File file = LittleFS.open(path, "w");
        if (!file) {
            Serial.println("[VOICE] failed to open audio output file");
            return false;
        }
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(60000);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code == 200) {
            WiFiClient *stream = http.getStreamPtr();
            uint8_t buffer[1024];
            while (http.connected() || stream->available()) {
                int available = stream->available();
                if (available <= 0) {
                    delay(1);
                    continue;
                }
                int readLen = stream->readBytes(buffer, min(available, (int)sizeof(buffer)));
                if (readLen > 0) {
                    file.write(buffer, readLen);
                }
            }
            http.end();
            file.close();
            return true;
        }
        if (code < 0) {
            Serial.printf("[VOICE] audio error: %s\n", http.errorToString(code).c_str());
        } else {
            String body = http.getString();
            Serial.printf("[VOICE] audio -> %d %s\n", code, body.substring(0, 200).c_str());
        }
        http.end();
        if (!recoverDeviceTokenIfUnauthorized(code)) {
            file.close();
            return false;
        }
        file.close();
    }
    return false;
}

bool fetchVoiceImage(const String &turnId) {
    if (!ensureDeviceToken()) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/voice/" + turnId + "/image";
    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            if (code < 0) {
                Serial.printf("[VOICE] image error: %s\n", http.errorToString(code).c_str());
            } else {
                String body = http.getString();
                Serial.printf("[VOICE] image -> %d %s\n", code, body.substring(0, 200).c_str());
            }
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) {
                return false;
            }
            continue;
        }

        WiFiClient *stream = http.getStreamPtr();
        uint8_t fileHeader[14];
        if (!readExact(stream, fileHeader, 14)) {
            http.end();
            return false;
        }

        uint32_t pixelOffset = fileHeader[10]
                             | ((uint32_t)fileHeader[11] << 8)
                             | ((uint32_t)fileHeader[12] << 16)
                             | ((uint32_t)fileHeader[13] << 24);
        int toSkip = (int)pixelOffset - 14;
        while (toSkip > 0 && stream->connected()) {
            if (stream->available()) {
                stream->read();
                toSkip--;
            }
        }

        uint8_t rowBuf[ROW_STRIDE];
        memset(imgBuf, 0xFF, IMG_BUF_LEN);
        for (int bmpY = 0; bmpY < H; bmpY++) {
            if (!readExact(stream, rowBuf, ROW_STRIDE)) {
                http.end();
                return false;
            }
            int dispY = H - 1 - bmpY;
            memcpy(imgBuf + dispY * ROW_BYTES, rowBuf, ROW_BYTES);
        }

        http.end();
        return true;
    }
    return false;
}

bool fetchVoiceIntroImage(int screenW, int screenH) {
    if (!ensureDeviceToken()) return false;

    String mac = WiFi.macAddress();
    String url = cfgServer + "/api/device/" + mac + "/voice/intro/image?w=" + String(screenW) + "&h=" + String(screenH);
    bool useSSL = cfgServer.startsWith("https://");
    for (int attempt = 0; attempt < 2; attempt++) {
        WiFiClient plainClient;
        WiFiClientSecure secClient;
        HTTPClient http;
        if (useSSL) {
            secClient.setCACert(ROOT_CA);
            http.begin(secClient, url);
        } else {
            http.begin(plainClient, url);
        }
        http.setTimeout(HTTP_TIMEOUT);
        if (cfgDeviceToken.length() > 0) {
            http.addHeader("X-Device-Token", cfgDeviceToken);
        }

        int code = http.GET();
        if (code != 200) {
            if (code < 0) {
                Serial.printf("[VOICE] intro image error: %s\n", http.errorToString(code).c_str());
            } else {
                String body = http.getString();
                Serial.printf("[VOICE] intro image -> %d %s\n", code, body.substring(0, 200).c_str());
            }
            http.end();
            if (!recoverDeviceTokenIfUnauthorized(code)) {
                return false;
            }
            continue;
        }

        WiFiClient *stream = http.getStreamPtr();
        uint8_t fileHeader[14];
        if (!readExact(stream, fileHeader, 14)) {
            http.end();
            return false;
        }

        uint32_t pixelOffset = fileHeader[10]
                             | ((uint32_t)fileHeader[11] << 8)
                             | ((uint32_t)fileHeader[12] << 16)
                             | ((uint32_t)fileHeader[13] << 24);
        int toSkip = (int)pixelOffset - 14;
        while (toSkip > 0 && stream->connected()) {
            if (stream->available()) {
                stream->read();
                toSkip--;
            }
        }

        uint8_t rowBuf[ROW_STRIDE];
        memset(imgBuf, 0xFF, IMG_BUF_LEN);
        for (int bmpY = 0; bmpY < H; bmpY++) {
            if (!readExact(stream, rowBuf, ROW_STRIDE)) {
                http.end();
                return false;
            }
            int dispY = H - 1 - bmpY;
            memcpy(imgBuf + dispY * ROW_BYTES, rowBuf, ROW_BYTES);
        }

        http.end();
        return true;
    }
    return false;
}

static void handleVoiceWsEvent(VoiceWsEvent &event) {
    if (gVoiceWsEventQueue == nullptr) {
        voiceWsReleaseEvent(event);
        return;
    }
    VoiceWsEvent *heapEvent = new VoiceWsEvent(event);
    if (heapEvent == nullptr) {
        voiceWsReleaseEvent(event);
        return;
    }
    if (xQueueSend(gVoiceWsEventQueue, &heapEvent, 0) != pdTRUE) {
        delete heapEvent;
        voiceWsReleaseEvent(event);
    }
}

static void voiceWsSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            gVoiceWsConnected = true;
            gVoiceWsDisconnected = false;
            gVoiceWsConnectedAt = millis();
            Serial.printf("[VOICE_WS] connected (heap=%u)\n", ESP.getFreeHeap());
            return;
        case WStype_DISCONNECTED: {
            bool wasConnected = gVoiceWsConnected;
            gVoiceWsConnected = false;
            gVoiceWsDisconnected = true;
            if (gVoiceWsConnectedAt > 0) {
                Serial.printf("[VOICE_WS] disconnected after %lu ms\n", millis() - gVoiceWsConnectedAt);
            } else if (gVoiceWsOpenStartedAt > 0) {
                Serial.printf("[VOICE_WS] disconnected before connected after %lu ms\n", millis() - gVoiceWsOpenStartedAt);
            } else {
                Serial.println("[VOICE_WS] disconnected before connected");
            }
            if (payload != nullptr && length > 0) {
                Serial.printf("[VOICE_WS] disconnected payload len=%u: ", (unsigned)length);
                for (size_t i = 0; i < length; i++) Serial.print((char)payload[i]);
                Serial.println();
            } else {
                Serial.println("[VOICE_WS] disconnected (no payload)");
            }
            if (wasConnected) {
                gVoiceWsConnectedAt = 0;
            }
            return;
        }
        case WStype_BIN: {
            if (payload == nullptr || length == 0) return;
            VoiceWsEvent event;
            event.type = VoiceWsEventType::TtsAudioChunk;
            event.sampleRate = 16000;
            event.needsDecode = gVoiceWsOpusEnabled;
            event.data = (uint8_t *)malloc(length);
            if (event.data == nullptr) return;
            memcpy(event.data, payload, length);
            event.dataLen = length;
            handleVoiceWsEvent(event);
            return;
        }
        case WStype_TEXT:
            Serial.printf("[VOICE_WS] recv text len=%u: %.120s\n", (unsigned)length, payload ? (char *)payload : "");
            break;
        case WStype_ERROR:
            if (payload && length > 0) {
                Serial.printf("[VOICE_WS] socket error: %.*s\n", (int)length, (char *)payload);
            } else {
                Serial.println("[VOICE_WS] socket error (no payload)");
            }
            return;
        default:
            return;
    }

    String body;
    body.reserve(length + 1);
    for (size_t i = 0; i < length; i++) {
        body += (char)payload[i];
    }
    String eventName = extractJsonStringField(body, "event");
    if (eventName.length() == 0) {
        return;
    }

    VoiceWsEvent event;
    if (eventName == "session.ready") {
        event.type = VoiceWsEventType::SessionReady;
        String binaryAudio = extractJsonBoolField(body, "binary_audio");
        gVoiceWsBinaryAudioEnabled = (binaryAudio == "true" || binaryAudio == "1");
        String codec = extractJsonStringField(body, "audio_codec");
        gVoiceWsOpusEnabled = (codec == "opus");
        String serverVad = extractJsonBoolField(body, "server_vad");
        gVoiceWsServerVadEnabled = (serverVad == "true" || serverVad == "1");
    } else if (eventName == "asr.partial") {
        event.type = VoiceWsEventType::AsrPartial;
        event.text = extractJsonStringField(body, "text");
    } else if (eventName == "asr.final") {
        event.type = VoiceWsEventType::AsrFinal;
        event.transcript = extractJsonStringField(body, "transcript");
    } else if (eventName == "llm.delta") {
        event.type = VoiceWsEventType::LlmDelta;
        event.text = extractJsonStringField(body, "delta");
        event.generationId = extractJsonIntField(body, "generation_id", 0);
    } else if (eventName == "tts.text_chunk") {
        event.type = VoiceWsEventType::TtsTextChunk;
        event.text = extractJsonStringField(body, "text");
        event.generationId = extractJsonIntField(body, "generation_id", 0);
        event.chunkId = extractJsonIntField(body, "chunk_id", 0);
    } else if (eventName == "tts.audio_chunk") {
        event.type = VoiceWsEventType::TtsAudioChunk;
        event.generationId = extractJsonIntField(body, "generation_id", 0);
        event.chunkId = extractJsonIntField(body, "chunk_id", 0);
        event.sampleRate = extractJsonIntField(body, "sample_rate", 16000);
        if (!decodeBase64ToHeap(extractJsonStringField(body, "audio"), &event.data, &event.dataLen)) {
            Serial.println("[VOICE_WS] failed to decode audio chunk");
            return;
        }
    } else if (eventName == "turn.done") {
        event.type = VoiceWsEventType::TurnDone;
        event.turnId = extractJsonStringField(body, "turn_id");
        event.transcript = extractJsonStringField(body, "transcript");
        event.text = extractJsonStringField(body, "reply_text");
        event.generationId = extractJsonIntField(body, "generation_id", 0);
        String exitFlag = extractJsonBoolField(body, "exit_conversation");
        event.exitConversation = (exitFlag == "true" || exitFlag == "1");
        event.switchToMode = extractJsonStringField(body, "switch_to_mode");
        if (!decodeBase64ToHeap(extractJsonStringField(body, "image"), &event.data, &event.dataLen)) {
            Serial.println("[VOICE_WS] failed to decode image payload");
            return;
        }
    } else if (eventName == "turn.interrupted") {
        event.type = VoiceWsEventType::TurnInterrupted;
        event.generationId = extractJsonIntField(body, "generation_id", 0);
    } else if (eventName == "error") {
        event.type = VoiceWsEventType::Error;
        event.text = extractJsonStringField(body, "message");
    } else {
        return;
    }
    handleVoiceWsEvent(event);
}

bool voiceWsOpen(int sampleRate, int screenW, int screenH, bool includeImage) {
    Serial.printf("[VOICE_WS] free heap before open: %u\n", ESP.getFreeHeap());
    if (WiFi.status() != WL_CONNECTED) return false;
    if (!ensureDeviceToken()) return false;

    if (gVoiceWsEventQueue == nullptr) {
        gVoiceWsEventQueue = xQueueCreate(12, sizeof(VoiceWsEvent *));
        if (gVoiceWsEventQueue == nullptr) {
            return false;
        }
    }

    bool useSSL = false;
    String host;
    uint16_t port = 0;
    String basePath;
    if (!parseWsServerUrl(cfgServer, useSSL, host, port, basePath)) {
        Serial.println("[VOICE_WS] invalid server url");
        return false;
    }

    String mac = WiFi.macAddress();
    String path = basePath + "/api/device/" + mac + "/voice/ws?token=" + cfgDeviceToken;
    String extraHeaders = String("X-Device-Token: ") + cfgDeviceToken;
    String startMsg = String("{\"type\":\"session.start\",\"sample_rate\":") + sampleRate
                    + ",\"w\":" + screenW
                    + ",\"h\":" + screenH
                    + ",\"include_image\":" + (includeImage ? "true" : "false")
                    + ",\"protocol\":2"
#if ENABLE_OPUS
                    + ",\"audio_codec\":\"opus\""
#else
                    + ",\"audio_codec\":\"pcm\""
#endif
                    + "}";

    for (int attempt = 1; attempt <= 3; attempt++) {
        voiceWsClose();
        gVoiceWsConnected = false;
        gVoiceWsDisconnected = false;
        gVoiceWsBinaryAudioEnabled = false;
        gVoiceWsOpusEnabled = false;
        gVoiceWsServerVadEnabled = false;
        gVoiceWsOpenStartedAt = millis();
        gVoiceWsConnectedAt = 0;

        gVoiceWsClient.onEvent(voiceWsSocketEvent);
        gVoiceWsClient.setReconnectInterval(60000);
        gVoiceWsClient.setExtraHeaders(extraHeaders.c_str());

        Serial.printf("[VOICE_WS] attempt %d/3 %s://%s:%u%s\n",
            attempt, useSSL ? "wss" : "ws", host.c_str(), port, path.c_str());

        if (useSSL) {
            gVoiceWsClient.beginSSL(host.c_str(), port, path.c_str(), nullptr, "");
        } else {
            gVoiceWsClient.begin(host.c_str(), port, path.c_str(), "");
        }
        gVoiceWsConfigured = true;

        unsigned long startAt = millis();
        while (!gVoiceWsConnected && !gVoiceWsDisconnected && millis() - startAt < 15000UL) {
            gVoiceWsClient.loop();
            delay(10);
        }
        if (!gVoiceWsConnected) {
            Serial.printf("[VOICE_WS] attempt %d failed: %s\n", attempt,
                gVoiceWsDisconnected ? "disconnected before connected" : "connect timeout");
            voiceWsClose();
            delay(500);
            continue;
        }

        // Stabilize: loop for 500ms, bail if connection drops.
        bool stable = true;
        unsigned long stableStart = millis();
        while (millis() - stableStart < 500UL) {
            gVoiceWsClient.loop();
            if (!gVoiceWsConnected) {
                Serial.printf("[VOICE_WS] attempt %d failed: dropped during stabilization\n", attempt);
                stable = false;
                break;
            }
            delay(10);
        }
        if (!stable) {
            voiceWsClose();
            delay(500);
            continue;
        }

        Serial.printf("[VOICE_WS] sending session.start len=%u\n", (unsigned)startMsg.length());
        if (!gVoiceWsClient.sendTXT(startMsg)) {
            Serial.printf("[VOICE_WS] attempt %d failed: sendTXT returned false\n", attempt);
            voiceWsClose();
            delay(500);
            continue;
        }
        Serial.println("[VOICE_WS] session.start sent OK");

        // Flush: loop for 500ms to ensure the frame is sent.
        bool flushed = true;
        unsigned long flushStart = millis();
        while (millis() - flushStart < 500UL) {
            gVoiceWsClient.loop();
            if (!gVoiceWsConnected) {
                Serial.printf("[VOICE_WS] attempt %d failed: dropped after session.start\n", attempt);
                flushed = false;
                break;
            }
            delay(10);
        }
        if (!flushed) {
            voiceWsClose();
            delay(500);
            continue;
        }

        gVoiceWsClient.enableHeartbeat(15000, 3000, 2);
        Serial.printf("[VOICE_WS] attempt %d connected successfully\n", attempt);
        return true;
    }

    Serial.println("[VOICE_WS] all 3 attempts failed");
    return false;
}

bool voiceWsConnected() {
    return gVoiceWsConnected;
}

void voiceWsLoop() {
    if (gVoiceWsConfigured) {
        gVoiceWsClient.loop();
    }
}

bool voiceWsSendAudioBin(const int16_t *samples, size_t sampleCount) {
    if (!gVoiceWsConnected || samples == nullptr || sampleCount == 0) return false;
#if ENABLE_OPUS
    if (gVoiceWsOpusEnabled) {
        uint8_t opusBuf[OPUS_MAX_PACKET_BYTES];
        int encoded = opusEncode(samples, sampleCount, opusBuf, sizeof(opusBuf));
        if (encoded > 0) {
            return gVoiceWsClient.sendBIN(opusBuf, encoded);
        }
        return false;
    }
#endif
    return gVoiceWsClient.sendBIN((const uint8_t *)samples, sampleCount * sizeof(int16_t));
}

bool voiceWsSendAudioChunk(const int16_t *samples, size_t sampleCount) {
    if (!gVoiceWsConnected || samples == nullptr || sampleCount == 0) return false;
    if (gVoiceWsBinaryAudioEnabled) {
        return voiceWsSendAudioBin(samples, sampleCount);
    }
    String audio = encodeBase64((const uint8_t *)samples, sampleCount * sizeof(int16_t));
    if (audio.length() == 0) {
        return false;
    }
    String msg = String("{\"type\":\"audio.append\",\"audio\":\"") + audio + "\"}";
    return gVoiceWsClient.sendTXT(msg);
}

bool voiceWsSendRawPacket(const uint8_t *data, size_t len) {
    if (!gVoiceWsConnected || data == nullptr || len == 0) return false;
    if (gVoiceWsBinaryAudioEnabled) {
        return gVoiceWsClient.sendBIN(data, len);
    }
    String audio = encodeBase64(data, len);
    if (audio.length() == 0) return false;
    String msg = String("{\"type\":\"audio.append\",\"audio\":\"") + audio + "\"}";
    return gVoiceWsClient.sendTXT(msg);
}

bool voiceWsBinaryAudio() {
    return gVoiceWsBinaryAudioEnabled;
}

bool voiceWsServerVad() {
    return gVoiceWsServerVadEnabled;
}

bool voiceWsCommitTurn() {
    if (!gVoiceWsConnected) return false;
    return gVoiceWsClient.sendTXT("{\"type\":\"audio.commit\"}");
}

bool voiceWsInterrupt() {
    if (!gVoiceWsConnected) return false;
    return gVoiceWsClient.sendTXT("{\"type\":\"interrupt\"}");
}

bool voiceWsPollEvent(VoiceWsEvent &eventOut) {
    if (gVoiceWsEventQueue == nullptr) return false;
    VoiceWsEvent *heapEvent = nullptr;
    if (xQueueReceive(gVoiceWsEventQueue, &heapEvent, 0) != pdTRUE || heapEvent == nullptr) {
        return false;
    }
    eventOut = *heapEvent;
    delete heapEvent;
    return true;
}

void voiceWsReleaseEvent(VoiceWsEvent &event) {
    if (event.data != nullptr) {
        free(event.data);
        event.data = nullptr;
    }
    event.dataLen = 0;
    event.text = "";
    event.transcript = "";
    event.turnId = "";
    event.type = VoiceWsEventType::None;
}

void voiceWsClose() {
    if (gVoiceWsConfigured) {
        if (gVoiceWsConnected) {
            gVoiceWsClient.sendTXT("{\"type\":\"close\"}");
        }
        gVoiceWsClient.disconnect();
    }
    gVoiceWsConfigured = false;
    gVoiceWsConnected = false;
    gVoiceWsDisconnected = false;
    gVoiceWsOpenStartedAt = 0;
    gVoiceWsConnectedAt = 0;
    gVoiceWsBinaryAudioEnabled = false;
    gVoiceWsOpusEnabled = false;
    gVoiceWsServerVadEnabled = false;
    if (gVoiceWsEventQueue != nullptr) {
        VoiceWsEvent *heapEvent = nullptr;
        while (xQueueReceive(gVoiceWsEventQueue, &heapEvent, 0) == pdTRUE) {
            if (heapEvent != nullptr) {
                voiceWsReleaseEvent(*heapEvent);
                delete heapEvent;
            }
        }
    }
}

// ── NTP time sync ───────────────────────────────────────────

void syncNTP(const char *server1, const char *server2, const char *server3) {
    const char *s1 = (server1 && server1[0]) ? server1 : "ntp.aliyun.com";
    const char *s2 = (server2 && server2[0]) ? server2 : "pool.ntp.org";
    configTime(NTP_UTC_OFFSET, 0, s1, s2, (server3 && server3[0]) ? server3 : nullptr);
    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 5000)) {
        curHour = timeinfo.tm_hour;
        curMin  = timeinfo.tm_min;
        curSec  = timeinfo.tm_sec;
        Serial.printf("NTP synced: %02d:%02d:%02d\n", curHour, curMin, curSec);
    } else {
        curHour = 0; curMin = 0; curSec = 0;
        Serial.println("NTP failed, using 00:00:00");
    }
}

// ── Software clock tick ─────────────────────────────────────

void tickTime() {
    curSec++;
    if (curSec >= 60) { curSec = 0; curMin++; }
    if (curMin >= 60) { curMin = 0; curHour++; }
    if (curHour >= 24) { curHour = 0; }
}
