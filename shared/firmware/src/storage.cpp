#include "storage.h"
#include "config.h"
#include <Preferences.h>
#include <string.h>

static Preferences prefs;
static const char *LIVE_BOOT_MARKER = __DATE__ " " __TIME__;
static const char *KEY_LIVE_BOOT_MARKER_NEW = "live_boot_mk";     // <= 15 chars
static const char *KEY_LIVE_BOOT_MARKER_OLD = "live_boot_marker"; // old invalid key, read-compat only

// Config version — bump when NVS schema changes
static const int CONFIG_VERSION = 2;

// ── Runtime config variables ────────────────────────────────
String cfgSSID;
String cfgPass;
String cfgServer;
int    cfgSleepMin;
String cfgConfigJson;
String cfgDeviceToken;
String cfgPendingPairCode;

// ── Multi-WiFi credential list (in-memory, mirrors NVS) ─────
static String g_wifiSsids[MAX_WIFI_NETWORKS];
static String g_wifiPass[MAX_WIFI_NETWORKS];
static int    g_wifiCount = 0;
static void persistWiFiList(Preferences &p);

// 编译期多网络预置（EXTRA_WIFI = "SSID2~PASS2^SSID3~PASS3"，'~' 字段分隔、'^' 记录分隔）：
// 把 DEFAULT_SSID 之外的家/公司网络并入列表（去重，尊重已有条目，仅追加到满为止）。
static void seedExtraWifi() {
    const char *s = EXTRA_WIFI;
    if (!s || !*s) return;
    while (*s && g_wifiCount < MAX_WIFI_NETWORKS) {
        const char *tilde = strchr(s, '~');
        const char *caret = strchr(s, '^');
        if (!tilde) break;
        const char *pend = caret ? caret : s + strlen(s);
        if (pend <= tilde) break;  // 畸形段，放弃后续
        String ssid(s, tilde - s);
        String pass(tilde + 1, pend - tilde - 1);
        bool dup = false;
        for (int i = 0; i < g_wifiCount; i++) {
            if (g_wifiSsids[i] == ssid) { dup = true; break; }
        }
        if (!dup && ssid.length() > 0 && g_wifiCount < MAX_WIFI_NETWORKS) {
            g_wifiSsids[g_wifiCount] = ssid;
            g_wifiPass[g_wifiCount]  = pass;
            g_wifiCount++;
        }
        if (!caret) break;
        s = caret + 1;
    }
}

static bool promoteCompiledNetwork(const String &ssid, const String &pass) {
    if (ssid.length() == 0) return false;
    int existing = -1;
    for (int i = 0; i < g_wifiCount; i++) {
        if (g_wifiSsids[i] == ssid) { existing = i; break; }
    }
    bool samePassword = existing >= 0 && (pass.length() == 0 || g_wifiPass[existing] == pass);
    if (existing == 0 && samePassword) return false;

    String effectivePass = pass;
    if (effectivePass.length() == 0 && existing >= 0) effectivePass = g_wifiPass[existing];
    if (existing >= 0) {
        for (int i = existing; i > 0; i--) {
            g_wifiSsids[i] = g_wifiSsids[i - 1];
            g_wifiPass[i] = g_wifiPass[i - 1];
        }
    } else {
        int last = min(g_wifiCount, MAX_WIFI_NETWORKS - 1);
        for (int i = last; i > 0; i--) {
            g_wifiSsids[i] = g_wifiSsids[i - 1];
            g_wifiPass[i] = g_wifiPass[i - 1];
        }
        if (g_wifiCount < MAX_WIFI_NETWORKS) g_wifiCount++;
    }
    g_wifiSsids[0] = ssid;
    g_wifiPass[0] = effectivePass;
    return true;
}

