import AppKit
import Darwin
import Foundation
import SwiftUI
import ServiceManagement

private let appVersion = "v0.1.0-test.9"
private let inkBlue = Color.primary

@main
final class InkSightDesktopApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate {
    private static let delegate = InkSightDesktopApp()
    private var window: NSWindow?
    private var statusItem: NSStatusItem?
    private var stateItem = NSMenuItem(title: "服务：准备中", action: nil, keyEquivalent: "")
    private var publishItem = NSMenuItem(title: "最近成功发布：尚无验证记录", action: nil, keyEquivalent: "")
    private var quotaItem = NSMenuItem(title: "Codex：尚无有效采集", action: nil, keyEquivalent: "")
    private var pauseItem = NSMenuItem(title: "暂停自动服务", action: nil, keyEquivalent: "")
    private var loginItem = NSMenuItem(title: "登录时启动：检查中", action: nil, keyEquivalent: "")
    private var menuTimer: Timer?
    private var quitPending = false
    private var quitApproved = false

    static func main() {
        #if INKSIGHT_LIFECYCLE_TEST
        if CommandLine.arguments.contains("--self-test-lifecycle") || CommandLine.arguments.contains("--self-test-port-conflict") {
            guard let root = ProcessInfo.processInfo.environment["INKSIGHT_APP_DATA_ROOT"],
                  FileManager.default.fileExists(atPath: root + "/.offline-lifecycle-test"),
                  Bundle.main.bundleIdentifier == "cc.nuoye.inksight.lifecycle-test" else { Darwin.exit(90) }
            UserDefaults.standard.set(ProcessInfo.processInfo.environment["INKSIGHT_TEST_PORT"] ?? "18181", forKey: "servicePort")
            for key in ["allowScheduledNetwork", "resumePublisher", "servicesPaused", "explainedBackgroundClose"] {
                UserDefaults.standard.set(false, forKey: key)
            }
        }
        #endif
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let applicationItem = NSMenuItem()
        menu.addItem(applicationItem)
        let applicationMenu = NSMenu()
        applicationMenu.addItem(withTitle: "退出 InkSight", action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q")
        applicationItem.submenu = applicationMenu
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApplication.shared.mainMenu = menu

        let control = ControlView(runtime: RuntimeManager.shared).frame(minWidth: 650, minHeight: 580)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 690),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "InkSight"
        window.minSize = NSSize(width: 650, height: 580)
        window.contentView = NSHostingView(rootView: control)
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        installStatusItem()
        if statusItem?.isVisible == true {
            // Startup, including login, is quiet. User-initiated menu/Dock
            // actions are the only paths that activate an existing window.
            NSApplication.shared.setActivationPolicy(.accessory)
        } else { showWindow() }
        RuntimeManager.shared.prepareAndStart()
        menuTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refreshMenu()
        }
        #if INKSIGHT_LIFECYCLE_TEST
        if CommandLine.arguments.contains("--self-test-lifecycle") { runLifecycleTest() }
        if CommandLine.arguments.contains("--self-test-port-conflict") {
            guard RuntimeManager.shared.backend == "端口被占用" else { Darwin.exit(92) }
            print("LIFECYCLE: PASS external occupied port retained"); fflush(stdout)
            NSApplication.shared.terminate(nil)
        }
        #endif
    }

    private func installStatusItem() {
        if ProcessInfo.processInfo.environment["INKSIGHT_TEST_NO_STATUS"] == "1" { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        guard let button = item.button else { return }
        guard let mark = MenuMark.image() else {
            NSStatusBar.system.removeStatusItem(item)
            return
        }
        button.image = mark
        button.imagePosition = .imageLeading
        button.font = NSFont.menuBarFont(ofSize: 0)
        button.title = " -"
        // Reserve 100% width so normal updates do not move adjacent menu items.
        item.length = ceil(mark.size.width + (" 100%" as NSString).size(
            withAttributes: [.font: button.font!]).width + 16)
        button.setAccessibilityLabel("InkSight 后台服务")
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        for entry in [NSMenuItem(title: "打开配置", action: #selector(openConsole), keyEquivalent: ""),
                      NSMenuItem(title: "运行详情", action: #selector(showWindow), keyEquivalent: ""),
                      .separator(), stateItem, quotaItem, publishItem, .separator(), pauseItem, loginItem,
                      NSMenuItem(title: "系统登录项设置", action: #selector(openLoginSettings), keyEquivalent: ""),
                      .separator(), NSMenuItem(title: "退出 InkSight", action: #selector(quit), keyEquivalent: "q")] {
            entry.target = self
            menu.addItem(entry)
        }
        pauseItem.action = #selector(togglePause)
        loginItem.action = #selector(toggleLogin)
        item.menu = menu
        statusItem = item
        refreshMenu()
    }

    @objc private func showWindow() {
        NSApplication.shared.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    @objc private func openConsole() { RuntimeManager.shared.openConfiguration() }
    @objc private func openLoginSettings() { SMAppService.openSystemSettingsLoginItems() }
    @objc private func quit() { NSApplication.shared.terminate(nil) }
    @objc private func togglePause() { RuntimeManager.shared.togglePause() }
    @objc private func toggleLogin() {
        LoginItems.change { message in
            RuntimeManager.shared.detail = message
            self.refreshMenu()
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshMenu()
        LoginItems.refresh { status, label in
            self.loginItem.title = label
            self.loginItem.state = status == true ? .on : (status == nil ? .mixed : .off)
            self.loginItem.isEnabled = status != nil
        }
    }

    private func refreshMenu() {
        let runtime = RuntimeManager.shared
        runtime.refreshMenuQuota()
        let quota = runtime.menuQuotaLabel
        if statusItem?.button?.title != " " + quota { statusItem?.button?.title = " " + quota }
        statusItem?.button?.setAccessibilityLabel("InkSight，Codex 七日剩余 " + quota)
        quotaItem.title = runtime.menuQuotaDetail
        quotaItem.isEnabled = false
        stateItem.title = "服务：\(runtime.serviceStatus)"
        stateItem.isEnabled = false
        publishItem.title = "最近成功发布：\(runtime.lastPublicationLabel)"
        publishItem.isEnabled = false
        pauseItem.title = runtime.paused ? "恢复自动服务" : "暂停自动服务"
        pauseItem.isEnabled = runtime.backend == "运行正常" && !runtime.transitioning
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard statusItem?.isVisible == true, statusItem?.button != nil else {
            RuntimeManager.shared.detail = "菜单栏入口不可用，窗口将保留。退出请使用退出菜单。"
            return false
        }
        let hide = {
            sender.orderOut(nil)
            NSApplication.shared.setActivationPolicy(.accessory)
        }
        if !UserDefaults.standard.bool(forKey: "explainedBackgroundClose") {
            let alert = NSAlert()
            alert.messageText = "已在菜单栏继续运行"
            alert.informativeText = "关闭窗口不会暂停采集或发布。可从菜单栏重新打开，选择“退出 InkSight”才停止服务。"
            alert.addButton(withTitle: "知道了")
            alert.beginSheetModal(for: sender) { _ in
                UserDefaults.standard.set(true, forKey: "explainedBackgroundClose")
                hide()
            }
        } else { hide() }
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if statusItem == nil { showWindow() }
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitApproved { return .terminateNow }
        if quitPending { return .terminateCancel }
        quitPending = true
        RuntimeManager.shared.beginQuit()
        RuntimeManager.shared.prepareToQuit { safe in
            DispatchQueue.main.async {
                self.quitPending = false
                if safe {
                    self.quitApproved = true
                    sender.terminate(nil)
                } else { RuntimeManager.shared.cancelQuit(); self.showWindow() }
            }
        }
        // Keep the ordinary event loop alive while URLSession and UI state
        // drain. Some AppKit termination-modal loops do not drain main-queue
        // callbacks. Re-enter terminate only once the barrier has approved it.
        return .terminateCancel
    }
    func applicationWillTerminate(_ notification: Notification) { RuntimeManager.shared.shutdown() }

    #if INKSIGHT_LIFECYCLE_TEST
    private func runLifecycleTest() {
        guard let root = ProcessInfo.processInfo.environment["INKSIGHT_APP_DATA_ROOT"],
              FileManager.default.fileExists(atPath: root + "/.offline-lifecycle-test"),
              Bundle.main.bundleIdentifier == "cc.nuoye.inksight.lifecycle-test" else { Darwin.exit(90) }
        UserDefaults.standard.set(false, forKey: "allowScheduledNetwork")
        let runtime = RuntimeManager.shared
        func report(_ text: String) { print("LIFECYCLE: " + text); fflush(stdout) }
        func fail(_ text: String) { report("FAIL " + text); Darwin.exit(91) }
        func awaitState(_ check: @escaping () -> Bool, _ completion: @escaping () -> Void, remaining: Int = 60) {
            if check() { completion(); return }
            if remaining <= 0 { fail("state timeout"); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { awaitState(check, completion, remaining: remaining - 1) }
        }
        awaitState({ runtime.backend == "运行正常" }) {
            var pid = runtime.testBackendPID
            guard let window = self.window else { fail("missing window"); return }
            if self.statusItem == nil {
                guard window.isVisible, !self.windowShouldClose(window), window.isVisible else { fail("status failure hid window"); return }
                report("PASS status failure keeps window")
                NSApplication.shared.terminate(nil)
                return
            }
            guard !window.isVisible, window.attachedSheet == nil else { fail("startup showed a window"); return }
            report("PASS first startup is silent with no visible window")
            self.showWindow()
            guard window.isVisible, !self.windowShouldClose(window), let sheet = window.attachedSheet else { fail("first close hint missing"); return }
            window.endSheet(sheet, returnCode: .alertFirstButtonReturn)
            awaitState({ UserDefaults.standard.bool(forKey: "explainedBackgroundClose") && !window.isVisible }) {
                report("PASS first close hint and hidden window")
                self.showWindow()
                _ = self.windowShouldClose(window)
                guard !window.isVisible, window.attachedSheet == nil, runtime.testBackendPID == pid else { fail("repeat close or backend changed"); return }
                report("PASS repeat close retains unique backend")
                _ = self.applicationShouldHandleReopen(NSApplication.shared, hasVisibleWindows: false)
                _ = self.applicationShouldHandleReopen(NSApplication.shared, hasVisibleWindows: true)
                guard !window.isVisible else { fail("repeated launch showed window"); return }
                runtime.prepareAndStart()
                guard self.window === window, runtime.testBackendPID == pid else { fail("duplicate reopen"); return }
                report("PASS reopen reuses window/backend")
                let duplicate = Process()
                duplicate.executableURL = Bundle.main.executableURL
                duplicate.environment = ProcessInfo.processInfo.environment
                duplicate.standardOutput = FileHandle.nullDevice
                duplicate.standardError = FileHandle.nullDevice
                do { try duplicate.run() } catch { fail("duplicate could not start"); return }
                awaitState({ !duplicate.isRunning }) {
                    guard duplicate.terminationStatus == 0, runtime.testBackendPID == pid else { fail("duplicate created service"); return }
                    report("PASS second App process exits without another backend")
                    runtime.testCrashBackend()
                    awaitState({ runtime.backend == "运行正常" && runtime.testBackendPID != pid }) {
                        pid = runtime.testBackendPID
                        report("PASS backend exit recovers one backend")
                        runtime.testSetFlashInFlight(true)
                        runtime.prepareToQuit { safe in
                            guard !safe, runtime.testBackendPID == pid, runtime.backend == "运行正常" else { fail("critical work did not block exit"); return }
                            runtime.testSetFlashInFlight(false)
                            report("PASS simulated flash blocks exit without killing backend")
                            runtime.togglePause()
                            awaitState({ !runtime.paused && !runtime.transitioning }) {
                                runtime.togglePause()
                                awaitState({ runtime.paused && !runtime.transitioning }) {
                                    guard runtime.testBackendPID == pid, runtime.backend == "运行正常" else { fail("pause stopped management backend"); return }
                                    report("PASS pause leaves management backend")
                                    runtime.togglePause()
                                    awaitState({ !runtime.paused && !runtime.transitioning }) {
                                        guard runtime.testBackendPID == pid else { fail("resume created backend"); return }
                                        report("PASS resume retains backend")
                                        report("PASS requesting graceful AppKit terminate")
                                        NSApplication.shared.terminate(nil)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    #endif
}

final class RuntimeManager: ObservableObject {
    static let shared = RuntimeManager()

    @Published var backend = "未启动"
    @Published var collection = "尚无采集结果"
    @Published var cloud = "未配置"
    @Published var device = "未验证"
    @Published var root = "待检查"
    @Published var detail = "准备本机运行环境"
    @Published var portText = UserDefaults.standard.string(forKey: "servicePort") ?? "18080"
    @Published var macText = UserDefaults.standard.string(forKey: "deviceMac") ?? ""
    @Published var keySummary = "可跳过，默认使用本地寄语"
    @Published var allowNetwork = UserDefaults.standard.bool(forKey: "allowScheduledNetwork")
    @Published var cloudReady = false
    @Published var cloudTestPassed = false
    @Published var paused = UserDefaults.standard.bool(forKey: "servicesPaused")
    @Published var transitioning = false
    @Published var lifecycleError = false
    private let desktopToken = UUID().uuidString + UUID().uuidString
    private var quota: MenuQuota?
    private var quotaTask: URLSessionDataTask?
    private var quotaGeneration = MenuQuotaGeneration()
    private var quotaNextRead = Date.distantPast
    private var quitting = false

    var menuQuotaLabel: String { backend == "运行正常" ? (quota?.label() ?? "-") : "-" }
    var menuQuotaDetail: String {
        let state = menuQuotaLabel == "-" ? (quota?.reason == nil ? "数据已陈旧或尚未就绪" : quota!.detail) : "剩余 " + menuQuotaLabel
        var label = "Codex 7D：" + state
        if let stamp = quota?.lastSuccessAt, stamp.isFinite, stamp > 0 {
            let formatter = DateFormatter(); formatter.dateFormat = "MM-dd HH:mm:ss"
            label += "（本机采集 " + formatter.string(from: Date(timeIntervalSince1970: stamp)) + "）"
        }
        return label
    }

    private func invalidateQuota() {
        quotaGeneration.invalidate()
        quotaTask?.cancel(); quotaTask = nil
        quota = nil; quotaNextRead = .distantPast
    }

    func beginQuit() { quitting = true; quotaGeneration.stop(); invalidateQuota() }
    func cancelQuit() { quitting = false; quotaGeneration.resume(); quotaNextRead = .distantPast }

    func refreshMenuQuota() {
        guard !quitting, backend == "运行正常", backendProcess?.isRunning == true else {
            if quota != nil || quotaTask != nil { invalidateQuota() }
            return
        }
        guard quotaTask == nil, Date() >= quotaNextRead else { return }
        guard let generation = quotaGeneration.begin() else { return }
        let process = backendProcess
        let url = baseURL.appendingPathComponent("api/desktop/quota")
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        request.timeoutInterval = 4
        request.setValue(desktopToken, forHTTPHeaderField: "X-InkSight-Desktop-Token")
        quotaNextRead = Date().addingTimeInterval(15)
        quotaTask = URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self, !self.quitting, self.quotaGeneration.accepts(generation),
                      self.backendProcess === process, self.backend == "运行正常" else { return }
                self.quotaTask = nil
                self.quota = (response as? HTTPURLResponse)?.statusCode == 200
                    ? data.flatMap { try? JSONDecoder().decode(MenuQuota.self, from: $0) } : nil
            }
        }
        quotaTask?.resume()
    }

    var serviceStatus: String {
        if lifecycleError { return "状态核验失败，未强制停止服务" }
        if transitioning { return "正在完成在途任务" }
        if backend != "运行正常" { return "异常或未启动（\(backend)）" }
        if paused { return "自动服务已暂停，控制台仍可用" }
        if !cloudReady { return "待配置；本机服务运行" }
        return "后端运行；\(publisherProcess?.isRunning == true ? "发布循环运行" : "发布未运行")，设备未验证"
    }
    var lastPublicationLabel: String {
        guard let runtime = runtimeURL,
              let data = try? Data(contentsOf: runtime.appendingPathComponent("shared/tools/.cloud_publish_state.json")),
              let state = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let records = state["_desktop_publications"] as? [String: [String: Any]],
              let record = records[macText.replacingOccurrences(of: ":", with: "").replacingOccurrences(of: "-", with: "").uppercased()],
              let timestamp = record["last_success_at"] as? TimeInterval,
              timestamp > 0, timestamp <= Date().timeIntervalSince1970 + 60 else { return "尚无验证记录" }
        let date = Date(timeIntervalSince1970: timestamp)
        let formatter = DateFormatter()
        formatter.dateFormat = "MM-dd HH:mm:ss"
        let suffix = Date().timeIntervalSince(date) > 600 ? "（已陈旧）" : ""
        return formatter.string(from: date) + suffix
    }

    private var runtimeURL: URL?
    private var backendProcess: Process?
    private var publisherProcess: Process?
    private var cloudProbeProcess: Process?
    private var recoveryProcess: Process?
    private var publisherRequested = UserDefaults.standard.bool(forKey: "resumePublisher")
    private var serviceRequested = false
    private var activeNetwork = false
    private var testedMac = ""
    private var restartAttempts = 0
    private var cloudRetryTimer: Timer?
    private var wakeObserver: NSObjectProtocol?
    private var wakeTimer: Timer?
    private var lastRecovery = Date.distantPast
    private var lastAbsolute = mach_absolute_time()
    private var lastContinuous = mach_continuous_time()
    private var healthTimer: Timer?
    private var healthAttempts = 0
    private var currentPort = 18080
    private var lockFD: Int32 = -1
    private var hasPrepared = false
    private var openedBrowser = false
    #if INKSIGHT_LIFECYCLE_TEST
    var testBackendPID: Int32? { backendProcess?.processIdentifier }
    func testCrashBackend() { backendProcess?.terminate() }
    func testSetFlashInFlight(_ active: Bool) {
        guard let runtime = runtimeURL,
              FileManager.default.fileExists(atPath: dataRoot.appendingPathComponent(".offline-lifecycle-test").path)
        else { Darwin.exit(93) }
        let value: [String: Any] = ["builds": [:], "plans": [:], "rollbacks": [:],
            "flashes": active ? ["lifecycle-simulation": ["status": "running"]] : [:]]
        let data = try! JSONSerialization.data(withJSONObject: value)
        try! data.write(to: runtime.appendingPathComponent("shared/backend/state/firmware_tasks.json"), options: .atomic)
    }
    #endif

    private var dataRoot: URL {
        if let override = ProcessInfo.processInfo.environment["INKSIGHT_APP_DATA_ROOT"],
           override.hasPrefix("/") {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("InkSight-GLHF", isDirectory: true)
    }

    private var resources: URL { Bundle.main.resourceURL! }
    private var python: URL { resources.appendingPathComponent("Python/bin/python3.11") }
    private var template: URL { resources.appendingPathComponent("InkSight-Source") }
    private var baseURL: URL { URL(string: "http://127.0.0.1:\(currentPort)")! }

    private func cleanEnvironment() -> [String: String] {
        ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
         "LANG": "zh_CN.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"]
    }

    private func acquireLock() -> Bool {
        do {
            try FileManager.default.createDirectory(at: dataRoot, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        } catch { detail = "无法创建本机用户数据目录"; return false }
        let path = dataRoot.appendingPathComponent(".desktop.lock").path
        lockFD = Darwin.open(path, O_CREAT | O_RDWR, 0o600)
        if lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
            if lockFD >= 0 { Darwin.close(lockFD); lockFD = -1 }
            let ownPID = ProcessInfo.processInfo.processIdentifier
            _ = ownPID // A repeated launch is quiet, including an existing hidden window.
            // A second UI process owns no backend. Exit immediately; SwiftUI's
            // window lifecycle may otherwise keep it alive despite terminate().
            Darwin.exit(0)
        }
        return true
    }

    private func runMaintenance(_ arguments: [String]) throws -> [String: Any] {
        let process = Process()
        let output = Pipe()
        process.executableURL = python
        process.arguments = [template.appendingPathComponent("shared/tools/app_data.py").path,
                             "--data-root", dataRoot.path] + arguments
        process.environment = cleanEnvironment()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw NSError(domain: "InkSight", code: Int(process.terminationStatus),
                             userInfo: [NSLocalizedDescriptionKey: "用户数据准备失败；原有数据未被自动删除"]) }
        return object
    }

    func prepareAndStart() {
        if hasPrepared { return }
        hasPrepared = true
        guard acquireLock() else { return }
        do {
            let result = try runMaintenance(["install", "--template", template.path,
                                             "--version", appVersion])
            guard let path = result["runtime"] as? String else { throw NSError(domain: "InkSight", code: 1) }
            runtimeURL = URL(fileURLWithPath: path, isDirectory: true)
            detail = result["status"] as? String == "installed" ? "本机数据已准备，可从菜单打开配置。" : "继续使用本机数据。"
            refreshConfiguration()
            checkCollection()
            observeWake()
            start()
        } catch { backend = "准备失败"; detail = "无法准备本机运行环境。请保留用户数据目录并查看构建说明。" }
    }

    private func isPortBusy(_ port: Int) -> Bool {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { return false }
        defer { Darwin.close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(port).bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    func start() {
        guard backendProcess == nil, let runtime = runtimeURL else { return }
        serviceRequested = true
        guard let port = Int(portText), (1024...65535).contains(port) else {
            backend = "端口无效"; detail = "请输入 1024-65535 之间的端口。"; return
        }
        if isPortBusy(port) {
            backend = "端口被占用"
            detail = "端口 \(port) 已被其他进程使用。请换一个端口；不会停止或接管其他服务。"
            return
        }
        currentPort = port
        UserDefaults.standard.set(portText, forKey: "servicePort")
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime.appendingPathComponent("shared/backend", isDirectory: true)
        process.arguments = [runtime.appendingPathComponent("shared/tools/app_backend.py").path,
                             "--port", String(port), "--parent-pid",
                             String(ProcessInfo.processInfo.processIdentifier)]
        var environment = cleanEnvironment()
        environment["INKSIGHT_DESKTOP_TOKEN"] = desktopToken
        environment["INKSIGHT_DESKTOP_PAUSED"] = paused ? "1" : "0"
        activeNetwork = allowNetwork
        if !activeNetwork { environment["INKSIGHT_OFFLINE_STARTUP"] = "1" }
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, self.backendProcess === finished else { return }
                self.backendProcess = nil
                self.invalidateQuota()
                self.stopPublisher(preserveRequest: true)
                self.healthTimer?.invalidate()
                self.backend = "已停止"
                self.detail = "后端已退出（代码 \(finished.terminationStatus)）；未影响其他服务。"
                if self.serviceRequested && self.restartAttempts < 3 {
                    self.restartAttempts += 1
                    let delay = Double(self.restartAttempts * 2)
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                        if self.serviceRequested { self.start() }
                    }
                }
            }
        }
        do {
            try process.run()
            backendProcess = process
            backend = "正在启动"
            detail = "本机后端仅监听 127.0.0.1:\(port)。"
            openedBrowser = false
            healthAttempts = 0
            healthTimer?.invalidate()
            healthTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { [weak self] _ in
                self?.pollHealth()
            }
            pollHealth()
        } catch { backend = "启动失败"; detail = "嵌入式运行时无法启动；请检查应用是否完整。" }
    }

    private func pollHealth() {
        guard backendProcess?.isRunning == true else { return }
        healthAttempts += 1
        if healthAttempts > 35 {
            healthTimer?.invalidate()
            backend = "启动超时"
            detail = "后端未在约 28 秒内响应；可重启，不会清空配置。"
            return
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("api/health"))
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let data = data,
                  let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  value["status"] as? String == "ok" else { return }
            DispatchQueue.main.async {
                guard self.backendProcess?.isRunning == true else { return }
                self.healthTimer?.invalidate()
                self.backend = "运行正常"
                self.detail = "后端健康检查通过。采集、云端和设备仍需分别确认。"
                self.checkRoot()
                self.checkCollection()
                if self.publisherRequested && self.cloudReady && !self.paused && !self.transitioning {
                    self.performCloudTest(resume: true)
                }
                // Opening a browser requires an explicit menu/window action.
            }
        }.resume()
    }

    private func checkRoot() {
        root = "App 本机受信任会话，无需管理员账号"
    }

    func checkCollection() {
        guard let runtime = runtimeURL else { collection = "运行环境未就绪"; return }
        let path = runtime.appendingPathComponent("shared/backend/runtime_uploads/unified_feed.json")
        guard let data = try? Data(contentsOf: path),
              let feed = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              feed["schema"] as? String == "unified_feed_v1",
              let generated = feed["generated_at"] as? TimeInterval else {
            collection = "尚无采集结果"
            return
        }
        let age = Date().timeIntervalSince1970 - generated
        guard age >= -60 else { collection = "采集时间异常"; return }
        if age > 3600 {
            collection = "汇总文档已过期"
        } else {
            collection = "汇总文档已生成；来源状态需逐项核验"
        }
    }

    func refreshConfiguration() {
        guard let runtime = runtimeURL else { return }
        let path = runtime.appendingPathComponent("shared/config/inksight_secrets.json")
        guard let data = try? Data(contentsOf: path),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { cloudReady = false; cloud = "未配置"; keySummary = "可跳过，默认使用本地寄语"; return }
        let services = value["services"] as? [String: Any] ?? [:]
        let keys = ["deepseek_api_key", "news_deepseek_api_key", "openai_admin_api_key"]
        let count = keys.filter { !(services[$0] as? String ?? "").isEmpty }.count
        keySummary = count == 0 ? "未配置付费服务，使用本地寄语" : "已配置 \(count) 项可选服务；请在管理端确认授权"
        let settings = value["cloud"] as? [String: Any] ?? [:]
        cloudReady = ["base_url", "user", "password"].allSatisfy {
            !(settings[$0] as? String ?? "").isEmpty
        }
        cloud = cloudReady ? (cloudTestPassed ? "测试通过" : "已填写，未测试") : "未配置"
    }

    func openConfiguration() {
        guard !quitting, backend == "运行正常" else {
            detail = "配置服务尚未就绪，请稍后从菜单重新打开。"
            return
        }
        let entryBase = baseURL
        let owner = backendProcess
        var request = URLRequest(url: entryBase.appendingPathComponent("api/desktop/browser-ticket"),
                                 cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue(desktopToken, forHTTPHeaderField: "X-InkSight-Desktop-Token")
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data,
                  let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let ticket = object["ticket"] as? String,
                  let path = object["entry_path"] as? String,
                  let url = DesktopEntry.url(base: entryBase, path: path, ticket: ticket) else {
                DispatchQueue.main.async { self.detail = "无法创建本机配置会话，请稍后重试。" }
                return
            }
            DispatchQueue.main.async {
                guard !self.quitting, self.backendProcess === owner, self.baseURL == entryBase,
                      self.backend == "运行正常" else { return }
                if !NSWorkspace.shared.open(url) { self.detail = "浏览器未能打开配置，请稍后从菜单重试。" }
            }
        }.resume()
    }

    func savePortAndRestart() {
        quiesce(forQuit: true) { safe in
            guard safe else { return }
            self.stopAll(preservePublishing: true)
            self.restartAttempts = 0
            self.start()
        }
    }

    func setScheduledNetwork(_ value: Bool) {
        allowNetwork = value
        UserDefaults.standard.set(value, forKey: "allowScheduledNetwork")
        detail = value ? "已授权下次启动时运行后台采集；请审查已配置来源与费用。" : "下次启动保持离线采集模式。"
    }

    func importOldSource() {
        guard backendProcess == nil else { detail = "请先停止后端，再导入旧数据。"; return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "检查旧目录"
        guard panel.runModal() == .OK, let source = panel.url else { return }
        do {
            let preview = try runMaintenance(["import", "--source", source.path])
            let items = preview["items"] as? [String] ?? []
            let alert = NSAlert()
            alert.messageText = "只导入所选旧数据？"
            alert.informativeText = items.isEmpty ? "没有发现可导入的配置和状态。" :
                "将复制 \(items.count) 类私有文件并保留原目录。当前应用数据会先备份。"
            alert.addButton(withTitle: "导入并备份")
            alert.addButton(withTitle: "取消")
            guard !items.isEmpty, alert.runModal() == .alertFirstButtonReturn else { return }
            _ = try runMaintenance(["import", "--source", source.path, "--confirm"])
            refreshConfiguration()
            detail = "旧数据已选择性导入，原目录和应用内备份均保留。请启动并检查。"
        } catch { detail = "旧数据导入失败；原有数据未被自动删除。" }
    }

    func testCloud() { performCloudTest(resume: publisherRequested) }

    private func performCloudTest(resume: Bool) {
        guard !paused, !transitioning else { return }
        guard cloudProbeProcess == nil else { return }
        guard backend == "运行正常", cloudReady, let runtime = runtimeURL else {
            cloud = "请先完成配置"; return
        }
        let mac = macText.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = mac.replacingOccurrences(of: ":", with: "")
            .replacingOccurrences(of: "-", with: "")
        guard normalized.range(of: "^[0-9A-Fa-f]{12}$", options: .regularExpression) != nil else {
            cloud = "设备标识无效"; return
        }
        cloud = "正在测试"
        cloudTestPassed = false
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime.appendingPathComponent("shared/tools", isDirectory: true)
        process.arguments = [runtime.appendingPathComponent("shared/tools/host_cycle.py").path,
                             "--mac", mac, "--backend", baseURL.absoluteString,
                             "--probe-webdav"]
        process.environment = cleanEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            cloudProbeProcess = process
            DispatchQueue.global(qos: .userInitiated).async {
                process.waitUntilExit()
                DispatchQueue.main.async {
                    guard self.cloudProbeProcess === process else { return }
                    self.cloudProbeProcess = nil
                    self.cloudTestPassed = process.terminationStatus == 0
                    self.testedMac = self.cloudTestPassed ? mac : ""
                    self.cloud = self.cloudTestPassed ? "上传/回读通过" : "测试失败"
                    self.detail = self.cloudTestPassed ? "云端测试通过；设备是否已拉取仍需实机确认。" :
                        "云端测试未通过。请在管理端检查地址、账号、应用密码和网络。"
                    if resume && self.publisherRequested && self.serviceRequested && !self.paused && !self.transitioning {
                        if self.cloudTestPassed {
                            self.cloudRetryTimer?.invalidate()
                            self.startPublisher()
                        } else {
                            self.cloudRetryTimer?.invalidate()
                            self.cloudRetryTimer = Timer.scheduledTimer(withTimeInterval: 60,
                                repeats: false) { [weak self] _ in
                                    self?.performCloudTest(resume: true)
                                }
                        }
                    }
                }
            }
        } catch { cloud = "测试无法启动" }
    }

    func startPublisher() {
        guard !paused, !transitioning else { return }
        guard publisherProcess == nil, cloudTestPassed, backend == "运行正常",
              let runtime = runtimeURL else { return }
        let mac = macText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard mac == testedMac else { cloud = "设备标识已改变，请重新测试云端"; return }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime.appendingPathComponent("shared/tools", isDirectory: true)
        process.arguments = [runtime.appendingPathComponent("shared/tools/host_cycle.py").path,
                             "--mac", mac,
                             "--backend", baseURL.absoluteString, "--codex", "--parent-pid",
                             String(ProcessInfo.processInfo.processIdentifier)]
        if activeNetwork { process.arguments?.append("--request-queues") }
        process.environment = cleanEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, self.publisherProcess === finished else { return }
                self.publisherProcess = nil
                self.cloud = "发布已停止"
                if self.serviceRequested && self.publisherRequested && !self.paused && !self.transitioning {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.startPublisher() }
                }
            }
        }
        do {
            try process.run()
            publisherProcess = process
            publisherRequested = true
            UserDefaults.standard.set(true, forKey: "resumePublisher")
            UserDefaults.standard.set(macText, forKey: "deviceMac")
            cloud = "发布循环运行中"
            device = "未验证（需实机确认）"
        } catch { cloud = "发布无法启动" }
    }

    func stopPublisher() {
        publisherRequested = false
        UserDefaults.standard.set(false, forKey: "resumePublisher")
        if let process = publisherProcess, process.isRunning { kill(process.processIdentifier, SIGUSR1) }
        cloudRetryTimer?.invalidate()
        cloud = "正在完成本轮发布后停止"
    }

    private func stopPublisher(preserveRequest: Bool) {
        cloudRetryTimer?.invalidate()
        if !preserveRequest {
            publisherRequested = false
            UserDefaults.standard.set(false, forKey: "resumePublisher")
        }
        if let process = publisherProcess {
            if process.isRunning { kill(process.processIdentifier, SIGUSR1) }
            else { publisherProcess = nil }
        }
        if cloudReady { cloud = cloudTestPassed ? "测试通过，发布已停止" : "已填写，未测试" }
    }

    func stop() { togglePause() }

    private func desktopRequest(_ action: String, completion: @escaping ([String: Any]?) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/desktop/service/\(action)"))
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue(desktopToken, forHTTPHeaderField: "X-InkSight-Desktop-Token")
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let value = (response as? HTTPURLResponse)?.statusCode == 200 ?
                data.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] } : nil
            DispatchQueue.main.async { completion(value) }
        }.resume()
    }

    func togglePause() {
        guard !transitioning else { return }
        if paused {
            desktopRequest("resume") { result in
                guard result?["paused"] as? Bool == false else { self.detail = "恢复失败，仍保持暂停。"; return }
                self.paused = false
                self.lifecycleError = false
                UserDefaults.standard.set(false, forKey: "servicesPaused")
                self.detail = "自动采集、调度已恢复，沿用原出刊去重记录。"
                if self.publisherRequested && self.cloudReady { self.performCloudTest(resume: true) }
            }
        } else {
            quiesce(forQuit: false) { safe in
                self.detail = safe ? "自动采集、出刊和发布已暂停，控制台仍可用。" :
                    "新任务已暂停；在途任务或状态核验未完成，未强制终止。"
            }
        }
    }

    func prepareToQuit(_ completion: @escaping (Bool) -> Void) { quiesce(forQuit: true, completion: completion) }

    private func quiesce(forQuit: Bool, completion: @escaping (Bool) -> Void) {
        guard !transitioning else { completion(false); return }
        if backendProcess == nil {
            let owned = [publisherProcess, cloudProbeProcess, recoveryProcess].compactMap { $0 }
            let safe = !owned.contains(where: { $0.isRunning }) && !localCriticalWorkExists()
            if !safe { detail = "后端不可用且在途任务未核验，已阻止停止。" }
            completion(safe)
            return
        }
        transitioning = true
        lifecycleError = false
        cloudRetryTimer?.invalidate()
        #if INKSIGHT_LIFECYCLE_TEST
        let deadline = Date().addingTimeInterval(5)
        #else
        let deadline = Date().addingTimeInterval(240)
        #endif
        desktopRequest("pause") { result in
            guard result != nil else {
                self.transitioning = false
                self.lifecycleError = true
                self.detail = "无法核验在途任务，已阻止停止；服务未被强制关闭。"
                completion(false)
                return
            }
            if !forQuit {
                self.paused = true
                UserDefaults.standard.set(true, forKey: "servicesPaused")
            }
            if let process = self.publisherProcess, process.isRunning { kill(process.processIdentifier, SIGUSR1) }
            self.detail = "正在等待在途采集、出刊、烧录和发布安全完成。"
            self.waitForSafeStop(deadline: deadline, completion: completion)
        }
    }

    private func localCriticalWorkExists() -> Bool {
        guard let runtime = runtimeURL else { return false }
        for (name, groups) in [("firmware_tasks.json", ["builds", "flashes", "rollbacks"]),
                               ("manual_issue_tasks.json", ["tasks"])] {
            let url = runtime.appendingPathComponent("shared/backend/state/\(name)")
            if !FileManager.default.fileExists(atPath: url.path) { continue }
            guard let data = try? Data(contentsOf: url),
                  let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { return true }
            for group in groups {
                guard let raw = object[group] else { continue }
                guard let jobs = raw as? [String: [String: Any]] else { return true }
                if jobs.values.contains(where: {
                    guard let status = $0["status"] as? String else { return true }
                    return ["queued", "running", "waiting_heartbeat"].contains(status)
                }) { return true }
            }
        }
        return false
    }

    private func waitForSafeStop(deadline: Date, completion: @escaping (Bool) -> Void) {
        desktopRequest("status") { result in
            let processes = [self.publisherProcess, self.cloudProbeProcess, self.recoveryProcess].compactMap { $0 }
            if result?["safe_to_stop"] as? Bool == true && !processes.contains(where: { $0.isRunning }) {
                self.transitioning = false
                completion(true)
            } else if Date() >= deadline {
                self.transitioning = false
                self.paused = true
                UserDefaults.standard.set(true, forKey: "servicesPaused")
                self.detail = "在途任务尚未安全结束或状态不可用，已阻止退出。新自动任务保持暂停。"
                completion(false)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    self.waitForSafeStop(deadline: deadline, completion: completion)
                }
            }
        }
    }

    func shutdown() { stopAll(preservePublishing: true) }

    private func stopAll(preservePublishing: Bool) {
        invalidateQuota()
        serviceRequested = false
        stopPublisher(preserveRequest: preservePublishing)
        for process in [cloudProbeProcess, recoveryProcess].compactMap({ $0 }) {
            if process.isRunning { process.terminate(); process.waitUntilExit() }
        }
        cloudProbeProcess = nil
        recoveryProcess = nil
        healthTimer?.invalidate()
        if let process = backendProcess {
            backendProcess = nil
            if process.isRunning { process.terminate(); process.waitUntilExit() }
        }
        backend = "已停止"
        detail = "本应用启动的服务已停止；不会停止其他源码部署。"
    }

    private func observeWake() {
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
                self?.scheduleWakeRecovery()
            }
        wakeTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            var info = mach_timebase_info_data_t()
            mach_timebase_info(&info)
            let absolute = mach_absolute_time()
            let continuous = mach_continuous_time()
            let elapsed = Double(continuous - self.lastContinuous) - Double(absolute - self.lastAbsolute)
            let suspended = elapsed * Double(info.numer) / Double(info.denom) / 1_000_000_000
            self.lastAbsolute = absolute
            self.lastContinuous = continuous
            if suspended >= 20 { self.scheduleWakeRecovery() }
        }
    }

    private func scheduleWakeRecovery() {
        guard serviceRequested, activeNetwork, !paused, !transitioning, backend == "运行正常",
              Date().timeIntervalSince(lastRecovery) >= 15 else { return }
        lastRecovery = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.recoverAfterWake() }
    }

    private func recoverAfterWake() {
        guard serviceRequested, activeNetwork, !paused, !transitioning, backend == "运行正常", recoveryProcess == nil,
              let runtime = runtimeURL else { return }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime.appendingPathComponent("shared/tools", isDirectory: true)
        process.arguments = [runtime.appendingPathComponent("shared/tools/host_cycle.py").path,
                             "--backend", baseURL.absoluteString, "--recover-host", "--parent-pid",
                             String(ProcessInfo.processInfo.processIdentifier)]
        process.environment = cleanEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                if self?.recoveryProcess === finished { self?.recoveryProcess = nil }
            }
        }
        do { try process.run(); recoveryProcess = process }
        catch { detail = "唤醒恢复未能启动；正常采集仍按原计划执行。" }
    }
}

