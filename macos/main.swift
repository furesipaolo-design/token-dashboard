// Token Dashboard — native macOS shell.
//
// A thin AppKit wrapper around the stdlib-only Python backend bundled in
// Contents/Resources/backend. On launch it either attaches to an already
// running dashboard server on the fixed port, or spawns its own with
// `--scan-async` (serve immediately, ingest transcripts in the background),
// then shows the UI in a WKWebView window. Quitting the app terminates the
// server it spawned.
//
// The port is fixed (not ephemeral) on purpose: the web origin must stay
// stable across launches or the frontend's localStorage (first-run flag,
// per-view preferences) would reset every time.

import AppKit
import WebKit

let HOST = "127.0.0.1"
let PORT = ProcessInfo.processInfo.environment["TOKEN_DASHBOARD_PORT"].flatMap { Int($0) } ?? 8377
let DASHBOARD_URL = URL(string: "http://\(HOST):\(PORT)/")!
let HEALTH_URL = URL(string: "http://\(HOST):\(PORT)/api/plan")!
let STARTUP_DEADLINE: TimeInterval = 30

func logFileURL() -> URL {
    let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("token-dashboard.log")
}

func findPython() -> String? {
    var candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
    if let override = ProcessInfo.processInfo.environment["TOKEN_DASHBOARD_PYTHON"] {
        candidates.insert(override, at: 0)
    }
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
}

/// GET /api/plan and report whether a dashboard server answered.
func probeDashboard(timeout: TimeInterval, _ completion: @escaping (Bool) -> Void) {
    var req = URLRequest(url: HEALTH_URL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
    req.httpMethod = "GET"
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            && data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["plan"] != nil
        completion(ok)
    }.resume()
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var spinner: NSProgressIndicator!
    var statusLabel: NSTextField!
    var serverProcess: Process?
    var isQuitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startup()
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        isQuitting = true
        if let p = serverProcess, p.isRunning {
            p.terminate() // SIGTERM — server.py shuts down gracefully
            p.waitUntilExit()
        }
    }

    // MARK: UI

    func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Token Dashboard"
        window.minSize = NSSize(width: 760, height: 480)
        window.setFrameAutosaveName("TokenDashboardMain")
        window.center()

        let conf = WKWebViewConfiguration()
        conf.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: conf)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true

        let loading = NSView()
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.startAnimation(nil)
        statusLabel = NSTextField(labelWithString: "Avvio del server locale…")
        statusLabel.alignment = .center
        statusLabel.textColor = .secondaryLabelColor
        for v in [spinner!, statusLabel!] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            loading.addSubview(v)
        }
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: loading.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: loading.centerYAnchor, constant: -16),
            spinner.widthAnchor.constraint(equalToConstant: 32),
            spinner.heightAnchor.constraint(equalToConstant: 32),
            statusLabel.centerXAnchor.constraint(equalTo: loading.centerXAnchor),
            statusLabel.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 12),
        ])
        window.contentView = loading

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showDashboard() {
        window.contentView = webView
        webView.load(URLRequest(url: DASHBOARD_URL))
    }

    func fail(_ message: String) {
        spinner.stopAnimation(nil)
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Token Dashboard non è partita"
        alert.informativeText = message + "\n\nLog: \(logFileURL().path)"
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: server lifecycle

    func startup() {
        probeDashboard(timeout: 0.6) { running in
            DispatchQueue.main.async {
                if running {
                    self.showDashboard() // a dashboard server is already up — reuse it
                } else {
                    self.spawnServer()
                }
            }
        }
    }

    func spawnServer() {
        guard let backend = Bundle.main.resourceURL?.appendingPathComponent("backend"),
              FileManager.default.fileExists(atPath: backend.appendingPathComponent("cli.py").path) else {
            fail("Bundle corrotto: backend Python non trovato dentro l'app.")
            return
        }
        guard let python = findPython() else {
            fail("python3 non trovato. Installa i Command Line Tools con:\nxcode-select --install")
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [backend.appendingPathComponent("cli.py").path, "dashboard", "--no-open", "--scan-async"]
        p.currentDirectoryURL = backend
        var env = ProcessInfo.processInfo.environment
        env["HOST"] = HOST
        env["PORT"] = String(PORT)
        p.environment = env

        FileManager.default.createFile(atPath: logFileURL().path, contents: nil)
        if let log = try? FileHandle(forWritingTo: logFileURL()) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, !self.isQuitting else { return }
                self.fail("Il server locale si è chiuso in modo inatteso (exit \(proc.terminationStatus)).")
            }
        }
        do {
            try p.run()
        } catch {
            fail("Impossibile avviare python3: \(error.localizedDescription)")
            return
        }
        serverProcess = p
        waitUntilHealthy(deadline: Date().addingTimeInterval(STARTUP_DEADLINE))
    }

    func waitUntilHealthy(deadline: Date) {
        probeDashboard(timeout: 1) { ok in
            DispatchQueue.main.async {
                if ok {
                    self.showDashboard()
                } else if Date() > deadline {
                    self.fail("Il server non risponde su \(DASHBOARD_URL.absoluteString).")
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                        self.waitUntilHealthy(deadline: deadline)
                    }
                }
            }
        }
    }

    // MARK: navigation — keep the webview on the local dashboard, push the rest to the browser

    func isLocal(_ url: URL) -> Bool {
        url.host == HOST || url.host == "localhost"
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, !isLocal(url),
           ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) {
                webView.load(URLRequest(url: url))
            } else {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }

    // MARK: menu

    @objc func reloadPage(_ sender: Any?) { webView?.reload() }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Token Dashboard",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Token Dashboard",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Token Dashboard",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewItem.submenu = viewMenu
        viewMenu.addItem(withTitle: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "Window")
        windowItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "Minimize",
                           action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
