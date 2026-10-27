import Foundation

/// Main-thread request identity. Stop/restart and quit invalidate late replies.
struct MenuQuotaGeneration {
    private var value = 0
    private var stopped = false
    mutating func begin() -> Int? {
        guard !stopped else { return nil }
        value += 1
        return value
    }
    func accepts(_ candidate: Int) -> Bool { !stopped && candidate == value }
    mutating func invalidate() { value += 1 }
    mutating func stop() { stopped = true; invalidate() }
    mutating func resume() { stopped = false; invalidate() }
}

/// A successful source sample has a fixed expiry, never extended by UI polling.
struct MenuQuota: Decodable {
    let remainingPercent: Int?
    let lastSuccessAt: Double?
    let expiresAt: Double?
    let maxAgeSeconds: Int
    let reason: String?
    let announcements: MenuAnnouncements?

    enum CodingKeys: String, CodingKey {
        case remainingPercent = "remaining_percent", lastSuccessAt = "last_success_at"
        case expiresAt = "expires_at", maxAgeSeconds = "max_age_seconds", reason, announcements
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        remainingPercent = try c.decodeIfPresent(Int.self, forKey: .remainingPercent)
        lastSuccessAt = try c.decodeIfPresent(Double.self, forKey: .lastSuccessAt)
        expiresAt = try c.decodeIfPresent(Double.self, forKey: .expiresAt)
        maxAgeSeconds = try c.decode(Int.self, forKey: .maxAgeSeconds)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        // The optional experimental field must never poison valid quota data.
        announcements = try? c.decodeIfPresent(MenuAnnouncements.self, forKey: .announcements)
    }

    func quotaLabel(at now: Date = Date()) -> String {
        guard reason == nil, let percent = remainingPercent, (0...100).contains(percent),
              let stamp = lastSuccessAt, let expiry = expiresAt,
              stamp.isFinite, expiry.isFinite, maxAgeSeconds > 0,
              expiry == stamp + Double(maxAgeSeconds),
              stamp <= now.timeIntervalSince1970, expiry > now.timeIntervalSince1970
        else { return "-" }
        return "\(percent)%"
    }

    func label(at now: Date = Date()) -> String {
        quotaLabel(at: now) + (announcements?.isActive(at: now) == true ? "*" : "")
    }

    var detail: String {
        let reasons = ["stale": "数据已陈旧", "account_mismatch": "账户未确认或已变化",
                       "missing_7d": "没有七日窗口", "invalid_percent": "额度值无效",
                       "wrong_source": "不是本机采集", "collector_unavailable": "采集暂不可用",
                       "invalid_timestamp": "采集时间无效", "unavailable": "尚无有效采集"]
        return reasons[reason ?? ""] ?? "七日窗口剩余额度"
    }
}

struct MenuAnnouncements: Decodable {
    let schemaVersion: Int
    let enabled: Bool
    let quotaReset: MenuAnnouncement?
    let resetGrant: MenuAnnouncement?
    let resetUnspecified: MenuAnnouncement?
    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version", enabled
        case quotaReset = "quota_reset", resetGrant = "reset_grant"
        case resetUnspecified = "reset_unspecified"
    }
    func isActive(at now: Date) -> Bool {
        schemaVersion == 1 && enabled &&
        (quotaReset?.isActive(kind: "quota_reset", at: now) == true ||
         resetGrant?.isActive(kind: "reset_grant", at: now) == true ||
         resetUnspecified?.isActive(kind: "reset_unspecified", at: now) == true)
    }
    func detail(at now: Date = Date()) -> String? {
        guard schemaVersion == 1 && enabled else { return nil }
        let active = [("quota_reset",quotaReset),("reset_grant",resetGrant),("reset_unspecified",resetUnspecified)]
            .compactMap { kind, value -> MenuAnnouncement? in
                guard let value, value.isActive(kind: kind, at: now) else { return nil }; return value
            }.sorted { $0.targetAt == $1.targetAt ? $0.publishedAt > $1.publishedAt : $0.targetAt < $1.targetAt }
        guard let event = active.first else { return nil }
        let formatter = DateFormatter(); formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "MM-dd HH:mm"
        var text = "预计 " + formatter.string(from: Date(timeIntervalSince1970: event.targetAt))
        text += event.timeRelation == "at" ? " " : " 前"
        text += event.kind == "reset_grant" ? "发放" : "重置"
        if event.planScope == "unspecified" { text += "；适用套餐未说明" }
        if let zone = event.sourceTimeZone, ["PST","PDT","PT","UTC","GMT","EST","EDT"].contains(zone.uppercased()) {
            text += "（原文 " + zone.uppercased() + "）"
        }
        return text
    }
}

struct MenuAnnouncement: Decodable {
    let kind: String, status: String, confidence: String
    let targetAt: Double, expiresAt: Double, validUntil: Double, publishedAt: Double
    let timeRelation: String?, planScope: String?, sourceTimeZone: String?
    enum CodingKeys: String, CodingKey {
        case kind, status, confidence, targetAt = "target_at", expiresAt = "expires_at"
        case validUntil = "valid_until", publishedAt = "published_at"
        case timeRelation = "time_relation", planScope = "plan_scope", sourceTimeZone = "source_time_zone"
    }
    func isActive(kind expected: String, at now: Date) -> Bool {
        let stamp = now.timeIntervalSince1970
        return kind == expected && status == "pending" && confidence == "explicit" &&
        [targetAt, expiresAt, validUntil, publishedAt].allSatisfy { $0.isFinite } &&
        publishedAt > 0 && publishedAt <= stamp && stamp < validUntil &&
        validUntil <= targetAt && targetAt == expiresAt && targetAt <= stamp + 14 * 86400
    }
}
