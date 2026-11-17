import Foundation
import CryptoKit
import CoreFoundation

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
    let resetsAt: Double?
    let retrying: Bool
    let announcements: MenuAnnouncements?

    enum CodingKeys: String, CodingKey {
        case remainingPercent = "remaining_percent", lastSuccessAt = "last_success_at"
        case expiresAt = "expires_at", maxAgeSeconds = "max_age_seconds", reason, announcements
        case resetsAt = "resets_at", retrying
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        remainingPercent = try c.decodeIfPresent(Int.self, forKey: .remainingPercent)
        lastSuccessAt = try c.decodeIfPresent(Double.self, forKey: .lastSuccessAt)
        expiresAt = try c.decodeIfPresent(Double.self, forKey: .expiresAt)
        maxAgeSeconds = try c.decode(Int.self, forKey: .maxAgeSeconds)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        resetsAt = try c.decodeIfPresent(Double.self, forKey: .resetsAt)
        retrying = (try? c.decode(Bool.self, forKey: .retrying)) ?? false
        // The optional experimental field must never poison valid quota data.
        announcements = try? c.decodeIfPresent(MenuAnnouncements.self, forKey: .announcements)
    }

    func quotaLabel(at now: Date = Date()) -> String {
        guard reason == nil, let percent = remainingPercent, (0...100).contains(percent),
              let stamp = lastSuccessAt, let expiry = expiresAt,
              stamp.isFinite, expiry.isFinite, maxAgeSeconds > 0,
              expiry <= stamp + Double(min(1800,maxAgeSeconds)), expiry > stamp,
              resetsAt.map({ $0.isFinite && expiry <= $0 }) ?? true,
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

/// Local identity/window check used even when the loopback HTTP request fails.
struct MenuQuotaLocalContext {
    let identity: String?
    let stamp: Double?
    let reset: Double?
    let valid: Bool
    let reason: String
    let loginBoundary: Double

    static func load(runtime: URL?, authURL: URL? = nil, at now: Date = Date()) -> Self {
        let auth = authURL ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex/auth.json")
        func json(_ path: URL?) -> [String: Any]? {
            guard let path, let data = try? Data(contentsOf: path), data.count <= 4*1024*1024 else { return nil }
            return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        func number(_ value: Any?) -> Double? {
            guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { return nil }
            return n.doubleValue
        }
        let account = (json(auth)?["tokens"] as? [String: Any])?["account_id"] as? String
        let identity = account.flatMap { $0.isEmpty ? nil : SHA256.hash(data: Data($0.utf8)).map { String(format:"%02x",$0) }.joined().prefix(20).description }
        let boundary = ((try? auth.resourceValues(forKeys:[.contentModificationDateKey]))?.contentModificationDate?.timeIntervalSince1970 ?? now.timeIntervalSince1970).rounded(.down)
        let groups = json(runtime?.appendingPathComponent("shared/backend/state/data_cache.json"))?["groups"] as? [String: Any]
        let group = groups?["ai.codex"] as? [String: Any]
        let value = group?["value"] as? [String: Any]
        let stamp = number(group?["last_success"]), reset = number(value?["resets_at"])
        let health = json(runtime?.appendingPathComponent("shared/backend/state/codex_collector_health.json"))
        var reason = "current"
        if identity == nil { reason = "identity_unknown" }
        else if identity != value?["account_key"] as? String { reason = "account_mismatch" }
        else if value?["source"] as? String != "mac" { reason = "wrong_source" }
        else if number(value?["duration_minutes"]) != 10080 { reason = "missing_7d" }
        else if let used = number(value?["used_percent"]), (0...100).contains(used), let stamp, stamp > 0, stamp <= now.timeIntervalSince1970 {
            if reset != nil && reset! <= now.timeIntervalSince1970 { reason = "reset_boundary" }
            if value?["resets_at"] != nil && reset == nil { reason = "invalid_reset" }
            if health?["reason"] as? String == "missing_7d", (number(health?["last_attempt_at"]) ?? 0) >= stamp.rounded(.down) { reason = "missing_7d" }
            if !["fresh","stale"].contains(group?["status"] as? String ?? "") { reason = "unavailable" }
            if let h=number(health?["last_success_at"]),h > now.timeIntervalSince1970+2 { reason="invalid_timestamp" }
            if let meta=group?["meta"] as? [String:Any], let fail=meta["last_fail"] as? String,
               ["no 7D window in codex payload","no 7D window"].contains(fail), (number(group?["last_attempt"]) ?? 0)>=stamp.rounded(.down) { reason="missing_7d" }
        } else { reason = "invalid_sample" }
        return Self(identity:identity,stamp:stamp,reset:reset,valid:reason == "current",reason:reason,loginBoundary:boundary)
    }
}

/// The displayed sample and the forecast have separate clocks and lifetimes.
struct MenuQuotaStore {
    var current: MenuQuota?
    var announcements: MenuAnnouncements?
    private var identity: String?
    private var window: Double?
    private var observed = false
    private var localValid = false
    private var invalidThrough = 0.0
    private var notBefore = 0.0
    private(set) var retrying = false
    private(set) var state = "unavailable"
    private(set) var cause: String?

    mutating func clear() {
        current=nil; announcements=nil; retrying=false; state="service_stopped"; cause="service_stopped"
    }
    @discardableResult mutating func observe(_ context: MenuQuotaLocalContext, at now: Date = Date()) -> Bool {
        let identityChanged = observed && identity != context.identity
        let changed = observed && (identityChanged || window != context.reset || localValid != context.valid)
        if changed {
            current=nil
            if identityChanged { announcements=nil; notBefore=max(notBefore,context.loginBoundary) }
            state="identity_or_window_changed"
        }
        if !context.valid {
            invalidThrough=max(invalidThrough,context.stamp ?? 0,current?.lastSuccessAt ?? 0)
            current=nil; retrying=false; state=context.reason; cause=context.reason
        }
        identity=context.identity; window=context.reset; localValid=context.valid; observed=true
        if let value=current, value.quotaLabel(at:now)=="-" { current=nil; state="expired"; cause="sample_expired" }
        return changed
    }
    mutating func accept(_ value: MenuQuota, context: MenuQuotaLocalContext, at now: Date = Date()) {
        observe(context,at:now)
        // The collector can advance between HTTP snapshot and main-thread
        // delivery. Ignore that older reply without clearing a still-valid
        // sample from the same identity/window; the next read gets the new one.
        if context.valid, let incoming=value.lastSuccessAt, let local=context.stamp, incoming < local { return }
        if let old=current?.lastSuccessAt, let incoming=value.lastSuccessAt, incoming < old { return }
        announcements=value.announcements
        guard context.valid, value.quotaLabel(at:now) != "-", let stamp=value.lastSuccessAt,
              stamp >= notBefore, stamp > invalidThrough, value.resetsAt == context.reset,
              stamp == context.stamp, stamp >= (current?.lastSuccessAt ?? 0) else {
            current=nil; retrying=false; state=value.reason ?? context.reason; return
        }
        current=value; retrying=value.retrying; state=retrying ? "retrying" : "current"; cause=retrying ? "collector_retrying" : nil
    }
    mutating func transportFailed(context: MenuQuotaLocalContext, cause errorCause: String = "loopback_error", at now: Date = Date()) {
        observe(context,at:now)
        retrying=current?.quotaLabel(at:now) != nil && current?.quotaLabel(at:now) != "-"
        state=retrying ? "retrying" : context.valid ? "unavailable" : context.reason
        cause=errorCause
    }
    func label(at now: Date = Date()) -> String {
        (current?.quotaLabel(at:now) ?? "-") + (announcements?.isActive(at:now)==true ? "*" : "")
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
