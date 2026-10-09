#include <iostream>
#include <string>
#include "news_issue_status.h"

static bool safeLabel(const char *s) {
    // Policy tests inject the same bounded label eligibility as the mirror;
    // real MiSans coverage and ink widths are tested separately with fonts.
    std::string text(s ? s : "");
    return !text.empty() && text.size() <= 36 && text.find("\n") == std::string::npos
        && text.find("侃") == std::string::npos && text.find("<") == std::string::npos;
}

int main() {
    setenv("TZ", "CST-8", 1); tzset();
    std::string line;
    while (std::getline(std::cin, line)) {
        JsonDocument doc;
        if (deserializeJson(doc, line)) return 2;
        time_t now = doc["_test_now"].as<long long>();
        const char *status = newsIssueStatusText(doc, now, safeLabel);
        JsonDocument result;
        result["status"] = status ? status : "";
        auto expected = newsExpectedIssue(doc, now);
        result["expected"] = expected.id;
        result["trusted"] = expected.trusted;
        newsClearObsoleteLocalUpdate(doc, now);
        result["local"] = doc["screen"]["pages"]["news_gold"]["news"]["device_update_state"] | "";
        serializeJson(result, std::cout); std::cout << '\n';
    }
}