// A newly built firmware carries one authoritative Wi-Fi snapshot. Apply it
// once per build ID so omitted old networks are removed, while later captive
// portal edits remain persistent across ordinary reboots of the same build.
static bool applyCompiledWifiSnapshot(Preferences &p) {
    if (strlen(DEFAULT_SSID) == 0) return false;
    String appliedBuild = p.getString("wifi_build", "");
    if (appliedBuild == String(INKSIGHT_BUILD_ID)) return false;
    String desiredSsid[MAX_WIFI_NETWORKS];
    String desiredPass[MAX_WIFI_NETWORKS];
    int desiredCount = 0;
    if (strlen(DEFAULT_SSID) > 0) {
        desiredSsid[desiredCount] = DEFAULT_SSID;
        desiredPass[desiredCount] = DEFAULT_PASS;
        desiredCount++;
    }
    const char *s = EXTRA_WIFI;
    while (s && *s && desiredCount < MAX_WIFI_NETWORKS) {
        const char *tilde = strchr(s, '~');
        const char *caret = strchr(s, '^');
        if (!tilde) break;
        const char *end = caret ? caret : s + strlen(s);
        if (end <= tilde) break;
        desiredSsid[desiredCount] = String(s, tilde - s);
        desiredPass[desiredCount] = String(tilde + 1, end - tilde - 1);
        if (desiredSsid[desiredCount].length() > 0) desiredCount++;
        if (!caret) break;
        s = caret + 1;
    }
    for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
        g_wifiSsids[i] = "";
        g_wifiPass[i] = "";
    }
    g_wifiCount = desiredCount;
    for (int i = 0; i < desiredCount; i++) {
        g_wifiSsids[i] = desiredSsid[i];
        g_wifiPass[i] = desiredPass[i];
    }
    p.putString("wifi_build", INKSIGHT_BUILD_ID);
    return true;
}

// Build NVS key "wifi_sN" / "wifi_pN" (kind = 's' or 'p').
static String wifiKey(char kind, int idx) {
    char buf[12];
    snprintf(buf, sizeof(buf), "wifi_%c%d", kind, idx);
    return String(buf);
}

// Persist the in-memory list to NVS (assumes prefs already open read-write).
static void persistWiFiList(Preferences &p) {
    p.putInt("wifi_n", g_wifiCount);
    for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
        if (i < g_wifiCount) {
            p.putString(wifiKey('s', i).c_str(), g_wifiSsids[i]);
            p.putString(wifiKey('p', i).c_str(), g_wifiPass[i]);
        } else {
            p.remove(wifiKey('s', i).c_str());
            p.remove(wifiKey('p', i).c_str());
        }
    }
    // Keep legacy single-SSID keys in sync with slot 0 (back-compat).
    if (g_wifiCount > 0) {
        p.putString("ssid", g_wifiSsids[0]);
        p.putString("pass", g_wifiPass[0]);
    } else {
        p.remove("ssid");
        p.remove("pass");
    }
}

// Mirror slot 0 into the legacy cfgSSID/cfgPass runtime vars used downstream.
static void syncPrimaryRuntime() {
    if (g_wifiCount > 0) {
        cfgSSID = g_wifiSsids[0];
        cfgPass = g_wifiPass[0];
    } else {
        cfgSSID = "";
        cfgPass = "";
    }
}


// ── Load config from NVS ────────────────────────────────────

