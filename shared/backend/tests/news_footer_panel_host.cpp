// Real panel_pages + panel_ui, host Arduino shims only; fonts remain local.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <time.h>
#include <iostream>
#include <iterator>
#include "panel_pages.h"
#include "codex_announcements.h"
QuietSerial Serial;
uint8_t imgBuf[48000];
static time_t frozen;
extern "C" time_t time(time_t *out) {if(out)*out=frozen;return frozen;}
int main() {
    JsonDocument input;
    const std::string text(std::istreambuf_iterator<char>(std::cin),{});
    if (deserializeJson(input,text)) return 2;
    frozen=input["now"].as<long long>();
    JsonDocument doc;doc.set(input["document"]);
    if(input["page"]==0) renderAiPanel(doc);else renderNewsGoldPanel(doc);
    std::cout.write((const char*)imgBuf,sizeof(imgBuf));
}
