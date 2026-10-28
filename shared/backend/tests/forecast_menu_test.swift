import AppKit
import Foundation

@main
enum ForecastMenuTest {
    static func main() throws {
        _ = NSApplication.shared
        let decoder = JSONDecoder()
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let raw = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let now = Date(timeIntervalSince1970: raw["now"] as! Double)
        let body: [String: Any] = ["remaining_percent":37,"last_success_at":now.timeIntervalSince1970-60,
            "expires_at":now.timeIntervalSince1970+1740,"max_age_seconds":1800,"announcements":raw["announcements"]!]
        let quota = try decoder.decode(MenuQuota.self, from: JSONSerialization.data(withJSONObject: body))
        let selected = quota.announcements?.selected(at: now)
        let link = quota.announcements?.link(at: now)
        var opened = [URL](); var pending: ((URL?) -> Void)?
        let binding = ForecastMenu(resolve: { _, completion in pending = completion }, open: { opened.append($0) })
        let menu = NSMenu();menu.autoenablesItems = false; menu.addItem(binding.item)
        binding.refresh(link: link)
        let initiallyEnabled = binding.item.isEnabled
        if initiallyEnabled {
            menu.performActionForItem(at: 0)
            precondition(!binding.item.isEnabled && pending != nil)
            if raw["race"] as? String == "expired" { binding.refresh(link: nil) }
            if raw["race"] as? String == "replaced", let link {
                binding.refresh(link: ForecastLink(eventID: link.eventID,revision: String(repeating:"f",count:24),url:link.url))
            }
            pending?(link?.url)
        }
        let result: [String: Any] = ["label":quota.label(at:now),"enabled":initiallyEnabled,
            "opened":opened.map(\.absoluteString),"event_id":selected?.eventID as Any? ?? NSNull(),
            "detail":quota.announcements?.detail(at:now) as Any? ?? NSNull(),"title":binding.item.title]
        print(String(data:try JSONSerialization.data(withJSONObject:result,options:.sortedKeys),encoding:.utf8)!)
    }
}
