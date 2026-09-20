#ifndef INKSIGHT_NETWORK_H
#define INKSIGHT_NETWORK_H

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

enum class VoiceWsEventType : uint8_t {
    None = 0,
    SessionReady,
    AsrPartial,
    AsrFinal,
    LlmDelta,
    TtsTextChunk,
    TtsAudioChunk,
    TurnDone,
    TurnInterrupted,
    Error,
};

struct VoiceWsEvent {
    VoiceWsEventType type = VoiceWsEventType::None;
    String text;
    String transcript;
    String turnId;
    String switchToMode;
    int generationId = 0;
    int chunkId = 0;
    int sampleRate = 16000;
    bool exitConversation = false;
    bool needsDecode = false;
    uint8_t *data = nullptr;
    size_t dataLen = 0;
};

extern bool g_userAborted;
extern bool g_suppressAbortCheck;

enum class NetworkFailureStage : uint8_t {
    None = 0,
    Scan,
    Association,
    Authentication,
    IP,
    DNS,
    TLS,
    HTTP,
    JSON,
};

NetworkFailureStage networkLastFailureStage();
const char *networkFailureStageName(NetworkFailureStage stage);

// ── Time state (updated by syncNTP / tickTime) ──────────────
extern int curHour, curMin, curSec;

// ── WiFi ────────────────────────────────────────────────────

// Connect to WiFi using stored credentials. Returns true on success.
bool connectWiFi();

// ── HTTP ────────────────────────────────────────────────────

// Fetch BMP image from backend and store in imgBuf. Returns true on success.
// If nextMode is true, appends &next=1 to request the next mode in sequence.
bool fetchBMP(bool nextMode = false, bool *isFallback = nullptr, String *renderedModeIdOut = nullptr);

#ifdef ENABLE_STRUCTURED
#include <ArduinoJson.h>
// Fetch structured payload (fmt=structured) from backend into doc.
// modeIdOut: X-Mode-Id；pendingRefreshOut: X-Pending-Refresh（变化触发刷新的标志）。
bool fetchStructured(JsonDocument &doc, String *modeIdOut = nullptr, bool *pendingRefreshOut = nullptr);

// 每次真实启动/深睡唤醒只发一个 XAUS 刷新请求。云端模式写入受 Basic Auth
// 保护的 WebDAV 请求队列；本地模式调用设备令牌保护的后端端点。同一唤醒使用
// 稳定 request_id，重试可幂等；返回 true 表示请求已被通道接受。
bool requestGoldRefreshForWake(const JsonDocument &doc);
bool requestGoldCatchupIfStale(const JsonDocument &doc);  // legacy alias

// If the latest issue whose configured wall-clock time has passed is not in
// the feed, enqueue one replay-safe server-side check. The server independently
// selects the due issue and owns the paid-call ledger.
bool newsCurrentIssueMissing(const JsonDocument &doc);
bool requestNewsCheckForWake(JsonDocument &doc);
#endif

// Check whether backend has pending refresh/switch request for this device.
// If shouldExitLive is not null, it is set to true when backend runtime_mode is interval.
bool hasPendingRemoteAction(bool *shouldExitLive = nullptr);

// Peek pending_mode for this device without consuming it.
bool peekPendingMode(String &pendingModeOut);

// POST runtime mode (active/interval) to backend.
bool postRuntimeMode(const char *mode);
bool postVocabEvent(const char *action, const char *rating = nullptr);
bool fetchVocabReviewPack(uint8_t *ratingParts, size_t partLen, int yStart, int yEnd);
typedef void (*AudioChunkCallback)(const uint8_t *data, size_t len, void *userData);
bool fetchVocabAudio(AudioChunkCallback onChunk, void *userData = nullptr);

// POST device config JSON to backend /api/config endpoint.
void postConfigToBackend();

bool submitVoiceTurn(const char *pcmPath, int sampleRate, int screenW, int screenH, String &turnId, String &replyText, String &transcript, bool &exitConversation);
bool submitVoiceTurnBytes(const uint8_t *pcmBytes, size_t pcmSize, int sampleRate, int screenW, int screenH, String &turnId, String &replyText, String &transcript, bool &exitConversation);
bool fetchVoiceAudio(const String &turnId, const char *path);
bool fetchVoiceImage(const String &turnId);
bool fetchVoiceIntroImage(int screenW, int screenH);
bool voiceWsOpen(int sampleRate, int screenW, int screenH, bool includeImage);
bool voiceWsConnected();
void voiceWsLoop();
bool voiceWsSendAudioBin(const int16_t *samples, size_t sampleCount);
bool voiceWsSendAudioChunk(const int16_t *samples, size_t sampleCount);
bool voiceWsSendRawPacket(const uint8_t *data, size_t len);
bool voiceWsCommitTurn();
bool voiceWsInterrupt();
bool voiceWsPollEvent(VoiceWsEvent &eventOut);
void voiceWsReleaseEvent(VoiceWsEvent &event);
void voiceWsClose();
bool voiceWsBinaryAudio();
bool voiceWsServerVad();

bool ensureDeviceToken();
bool postHeartbeat(bool force = false);

// ── Focus listening helpers ─────────────────────────────────
bool fetchFocusListeningFlag(bool *outEnabled, bool *outAlwaysActive = nullptr);
bool fetchFocusAlertBMP();

// ── Battery ─────────────────────────────────────────────────

// Read battery voltage via ADC (returns volts)
float readBatteryVoltage();

// ── NTP time ────────────────────────────────────────────────

// Sync time from NTP servers
void syncNTP(const char *server1 = nullptr, const char *server2 = nullptr,
             const char *server3 = nullptr);

// Advance software clock by one second
void tickTime();

#endif // INKSIGHT_NETWORK_H
