import Foundation

@main
enum DesktopNativeTest {
    static func main() throws {
        var checks = 0
        func check(_ value: Bool) { precondition(value); checks += 1 }
        let decoder = JSONDecoder()
        func quota(_ percent: Any, stamp: Double = 1000, expiry: Double = 2800,
                   reason: Any = NSNull()) throws -> MenuQuota? {
            let body: [String: Any] = ["remaining_percent": percent, "last_success_at": stamp,
                "expires_at": expiry, "max_age_seconds": 1800, "reason": reason]
            return try? decoder.decode(MenuQuota.self, from: JSONSerialization.data(withJSONObject: body))
        }
        let now = Date(timeIntervalSince1970: 1100)
        for p in [0, 9, 36, 100] { check(try quota(p)?.label(at: now) == "\(p)%") }
        for p: Any in [-1, 101, true, "36", NSNull()] {
            check(try quota(p)?.label(at: now) ?? "-" == "-")
        }
        check(try quota(36)?.label(at: Date(timeIntervalSince1970: 2800)) == "-")
        check(try quota(36, stamp: 1200)?.label(at: now) == "-")
        check(try quota(36, expiry: 999999)?.label(at: now) == "-")
        check(try quota(36, reason: "account_mismatch")?.label(at: now) == "-")
        var generation = MenuQuotaGeneration()
        let old = generation.begin()!
        let newer = generation.begin()!
        check(!generation.accepts(old)); check(generation.accepts(newer))
        generation.invalidate()  // Backend restart/port change invalidates pending read.
        check(!generation.accepts(newer))
        let quitting = generation.begin()!
        generation.stop()
        check(!generation.accepts(quitting)); check(generation.begin() == nil)
        generation.resume()
        let restored = generation.begin()!
        check(generation.accepts(restored)); check(!generation.accepts(quitting))
        let base = URL(string: "http://127.0.0.1:18181")!
        let path = "/desktop/config/" + String(repeating: "a", count: 32)
        let ticket = String(repeating: "b", count: 43)  // Synthetic, no production capability.
        let target = DesktopEntry.url(base: base, path: path, ticket: ticket)!
        check(target.path == path && target.host == "127.0.0.1" && target.port == 18181)
        check(target.query == nil && target.fragment == "entry=" + ticket)
        for bad in ["/", "//evil.example", "/desktop/config/bad", path + "?entry=bad", path + "/"] {
            check(DesktopEntry.url(base: base, path: bad, ticket: ticket) == nil)
        }
        check(DesktopEntry.url(base: base, path: path, ticket: "invalid") == nil)
        print("PASS \(checks) native quota/expiry/out-of-order/quit/entry URL checks")
    }
}
