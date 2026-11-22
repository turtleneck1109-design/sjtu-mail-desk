import AppKit
import Foundation

let resources = Bundle.main.resourceURL!
let engineURL = resources.appendingPathComponent("engine/sjtu-mail-engine")

// CI and packaging verification execute the same bundled engine with no UI,
// no browser and no mail delivery. This path never searches PATH for Python.
if CommandLine.arguments.contains("--self-test") {
    let task = Process()
    task.executableURL = engineURL
    task.arguments = ["--self-test"]
    do {
        try task.run()
        task.waitUntilExit()
        exit(task.terminationStatus)
    } catch {
        fputs("Bundled engine could not start.\n", stderr)
        exit(1)
    }
}

final class EngineSession {
    let process = Process()
    let output = Pipe()
    var buffer = Data()
    var url: URL?
    var wantsOpen = true
    let demo: Bool
    init(demo: Bool) { self.demo = demo }
}

final class MailDeskDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    let status = NSTextField(wrappingLabelWithString: "准备就绪 · 已内置 Python，无需终端")
    var sessions: [String: EngineSession] = [:]
    var quitting = false
    var quitRequested = false
    let normalButton = NSButton(title: "打开邮件工作台", target: nil, action: nil)
    let demoButton = NSButton(title: "体验演示模式", target: nil, action: nil)
    var supportRoot: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SJTUMailDesk", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
    }

    func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于交邮", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "隐藏交邮", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出交邮", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = NSMenuItem(title: "工作区", action: nil, keyEquivalent: "")
        file.submenu = NSMenu(title: "工作区")
        file.submenu!.addItem(withTitle: "打开邮件工作台", action: #selector(openNormal), keyEquivalent: "o")
        file.submenu!.addItem(withTitle: "体验演示模式", action: #selector(openDemo), keyEquivalent: "d")
        file.submenu!.addItem(withTitle: "使用说明", action: #selector(openHelp), keyEquivalent: "?")
        mainMenu.addItem(file)
        let edit = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        edit.submenu = NSMenu(title: "编辑")
        edit.submenu!.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.submenu!.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        mainMenu.addItem(edit)
        NSApp.mainMenu = mainMenu
        for menu in [appMenu, file.submenu!] {
            for item in menu.items where item.action == #selector(about) || item.action == #selector(openNormal) || item.action == #selector(openDemo) || item.action == #selector(openHelp) {
                item.target = self
            }
        }
    }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 540),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "交邮 · 本机邮件工作台"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        let content = NSView()
        window.contentView = content
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 38),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -38),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 28)
        ])
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: "envelope.badge.shield.half.filled", accessibilityDescription: "本机邮件") ?? NSImage(systemSymbolName: "envelope.fill", accessibilityDescription: "邮件")
        icon.contentTintColor = NSColor(red: 0.35, green: 0.29, blue: 0.84, alpha: 1)
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 54).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 54).isActive = true
        stack.addArrangedSubview(icon)
        let title = NSTextField(labelWithString: "每封邮件，都有清晰的去向")
        title.font = NSFont.systemFont(ofSize: 24, weight: .semibold)
        stack.addArrangedSubview(title)
        let subtitle = NSTextField(wrappingLabelWithString: "批量导入 .eml · 核对收件人与附件 · 确认后发送")
        subtitle.font = NSFont.systemFont(ofSize: 14)
        subtitle.textColor = .secondaryLabelColor
        subtitle.alignment = .center
        stack.addArrangedSubview(subtitle)
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 12
        normalButton.target = self; normalButton.action = #selector(openNormal)
        demoButton.target = self; demoButton.action = #selector(openDemo)
        for button in [normalButton, demoButton] {
            button.bezelStyle = .rounded
            button.controlSize = .large
            button.font = NSFont.systemFont(ofSize: 15, weight: .medium)
            button.heightAnchor.constraint(equalToConstant: 38).isActive = true
            buttons.addArrangedSubview(button)
        }
        normalButton.keyEquivalent = "\r"
        stack.addArrangedSubview(buttons)
        status.font = NSFont.systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        stack.addArrangedSubview(status)
        let note = NSTextField(wrappingLabelWithString: "工作台在默认浏览器中打开，数据只保存在这台 Mac。\n演示模式不会发送邮件，也不需要真实密码。\n从旧版升级时，请先按说明迁移 data 文件夹。")
        note.font = NSFont.systemFont(ofSize: 13)
        note.textColor = .secondaryLabelColor
        note.alignment = .center
        stack.addArrangedSubview(note)
        let utilities = NSStackView()
        utilities.orientation = .horizontal
        utilities.spacing = 16
        for (name, action) in [("使用说明", #selector(openHelp)), ("打开数据文件夹", #selector(openData)), ("退出交邮", #selector(exitApp))] {
            let button = NSButton(title: name, target: self, action: action)
            button.bezelStyle = .rounded
            utilities.addArrangedSubview(button)
        }
        stack.addArrangedSubview(utilities)
        let version = NSTextField(labelWithString: "v1.1 · Apple 芯片与 Intel 通用版 · 未经 Apple 公证")
        version.font = NSFont.systemFont(ofSize: 11)
        version.textColor = .tertiaryLabelColor
        stack.addArrangedSubview(version)
        window.makeKeyAndOrderFront(nil)
    }

    @objc func openNormal() { start(demo: false) }
    @objc func openDemo() { start(demo: true) }
    @objc func exitApp() { NSApp.terminate(nil) }
    @objc func openHelp() {
        NSWorkspace.shared.open(resources.appendingPathComponent("Mac应用使用说明.txt"))
    }
    @objc func openData() {
        do {
            try FileManager.default.createDirectory(at: supportRoot, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            NSWorkspace.shared.open(supportRoot)
        } catch { showError("无法打开数据目录，请检查用户目录的访问权限。") }
    }
    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "交邮", .applicationVersion: "1.1.0",
            .version: "1.1.0", .credits: NSAttributedString(string: "本机批量邮件工作台\n内置 Python · 默认浏览器界面\n项目：https://github.com/turtleneck1109-design/sjtu-mail-desk")
        ])
    }

    func start(demo: Bool) {
        guard !quitting && !quitRequested else { return }
        let key = demo ? "demo" : "real"
        if let existing = sessions[key] {
            if let url = existing.url { NSWorkspace.shared.open(url) }
            else { existing.wantsOpen = true; status.stringValue = "正在启动，请稍候…" }
            return
        }
        let session = EngineSession(demo: demo)
        session.process.executableURL = engineURL
        session.process.arguments = ["--no-browser", "--data-dir", supportRoot.appendingPathComponent(demo ? "demo_data" : "data").path] + (demo ? ["--demo"] : [])
        var environment = ProcessInfo.processInfo.environment
        for name in ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"] { environment.removeValue(forKey: name) }
        session.process.environment = environment
        session.process.standardOutput = session.output
        session.process.standardError = FileHandle.nullDevice
        session.output.fileHandleForReading.readabilityHandler = { [weak self, weak session] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self, let session = session else { return }
                session.buffer.append(data)
                while let end = session.buffer.firstIndex(of: 10) {
                    let line = String(data: session.buffer[..<end], encoding: .utf8) ?? ""
                    session.buffer.removeSubrange(...end)
                    if line.hasPrefix("页面地址：http://127.0.0.1:"),
                       let url = URL(string: String(line.dropFirst("页面地址：".count))),
                       url.host == "127.0.0.1", url.scheme == "http" {
                        session.url = url
                        self.status.stringValue = demo ? "演示工作区已启动 · 不发送真实邮件" : "邮件工作台已启动 · 发送前需要明确确认"
                        if session.wantsOpen && !self.quitting && !self.quitRequested {
                            NSWorkspace.shared.open(url)
                            session.wantsOpen = false
                        }
                    }
                }
                if session.buffer.count > 8192 { session.buffer = Data() }
            }
        }
        session.process.terminationHandler = { [weak self, weak session] process in
            DispatchQueue.main.async {
                guard let self = self, let session = session else { return }
                session.output.fileHandleForReading.readabilityHandler = nil
                self.sessions.removeValue(forKey: key)
                if self.quitting {
                    if self.sessions.isEmpty { NSApp.reply(toApplicationShouldTerminate: true) }
                } else if process.terminationStatus != 0 || session.url == nil {
                    self.status.stringValue = "启动失败或工作区已被其他实例占用"
                    self.showError("无法启动工作区。请先关闭其他交邮实例，并检查数据文件夹是否可写。")
                } else {
                    self.status.stringValue = "工作区已停止，可以重新打开。"
                }
            }
        }
        sessions[key] = session
        status.stringValue = "正在启动本机工作区…"
        do { try session.process.run() }
        catch {
            sessions.removeValue(forKey: key)
            session.output.fileHandleForReading.readabilityHandler = nil
            status.stringValue = "无法启动内置运行环境"
            showError("未能启动内置运行环境。请重新下载并保留完整的交邮.app。")
        }
    }

    func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "交邮"
        alert.informativeText = message
        alert.addButton(withTitle: "知道了")
        alert.runModal()
    }

    // Ask only if a task is actively submitting, then let the engine finish the
    // current message and save its result before the application exits.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if sessions.isEmpty { return .terminateNow }
        if quitting || quitRequested { return .terminateLater }
        quitRequested = true
        normalButton.isEnabled = false; demoButton.isEnabled = false
        status.stringValue = "正在检查运行中的任务…"
        let group = DispatchGroup()
        var active = false
        var failed = false
        for session in sessions.values {
            guard let url = session.url, let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let token = parts.fragment?.replacingOccurrences(of: "token=", with: ""),
                  let stateURL = URL(string: "http://127.0.0.1:\(parts.port!)/api/state") else { failed = true; continue }
            var request = URLRequest(url: stateURL)
            request.setValue(token, forHTTPHeaderField: "X-MailDesk-Token")
            request.timeoutInterval = 4
            group.enter()
            URLSession.shared.dataTask(with: request) { data, _, error in
                DispatchQueue.main.async {
                    if let data = data, error == nil,
                       let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        if let job = state["job"] as? [String: Any], let status = job["status"] as? String,
                           ["running", "stopping"].contains(status) { active = true }
                    } else { failed = true }
                    group.leave()
                }
            }.resume()
        }
        group.notify(queue: .main) { [weak self] in
            guard let self = self else { return }
            if active || failed {
                let alert = NSAlert()
                alert.messageText = active ? "有邮件正在提交，仍要退出吗？" : "无法确认任务状态，仍要退出吗？"
                alert.informativeText = "退出将停止后续发送，并等待当前提交结束后保存结果。已经提交的邮件无法撤回。"
                alert.addButton(withTitle: "停止后续发送并退出")
                alert.addButton(withTitle: "继续运行")
                if alert.runModal() != .alertFirstButtonReturn {
                    self.quitRequested = false
                    self.normalButton.isEnabled = true; self.demoButton.isEnabled = true
                    self.status.stringValue = "已继续运行"
                    NSApp.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
            self.quitting = true
            self.status.stringValue = "正在停止后续发送，等待当前提交保存结果…"
            if self.sessions.isEmpty { NSApp.reply(toApplicationShouldTerminate: true) }
            else { for session in self.sessions.values where session.process.isRunning { session.process.terminate() } }
        }
        return .terminateLater
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)
        return false
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }
}

let application = NSApplication.shared
let delegate = MailDeskDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
