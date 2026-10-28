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
    let selectedEvent: MenuAnnouncement?
    let canonicalSourceURL: String?
    let targetAuthor: String?
    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version", enabled
        case quotaReset = "quota_reset", resetGrant = "reset_grant", resetUnspecified = "reset_unspecified"
        case selectedEvent = "selected_event", canonicalSourceURL = "canonical_source_url", targetAuthor = "target_author"
    }
    func selected(at now: Date) -> MenuAnnouncement? {
        guard enabled, [1,2].contains(schemaVersion) else { return nil }
        let values = [("quota_reset",quotaReset),("reset_grant",resetGrant),("reset_unspecified",resetUnspecified)]
        if schemaVersion == 2 {
            guard let event = selectedEvent, event.isActive(kind: event.kind, at: now, schema: 2),
                  values.contains(where: { $0.0 == event.kind && $0.1 == event }),
                  canonicalSourceURL == event.canonicalSourceURL, targetAuthor == event.sourceAuthor else { return nil }
            return event
        }
        return values.compactMap { kind, value -> MenuAnnouncement? in
            guard let value, value.isActive(kind: kind, at: now, schema: 1) else { return nil }; return value
        }.sorted {
            if $0.targetAt != $1.targetAt { return $0.targetAt! < $1.targetAt! }
            if $0.publishedAt != $1.publishedAt { return $0.publishedAt > $1.publishedAt }
            return $0.kind < $1.kind
        }.first
    }
    func isActive(at now: Date) -> Bool { selected(at: now) != nil }
    func link(at now: Date = Date()) -> ForecastLink? {
        guard schemaVersion == 2, let event = selected(at: now),
              let id = event.eventID, let revision = event.revision,
              let url = event.checkedURL else { return nil }
        return ForecastLink(eventID: id, revision: revision, url: url)
    }
    func detail(at now: Date = Date()) -> String? {
        guard let event = selected(at: now) else { return nil }
        let verb = event.kind == "reset_grant" ? "发放" : "重置"
        var text: String
        if let target = event.targetAt {
            let formatter = DateFormatter(); formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
            formatter.dateFormat = "MM-dd HH:mm"
            text = "预计 " + formatter.string(from: Date(timeIntervalSince1970: target))
            text += event.timeRelation == "at" ? " " : " 前"
            text += verb
        } else {
            text = "预计" + verb + "；" + (event.displayDate.map { String($0.suffix(5)) + "，" } ?? "") + "时间待定"
        }
        if event.planScope == "unspecified" { text += "；适用套餐未说明" }
        if let zone = event.sourceTimeZone, ["PST","PDT","PT","UTC","GMT","EST","EDT"].contains(zone.uppercased()) {
            text += "（原文 " + zone.uppercased() + "）"
        }
        return text
    }
}

struct ForecastLink: Equatable {
    let eventID: String
    let revision: String
    let url: URL
}

struct MenuAnnouncement: Decodable, Equatable {
    let kind: String, status: String, confidence: String
    let targetAt: Double?
    let expiresAt: Double, validUntil: Double, publishedAt: Double
    let timeRelation: String?, planScope: String?, sourceTimeZone: String?
    let eventID: String?, revision: String?, timeType: String?
    let canonicalSourceURL: String?, sourceAuthor: String?, postID: String?
    let rangeStartAt: Double?, rangeEndAt: Double?, displayDate: String?, expiryOriginAt: Double?
    enum CodingKeys: String, CodingKey {
        case kind, status, confidence, targetAt = "target_at", expiresAt = "expires_at"
        case validUntil = "valid_until", publishedAt = "published_at"
        case timeRelation = "time_relation", planScope = "plan_scope", sourceTimeZone = "source_time_zone"
        case eventID = "event_id", revision, timeType = "time_type"
        case canonicalSourceURL = "canonical_source_url", sourceAuthor = "source_author", postID = "post_id"
        case rangeStartAt = "range_start_at", rangeEndAt = "range_end_at", displayDate = "display_date", expiryOriginAt = "expiry_origin_at"
    }
    var checkedURL: URL? {
        guard let text = canonicalSourceURL, let author = sourceAuthor, let id = postID,
              author.range(of: "^[A-Za-z0-9_]{1,15}$", options: .regularExpression) != nil,
              id.range(of: "^[0-9]{5,24}$", options: .regularExpression) != nil,
              ["https://x.com/\(author)/status/\(id)","https://twitter.com/\(author)/status/\(id)"].contains(text)
        else { return nil }
        return URL(string: text)
    }
    func isActive(kind expected: String, at now: Date, schema: Int = 1) -> Bool {
        let stamp = now.timeIntervalSince1970
        guard ["quota_reset","reset_grant","reset_unspecified"].contains(kind), kind == expected,
              status == "pending", confidence == "explicit",
              [expiresAt, validUntil, publishedAt].allSatisfy({ $0.isFinite }),
              0 < publishedAt, publishedAt <= stamp, stamp < validUntil,
              validUntil <= expiresAt, expiresAt <= publishedAt + 14 * 86400 else { return false }
        if schema == 1 { return targetAt.map { $0.isFinite && validUntil <= $0 && $0 == expiresAt } ?? false }
        guard schema == 2, checkedURL != nil,
              eventID?.range(of: "^[A-Za-z0-9:_-]{1,100}$", options: .regularExpression) != nil,
              revision?.range(of: "^[a-f0-9]{24}$", options: .regularExpression) != nil else { return false }
        if timeType == "exact" {
            return targetAt.map { $0.isFinite && validUntil <= $0 && $0 == expiresAt } == true && ["at","before"].contains(timeRelation)
        }
        guard targetAt == nil else { return false }
        if timeType == "unknown" {
            return expiryOriginAt.map { $0.isFinite && 0 < $0 && $0 <= publishedAt && expiresAt <= $0 + 86400 } ?? false
        }
        if timeType == "date_range", let lo = rangeStartAt, let hi = rangeEndAt,
           lo.isFinite, hi.isFinite, 0 < lo, lo < hi, hi == expiresAt, hi-lo <= 25*3600 {
            if let date = displayDate {
                let f = DateFormatter(); f.timeZone = TimeZone(identifier: "Asia/Shanghai"); f.dateFormat = "yyyy-MM-dd"
                return f.string(from: Date(timeIntervalSince1970: lo)) == date && f.string(from: Date(timeIntervalSince1970: hi-1)) == date
            }
            return true
        }
        return false
    }
}
