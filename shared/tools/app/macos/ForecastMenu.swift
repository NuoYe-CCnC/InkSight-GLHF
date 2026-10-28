import AppKit
import Foundation

/// The real menu binding is also exercised without a status-bar item/backend.
/// It opens only the displayed event's revalidated URL, never a replacement.
final class ForecastMenu: NSObject {
    let item = NSMenuItem(title: "查看预告原文", action: nil, keyEquivalent: "")
    private(set) var link: ForecastLink?
    private var checking = false
    private var generation = 0
    private let resolve: (ForecastLink, @escaping (URL?) -> Void) -> Void
    private let open: (URL) -> Void
    init(resolve: @escaping (ForecastLink, @escaping (URL?) -> Void) -> Void,
         open: @escaping (URL) -> Void = { NSWorkspace.shared.open($0) }) {
        self.resolve = resolve; self.open = open
        super.init()
        item.target = self; item.action = #selector(activate)
        refresh(link: nil)
    }
    func refresh(link fresh: ForecastLink?) {
        if link != fresh { generation += 1 }
        link = fresh
        item.isEnabled = fresh != nil && !checking
    }
    @objc func activate() {
        guard item.isEnabled, !checking, let expected = link else { return }
        let token = generation
        checking = true; item.isEnabled = false
        resolve(expected) { [weak self] url in
            guard let self else { return }
            self.checking = false
            self.item.isEnabled = self.link != nil
            guard self.generation == token, self.link == expected, url == expected.url else { return }
            self.open(expected.url)
        }
    }
}