void loadConfig() {
    prefs.begin("inksight", false);  // read-write：命名空间不存在时自动创建（readonly 首启必 NOT_FOUND）

    int version = prefs.getInt("cfg_version", 0);
    if (version != CONFIG_VERSION) {
        Serial.printf("Config version mismatch (%d != %d), using defaults\n",
                      version, CONFIG_VERSION);
        cfgSSID = DEFAULT_SSID;
        cfgPass = DEFAULT_PASS;
        cfgServer = DEFAULT_SERVER;
        cfgSleepMin = 60;
        cfgConfigJson = "";
        cfgDeviceToken = "";
        cfgPendingPairCode = "";
        g_wifiCount = 0;
        if (cfgSSID.length() > 0) {  // 编译期预置 WiFi
            g_wifiSsids[0] = cfgSSID;
            g_wifiPass[0]  = cfgPass;
            g_wifiCount = 1;
        }
        seedExtraWifi();  // 编译期预置的备用网络（家/公司），仅首次安装（版本不匹配=全新 NVS）
        // 首启落盘默认值：否则每次开机都走 mismatch（readonly 还读不到），v4 状态也无法持久化
        prefs.putInt("cfg_version", CONFIG_VERSION);
        prefs.putString("wifi_build", INKSIGHT_BUILD_ID);
        persistWiFiList(prefs);
        prefs.end();
        syncPrimaryRuntime();
        return;
    }

    cfgSSID         = prefs.getString("ssid", "");
    cfgPass         = prefs.getString("pass", "");
    cfgServer       = prefs.getString("server", DEFAULT_SERVER);
    // 编译期默认值兜底（NVS 未配置时）
    if (cfgSSID.length() == 0) cfgSSID = DEFAULT_SSID;
    if (cfgPass.length() == 0) cfgPass = DEFAULT_PASS;
    cfgSleepMin     = prefs.getInt("sleep_min", 60);
    cfgConfigJson   = prefs.getString("config_json", "");
    cfgDeviceToken  = prefs.getString("device_token", "");
    cfgPendingPairCode = prefs.getString("pair_code", "");

    // ── Load multi-WiFi list ────────────────────────────────
    int wifiN = prefs.getInt("wifi_n", -1);
    bool needMigration = false;
    g_wifiCount = 0;
    if (wifiN >= 0) {
        if (wifiN > MAX_WIFI_NETWORKS) wifiN = MAX_WIFI_NETWORKS;
        for (int i = 0; i < wifiN; i++) {
            String s = prefs.getString(wifiKey('s', i).c_str(), "");
            if (s.length() == 0) continue;  // skip corrupt/empty slot
            g_wifiSsids[g_wifiCount] = s;
            g_wifiPass[g_wifiCount]  = prefs.getString(wifiKey('p', i).c_str(), "");
            g_wifiCount++;
        }
    } else if (cfgSSID.length() > 0) {
        // Legacy single-SSID config -> migrate into slot 0.
        g_wifiSsids[0] = cfgSSID;
        g_wifiPass[0]  = cfgPass;
        g_wifiCount = 1;
        needMigration = true;
    }
    // 编译期预置 WiFi：列表为空但有默认 SSID 时写入 slot 0
    if (g_wifiCount == 0 && cfgSSID.length() > 0) {
        g_wifiSsids[0] = cfgSSID;
        g_wifiPass[0]  = cfgPass;
        g_wifiCount = 1;
    }
    if (g_wifiCount == 0) {
        seedExtraWifi();  // 连默认 SSID 都没有但带了 EXTRA_WIFI 的情况
    } else if (needMigration) {
        seedExtraWifi();  // 旧版单网络迁移：把编译期备用网络一并并入
    }
    bool priorityChanged = applyCompiledWifiSnapshot(prefs);
    prefs.end();

    syncPrimaryRuntime();

    if (needMigration || priorityChanged) {
        prefs.begin("inksight", false);  // read-write
        prefs.putInt("cfg_version", CONFIG_VERSION);
        persistWiFiList(prefs);
        prefs.end();
        Serial.printf("Applied compiled WiFi priority (%d entries, migrated=%d)\n",
                      g_wifiCount, needMigration ? 1 : 0);
    }

    // Sanity checks
    if (cfgSleepMin < 10 || cfgSleepMin > 1440) {
        cfgSleepMin = 60;
    }
    if (cfgServer.length() > 200) {
        cfgServer = DEFAULT_SERVER;
    }
}

// ── Retry counter ───────────────────────────────────────────

int getRetryCount() {
    prefs.begin("inksight", true);
    int count = prefs.getInt("retry_count", 0);
    prefs.end();
    return count;
}

void setRetryCount(int count) {
    prefs.begin("inksight", false);
    prefs.putInt("retry_count", count);
    prefs.end();
}

void resetRetryCount() {
    setRetryCount(0);
}

bool isFirstInstallLiveModePending() {
    prefs.begin("inksight", true);
    String marker = prefs.getString(KEY_LIVE_BOOT_MARKER_NEW, "");
    if (marker.length() == 0) {
        marker = prefs.getString(KEY_LIVE_BOOT_MARKER_OLD, "");
    }
    prefs.end();
    return marker != String(LIVE_BOOT_MARKER);
}

void markFirstInstallLiveModeDone() {
    prefs.begin("inksight", false);
    prefs.putString(KEY_LIVE_BOOT_MARKER_NEW, LIVE_BOOT_MARKER);
    prefs.end();
}

// ── Save WiFi credentials ───────────────────────────────────

// Set as the primary network (slot 0). Back-compat wrapper over addWiFiConfig.
void saveWiFiConfig(const String &ssid, const String &pass) {
    addWiFiConfig(ssid, pass);
}

