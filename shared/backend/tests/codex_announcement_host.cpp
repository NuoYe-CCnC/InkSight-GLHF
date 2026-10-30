#include "codex_announcements.h"
#include <iostream>
#include <iterator>
int main() {
    const std::string input(std::istreambuf_iterator<char>(std::cin), {});
    JsonDocument doc;
    if (deserializeJson(doc, input)) return 2;
    const time_t now = doc["now"].as<long long>();
    JsonObjectConst ai = doc["ai"].as<JsonObjectConst>();
    std::cout << "{\"quota_reset\":" << (long long)codexAnnouncementTarget(ai, "quota_reset", now)
              << ",\"reset_grant\":" << (long long)codexAnnouncementTarget(ai, "reset_grant", now);
    if (doc["inspect_nearest"] | false) {
        const char *kind = nullptr;
        time_t target = codexNearestAnnouncement(ai,now,doc["show_reset"] | true,&kind);
        std::cout << ",\"nearest\":" << (long long)target << ",\"kind\":\"" << (kind ? kind : "") << "\"";
    }
    if (doc["inspect_selected"] | false) {
        JsonObjectConst selected=codexSelectedAnnouncement(ai,now,doc["show_reset"] | true);
        std::cout << ",\"selected\":";
        if (selected.isNull()) std::cout << "null";
        else { std::string json;serializeJson(selected,json);std::cout << json; }
    }
    if (doc["inspect_footer"] | false) {
        char footer[192],key[24];
        codexNewsFooterText(ai,now,doc["show_reset"] | true,footer,sizeof(footer));
        codexForecastDisplayKey(ai,now,doc["show_reset"] | true,key,sizeof(key));
        std::cout << ",\"footer\":\"" << footer << "\",\"display_key\":\"" << key << "\",\"changed\":"
                  << (codexForecastMetaChanged(doc["meta"] | "",key,doc["legacy_visible"] | false) ? "true" : "false");
    }
    std::cout << "}";
}
