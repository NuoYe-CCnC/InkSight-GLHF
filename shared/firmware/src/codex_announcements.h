#pragma once
#include <ArduinoJson.h>
#include <math.h>
#include <string.h>
#include <time.h>

// Optional v1 snapshot; untrusted/malformed data never affects actual usage.
inline double announcementNumber(JsonVariantConst v) {
    if (v.is<bool>() || !(v.is<double>() || v.is<long long>())) return NAN;
    return v.as<double>();
}

inline time_t codexAnnouncementTarget(JsonObjectConst ai, const char *kind, time_t now) {
    JsonObjectConst snapshot = ai["announcements"].as<JsonObjectConst>();
    if (!snapshot["schema_version"].is<int>() || snapshot["schema_version"].as<int>() != 1 ||
        !snapshot["enabled"].is<bool>() || !snapshot["enabled"].as<bool>()) return 0;
    JsonObjectConst event = snapshot[kind].as<JsonObjectConst>();
    if (strcmp(event["kind"] | "", kind) || strcmp(event["status"] | "", "pending") ||
        strcmp(event["confidence"] | "", "explicit")) return 0;
    double target = announcementNumber(event["target_at"]);
    double expiry = announcementNumber(event["expires_at"]);
    double valid = announcementNumber(event["valid_until"]);
    double published = announcementNumber(event["published_at"]);
    if (!isfinite(target) || !isfinite(expiry) || !isfinite(valid) || !isfinite(published) ||
        !(0 < published && published <= now && now < valid && valid <= target &&
          target == expiry && target <= (double)now + 14 * 86400)) return 0;
    return (time_t)target;
}

inline time_t codexNearestAnnouncement(JsonObjectConst ai, time_t now, bool showReset, const char **kind) {
    const char *keys[] = {"quota_reset", "reset_grant", "reset_unspecified"};
    time_t nearest = 0;
    *kind = nullptr;
    for (const char *key : keys) {
        if (!showReset && !strcmp(key, "reset_grant")) continue;
        time_t target = codexAnnouncementTarget(ai, key, now);
        if (target && (!nearest || target < nearest || (target == nearest &&
            announcementNumber(ai["announcements"][key]["published_at"]) >
            announcementNumber(ai["announcements"][*kind]["published_at"])))) {
            nearest = target; *kind = key;
        }
    }
    return nearest;
}
