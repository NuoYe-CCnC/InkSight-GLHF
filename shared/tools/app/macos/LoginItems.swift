import AppKit
import ServiceManagement

/// Native opt-ins use SMAppService. System-added items are retained, never
/// replaced silently or inferred from a stored preference flag.
enum LoginItems {
    private static var busy = false
    private static func legacy(_ remove: Bool = false) -> Bool? {
        let path = Bundle.main.bundleURL.path.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        with timeout of 3 seconds
            tell application "System Events"
                set matches to every login item whose path is "\(path)"
                set found to count of matches
                \(remove ? "repeat with entry in matches\n delete entry\n end repeat" : "")
                return found
            end tell
        end timeout
        """
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let deadline = Date().addingTimeInterval(4)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { process.terminate(); return nil }
            guard process.terminationStatus == 0,
                  let text = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8),
                  let count = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
            return count > 0
        } catch { return nil }
    }

    static func refresh(_ completion: @escaping (Bool?, String) -> Void) {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .utility).async {
            let old = legacy()
            let native = SMAppService.mainApp.status
            let enabled: Bool? = old == true || native == .enabled ? true : (old == nil ? nil : false)
            let label = native == .requiresApproval ? "登录时启动：等待系统允许" :
                (enabled == nil ? "登录时启动：请在系统设置核对" : "登录时启动")
            DispatchQueue.main.async { busy = false; completion(enabled, label) }
        }
    }

    static func change(_ completion: @escaping (String) -> Void) {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .utility).async {
            let old = legacy()
            var message = "登录项核验失败，未修改。请在系统登录项设置确认权限。"
            if let old = old {
                let native = SMAppService.mainApp.status
                do {
                    if old || native == .enabled || native == .requiresApproval {
                        if old && legacy(true) == nil { throw NSError(domain: "LoginItems", code: 1) }
                        if native == .enabled || native == .requiresApproval { try SMAppService.mainApp.unregister() }
                        message = "已关闭登录时启动；当前运行的服务未停止。"
                    } else {
                        try SMAppService.mainApp.register()
                        message = SMAppService.mainApp.status == .enabled ? "已开启登录时启动。" :
                            "登录项已申请，尚需在系统设置中允许，未确认启用。"
                    }
                } catch { message = "登录项修改未完整成功，请在系统设置核对实际状态。" }
            }
            DispatchQueue.main.async { busy = false; completion(message) }
        }
    }
}
