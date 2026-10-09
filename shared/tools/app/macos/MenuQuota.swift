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

    enum CodingKeys: String, CodingKey {
        case remainingPercent = "remaining_percent", lastSuccessAt = "last_success_at"
        case expiresAt = "expires_at", maxAgeSeconds = "max_age_seconds", reason
    }

    func label(at now: Date = Date()) -> String {
        guard reason == nil, let percent = remainingPercent, (0...100).contains(percent),
              let stamp = lastSuccessAt, let expiry = expiresAt,
              stamp.isFinite, expiry.isFinite, maxAgeSeconds > 0,
              expiry == stamp + Double(maxAgeSeconds),
              stamp <= now.timeIntervalSince1970, expiry > now.timeIntervalSince1970
        else { return "-" }
        return "\(percent)%"
    }

    var detail: String {
        let reasons = ["stale": "数据已陈旧", "account_mismatch": "账户未确认或已变化",
                       "missing_7d": "没有七日窗口", "invalid_percent": "额度值无效",
                       "wrong_source": "不是本机采集", "collector_unavailable": "采集暂不可用",
                       "invalid_timestamp": "采集时间无效", "unavailable": "尚无有效采集"]
        return reasons[reason ?? ""] ?? "七日窗口剩余额度"
    }
}