// ── Multi-WiFi credential list ──────────────────────────────

int getWiFiCount() {
    return g_wifiCount;
}

bool getWiFiAt(int idx, String &ssid, String &pass) {
    if (idx < 0 || idx >= g_wifiCount) return false;
    ssid = g_wifiSsids[idx];
    pass = g_wifiPass[idx];
    return true;
}

void getWiFiSSIDList(String out[], int &count) {
    count = g_wifiCount;
    for (int i = 0; i < g_wifiCount; i++) {
        out[i] = g_wifiSsids[i];
    }
}

bool addWiFiConfig(const String &ssid, const String &pass) {
    if (ssid.length() == 0) return false;

    // Existing SSID: update password and move to front (slot 0).
    int existing = -1;
    for (int i = 0; i < g_wifiCount; i++) {
        if (g_wifiSsids[i] == ssid) { existing = i; break; }
    }
    if (existing >= 0) {
        for (int i = existing; i > 0; i--) {
            g_wifiSsids[i] = g_wifiSsids[i - 1];
            g_wifiPass[i]  = g_wifiPass[i - 1];
        }
        g_wifiSsids[0] = ssid;
        g_wifiPass[0]  = pass;
    } else {
        if (g_wifiCount >= MAX_WIFI_NETWORKS) return false;  // list full
        // Append new network to the end; tried after earlier-saved ones.
        g_wifiSsids[g_wifiCount] = ssid;
        g_wifiPass[g_wifiCount]  = pass;
        g_wifiCount++;
    }

    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    persistWiFiList(prefs);
    prefs.end();
    syncPrimaryRuntime();
    return true;
}

bool deleteWiFiBySSID(const String &ssid) {
    int idx = -1;
    for (int i = 0; i < g_wifiCount; i++) {
        if (g_wifiSsids[i] == ssid) { idx = i; break; }
    }
    if (idx < 0) return false;
    for (int i = idx; i < g_wifiCount - 1; i++) {
        g_wifiSsids[i] = g_wifiSsids[i + 1];
        g_wifiPass[i]  = g_wifiPass[i + 1];
    }
    g_wifiCount--;
    g_wifiSsids[g_wifiCount] = "";
    g_wifiPass[g_wifiCount]  = "";

    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    persistWiFiList(prefs);
    prefs.end();
    syncPrimaryRuntime();
    return true;
}

// ── Save server URL ─────────────────────────────────────────

void saveServerUrl(const String &url) {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.putString("server", url);
    prefs.end();
    cfgServer = url;
}

// ── Save user config JSON ───────────────────────────────────

void saveUserConfig(const String &configJson) {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.putString("config_json", configJson);

    // Extract refreshInterval from JSON and persist as sleep_min
    int idx = configJson.indexOf("\"refreshInterval\"");
    if (idx >= 0) {
        int colon = configJson.indexOf(':', idx);
        if (colon >= 0) {
            int val = configJson.substring(colon + 1).toInt();
            if (val < 10)   val = 10;    // minimum 10 minutes
            if (val > 1440)  val = 1440;  // maximum 24 hours
            prefs.putInt("sleep_min", val);
            cfgSleepMin = val;
            Serial.printf("refreshInterval -> sleep_min = %d min\n", val);
        }
    }

    prefs.end();
    cfgConfigJson = configJson;
}

void saveSleepMin(int minutes) {
    if (minutes < 10) minutes = 10;
    if (minutes > 1440) minutes = 1440;
    if (cfgSleepMin == minutes) return;
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.putInt("sleep_min", minutes);
    prefs.end();
    cfgSleepMin = minutes;
}

// ── Device token ────────────────────────────────────────────

void saveDeviceToken(const String &token) {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.putString("device_token", token);
    prefs.end();
    cfgDeviceToken = token;
}

void clearDeviceToken() {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.remove("device_token");
    prefs.end();
    cfgDeviceToken = "";
}

void savePendingPairCode(const String &code) {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.putString("pair_code", code);
    prefs.end();
    cfgPendingPairCode = code;
}

void clearPendingPairCode() {
    prefs.begin("inksight", false);
    prefs.putInt("cfg_version", CONFIG_VERSION);
    prefs.remove("pair_code");
    prefs.end();
    cfgPendingPairCode = "";
}