private struct StatusLine: View {
    let name: String
    let value: String
    var body: some View {
        HStack {
            Text(name).foregroundStyle(.secondary)
            Spacer()
            Text(value).fontWeight(.semibold)
        }
        .font(.system(size: 13))
        .padding(.vertical, 5)
    }
}

private struct ControlView: View {
    @ObservedObject var runtime: RuntimeManager

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("InkSight").font(.system(size: 27, weight: .bold))
                        Text("本机发布端").font(.system(size: 13)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(runtime.backend).font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(inkBlue.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))
                }

                Text(runtime.detail).font(.system(size: 13)).foregroundStyle(.secondary)

                VStack(spacing: 0) {
                    StatusLine(name: "后端健康", value: runtime.backend)
                    Divider()
                    StatusLine(name: "采集新鲜度", value: runtime.collection)
                    Divider()
                    StatusLine(name: "云端发布", value: runtime.cloud)
                    Divider()
                    StatusLine(name: "设备连接", value: runtime.device)
                }

                HStack {
                    Text("端口").font(.system(size: 13))
                    TextField("18080", text: $runtime.portText).frame(width: 76)
                    Button("启动", action: runtime.start).disabled(runtime.backend == "运行正常")
                    Button(runtime.paused ? "恢复" : "暂停", action: runtime.togglePause)
                        .disabled(runtime.backend != "运行正常" || runtime.transitioning)
                    Button("重启", action: runtime.savePortAndRestart)
                    Spacer()
                    Button("打开配置页", action: runtime.openConfiguration)
                        .buttonStyle(.borderedProminent).tint(inkBlue)
                        .disabled(runtime.backend != "运行正常")
                }

                GroupBox("首次配置") {
                    VStack(alignment: .leading, spacing: 13) {
                        HStack {
                            VStack(alignment: .leading) {
                                Text("本机配置").fontWeight(.semibold)
                                Text("从菜单按需打开，无需账号密码。\(runtime.root)")
                                    .font(.system(size: 12)).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("打开", action: runtime.openConfiguration)
                                .disabled(runtime.backend != "运行正常")
                        }
                        Divider()
                        HStack {
                            VStack(alignment: .leading) {
                                Text("数据服务").fontWeight(.semibold)
                                Text(runtime.keySummary).font(.system(size: 12)).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("刷新状态", action: runtime.refreshConfiguration)
                        }
                        Toggle("允许下次启动运行已配置的后台网络采集", isOn: Binding(
                            get: { runtime.allowNetwork }, set: { runtime.setScheduledNetwork($0) }))
                            .font(.system(size: 12))
                        Text("默认不开启。启用后也处理设备补拉请求与主机唤醒恢复。请审查来源、密钥和费用；当前运行需重启才生效。")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                        HStack {
                            Text("采集结果独立于后端健康；未启用网络采集时可保持无结果。")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                            Spacer()
                            Button("检查采集", action: runtime.checkCollection)
                        }
                        Divider()
                        Text("云端发布与设备").fontWeight(.semibold)
                        Text("先在网页管理端填写 WebDAV 凭据。下面的测试仅验证云端上传、回读与清理，不会调用付费模型；设备画面仍需实机确认。")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                        HStack {
                            TextField("设备 MAC（12 位十六进制）", text: $runtime.macText)
                            Button("测试云端", action: runtime.testCloud)
                                .disabled(!runtime.cloudReady || runtime.backend != "运行正常")
                            Button("启动发布", action: runtime.startPublisher)
                                .disabled(!runtime.cloudTestPassed || runtime.backend != "运行正常")
                            Button("停止发布", action: runtime.stopPublisher)
                        }
                    }
                    .padding(8)
                }

                HStack {
                    Button("选择性导入旧源码数据", action: runtime.importOldSource)
                        .disabled(runtime.backend == "运行正常")
                    Spacer()
                    Text("关闭窗口后仍在菜单栏运行；退出才停止服务。暂停会停止新自动任务并等待在途任务完成。Mac 休眠期间不能保证定时发布。")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .padding(24)
            .frame(maxWidth: 840)
            .frame(maxWidth: .infinity)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}
