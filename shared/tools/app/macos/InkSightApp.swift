import AppKit
import Darwin
import Foundation
import SwiftUI

private let appVersion = "v0.1.0-test.4"
private let inkBlue = Color(red: 0.19, green: 0.37, blue: 0.53)

@main
final class InkSightDesktopApp: NSObject, NSApplicationDelegate {
    private static let delegate = InkSightDesktopApp()
    private var window: NSWindow?

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
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
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApplication.shared.activate(ignoringOtherApps: true)
        RuntimeManager.shared.prepareAndStart()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { RuntimeManager.shared.stop() }
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

    private var runtimeURL: URL?
    private var backendProcess: Process?
    private var publisherProcess: Process?
    private var healthTimer: Timer?
    private var healthAttempts = 0
    private var currentPort = 18080
    private var lockFD: Int32 = -1
    private var hasPrepared = false
    private var openedBrowser = false

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
            NSRunningApplication.runningApplications(withBundleIdentifier: "cc.nuoye.inksight")
                .first(where: { $0.processIdentifier != ownPID })?
                .activate(options: [.activateAllWindows])
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
            detail = result["status"] as? String == "installed" ? "本机数据已准备。请创建管理员。" : "继续使用本机数据。"
            refreshConfiguration()
            checkCollection()
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
        if !allowNetwork { environment["INKSIGHT_OFFLINE_STARTUP"] = "1" }
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, self.backendProcess === finished else { return }
                self.backendProcess = nil
                self.stopPublisher()
                self.healthTimer?.invalidate()
                self.backend = "已停止"
                self.detail = "后端已退出（代码 \(finished.terminationStatus)）；未影响其他服务。"
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
                if !self.openedBrowser {
                    self.openedBrowser = true
                    if ProcessInfo.processInfo.environment["INKSIGHT_TEST_NO_BROWSER"] != "1" {
                        self.openConfiguration()
                    }
                }
            }
        }.resume()
    }

    private func checkRoot() {
        let url = baseURL.appendingPathComponent("api/auth/local-root-bootstrap")
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { return }
            DispatchQueue.main.async { self.root = (value["needed"] as? Bool == true) ? "需要创建" : "已创建" }
        }.resume()
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

    func openConfiguration() { NSWorkspace.shared.open(baseURL) }

    func savePortAndRestart() {
        stop()
        start()
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

    func testCloud() {
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
            DispatchQueue.global(qos: .userInitiated).async {
                process.waitUntilExit()
                DispatchQueue.main.async {
                    self.cloudTestPassed = process.terminationStatus == 0
                    self.cloud = self.cloudTestPassed ? "上传/回读通过" : "测试失败"
                    self.detail = self.cloudTestPassed ? "云端测试通过；设备是否已拉取仍需实机确认。" :
                        "云端测试未通过。请在管理端检查地址、账号、应用密码和网络。"
                }
            }
        } catch { cloud = "测试无法启动" }
    }

    func startPublisher() {
        guard publisherProcess == nil, cloudTestPassed, backend == "运行正常",
              let runtime = runtimeURL else { return }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime.appendingPathComponent("shared/tools", isDirectory: true)
        process.arguments = [runtime.appendingPathComponent("shared/tools/host_cycle.py").path,
                             "--mac", macText.trimmingCharacters(in: .whitespacesAndNewlines),
                             "--backend", baseURL.absoluteString, "--codex", "--parent-pid",
                             String(ProcessInfo.processInfo.processIdentifier)]
        process.environment = cleanEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, self.publisherProcess === finished else { return }
                self.publisherProcess = nil
                self.cloud = "发布已停止"
            }
        }
        do {
            try process.run()
            publisherProcess = process
            UserDefaults.standard.set(macText, forKey: "deviceMac")
            cloud = "发布循环运行中"
            device = "未验证（需实机确认）"
        } catch { cloud = "发布无法启动" }
    }

    func stopPublisher() {
        if let process = publisherProcess {
            publisherProcess = nil
            if process.isRunning { process.terminate() }
        }
        if cloudReady { cloud = cloudTestPassed ? "测试通过，发布已停止" : "已填写，未测试" }
    }

    func stop() {
        stopPublisher()
        healthTimer?.invalidate()
        if let process = backendProcess {
            backendProcess = nil
            if process.isRunning { process.terminate() }
        }
        backend = "已停止"
        detail = "本应用启动的服务已停止；不会停止其他源码部署。"
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
                    Button("停止", action: runtime.stop).disabled(runtime.backend == "已停止")
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
                                Text("管理员").fontWeight(.semibold)
                                Text("首次在浏览器创建，无通用默认密码。当前：\(runtime.root)")
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
                        Text("默认不开启。启用前请在管理端审查新闻来源、密钥和可能的费用；当前运行需重启才生效。")
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
                    Text("关闭窗口会停止本应用服务。Mac 休眠期间不能保证定时发布。")
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
