import Foundation

enum DesktopEntry {
    static func url(base: URL, path: String, ticket: String) -> URL? {
        guard path.range(of: "^/desktop/config/[0-9a-f]{32}$", options: .regularExpression) != nil,
              ticket.range(of: "^[A-Za-z0-9_-]{32,128}$", options: .regularExpression) != nil,
              var target = URLComponents(url: base, resolvingAgainstBaseURL: false)
        else { return nil }
        target.path = path
        target.query = nil
        target.fragment = "entry=" + ticket
        return target.url
    }
}
