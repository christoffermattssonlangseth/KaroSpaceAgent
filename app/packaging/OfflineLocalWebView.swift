import AppKit
import WebKit
import JavaScriptCore
import CoreGraphics
import Darwin

// The in-process WebKit view renders only the bundled page. Unlike WKWebView,
// it does not need a separate network/content XPC process to display local HTML.
// App Sandbox has no network entitlements; no remote browser or cloud fallback.
final class LocalWebView: NSObject, NSApplicationDelegate, WebFrameLoadDelegate, WebUIDelegate, WebPolicyDelegate, NSWindowDelegate {
    var window: NSWindow!
    var web: WebView!
    var context: JSContext!
    var ready = false
    var pending = [String]()
    var buffer = Data()
    var bridge = ""
    let smoke = CommandLine.arguments.contains("--smoke-test")

    func emit(_ value: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: value) else { return }
        data.append(10); FileHandle.standardOutput.write(data)
    }

    func installMenus() {
        let menu = NSMenu()
        let application = NSMenuItem()
        application.submenu = NSMenu()
        application.submenu!.addItem(withTitle: "Quit KaroSpace Offline", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(application)

        let edit = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        edit.submenu = NSMenu(title: "Edit")
        // Leave targets nil so AppKit routes commands to the focused HTML field.
        // No JavaScript clipboard bridge or extra sandbox permissions are needed.
        edit.submenu!.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.submenu!.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.submenu!.addItem(.separator())
        for (title, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.submenu!.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        menu.addItem(edit)
        NSApp.mainMenu = menu
    }

    func editingShortcutsSmokeTest() -> Bool {
        // Exercise native keyboard routing without reading or replacing the user's clipboard.
        context.evaluateScript("document.getElementById('input').disabled=false; document.getElementById('input').value='Synthetic shortcut check'; document.getElementById('input').focus(); document.getElementById('input').setSelectionRange(0,0);")
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0,
                                          windowNumber: window.windowNumber, context: nil, characters: "a",
                                          charactersIgnoringModifiers: "a", isARepeat: false, keyCode: 0) else { return false }
        let handled = NSApp.mainMenu?.performKeyEquivalent(with: event) == true
        let selected = context.evaluateScript("document.getElementById('input').selectionStart===0 && document.getElementById('input').selectionEnd===document.getElementById('input').value.length")?.toBool() == true
        let paste = NSApp.mainMenu?.item(withTitle: "Edit")?.submenu?.item(withTitle: "Paste")
        let pasteRouted = paste?.action == Selector(("paste:")) && paste?.keyEquivalent == "v"
            && paste?.keyEquivalentModifierMask == .command && NSApp.target(forAction: Selector(("paste:"))) != nil
        context.evaluateScript("document.getElementById('input').value='';")
        return handled && selected && pasteRouted
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        var denied = fd < 0 && (errno == EPERM || errno == EACCES)
        if fd >= 0 {
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); address.sin_family = sa_family_t(AF_INET)
            address.sin_port = UInt16(9).bigEndian; address.sin_addr.s_addr = inet_addr("127.0.0.1")
            denied = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { target in
                    let result = Darwin.connect(fd, target, socklen_t(MemoryLayout<sockaddr_in>.size))
                    return result == -1 && (errno == EPERM || errno == EACCES)
                }
            }
            Darwin.close(fd)
        }
        guard denied else { emit(["ui_error": "ui_network_isolation_failed"]); exit(2) }
        let resources = Bundle.main.resourceURL!
        guard let script = try? String(contentsOf: resources.appendingPathComponent("offline-bridge.js"), encoding: .utf8),
              let html = try? String(contentsOf: resources.appendingPathComponent("index.html"), encoding: .utf8) else {
            emit(["ui_error": "missing_ui_assets"]); exit(2)
        }
        bridge = script
        installMenus()
        web = WebView(frame: .zero, frameName: nil, groupName: nil)
        web.frameLoadDelegate = self; web.uiDelegate = self; web.policyDelegate = self
        web.preferences.javaScriptCanOpenWindowsAutomatically = false
        web.preferences.privateBrowsingEnabled = true
        web.isContinuousSpellCheckingEnabled = false
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 780), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "KaroSpace Agent — Offline"; window.isReleasedWhenClosed = false; window.delegate = self
        window.contentView = web; window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        let csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; font-src 'none'; form-action 'none'; base-uri 'none'\">"
        web.mainFrame.loadHTMLString(html.replacingOccurrences(of: "<head>", with: "<head>" + csp), baseURL: nil)
        FileHandle.standardInput.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            if bytes.isEmpty { handle.readabilityHandler = nil; DispatchQueue.main.async { NSApp.terminate(nil) }; return }
            DispatchQueue.main.async { self?.receive(bytes) }
        }
    }

    func webView(_ sender: WebView!, didCreateJavaScriptContext js: JSContext!, for frame: WebFrame!) {
        guard frame === sender.mainFrame else { return }
        context = js
        let post: @convention(block) (String) -> Void = { [weak self] value in
            guard let data = value.data(using: .utf8), let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            self?.emit(body)
        }
        js.setObject(post, forKeyedSubscript: "karoPost" as NSString)
        js.exceptionHandler = { [weak self] _, _ in self?.emit(["ui_error": "page_script_failed"]) }
        js.evaluateScript("window.webkit={messageHandlers:{offline:{postMessage:function(x){karoPost(JSON.stringify(x));}}}};" + bridge)
    }
    func receive(_ bytes: Data) {
        buffer.append(bytes)
        if buffer.count > 16 * 1024 * 1024 { emit(["ui_error": "oversized_message"]); exit(2) }
        while let end = buffer.firstIndex(of: 10) {
            let line = String(decoding: buffer.prefix(upTo: end), as: UTF8.self); buffer.removeSubrange(...end)
            if ready { deliver(line) } else { pending.append(line) }
        }
    }
    func deliver(_ line: String) {
        guard let data = line.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) else { return }
        if smoke, (value as? [String: Any])?["smoke_send"] as? Bool == true {
            context.evaluateScript("document.getElementById('input').value='Do not use tools. Reply with a message saying Ready.'; document.getElementById('composer').requestSubmit();")
            return
        }
        context.objectForKeyedSubscript("karoOfflineReceive")?.call(withArguments: [value])
    }
    func webView(_ sender: WebView!, didFinishLoadFor frame: WebFrame!) {
        guard frame === sender.mainFrame else { return }
        ready = true
        for line in pending { deliver(line) }; pending.removeAll()
        emit(["ui_ready": true])
        if smoke {
            let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
            let visible = windows.contains { ($0[kCGWindowOwnerPID as String] as? Int) == Int(getpid()) && ($0[kCGWindowNumber as String] as? Int) == window.windowNumber }
            let rendered = context.evaluateScript("!!document.getElementById('composer') && !!document.getElementById('transcript')")?.toBool() == true
            let shortcuts = editingShortcutsSmokeTest()
            emit(["webview_smoke": rendered && visible && shortcuts, "window_onscreen": visible, "ui_network_denied": true, "editing_shortcuts": shortcuts])
        }
    }
    func webView(_ sender: WebView!, runJavaScriptConfirmPanelWithMessage message: String!, initiatedBy frame: WebFrame!) -> Bool {
        if smoke { return true }
        let alert = NSAlert(); alert.messageText = "Review local request"; alert.informativeText = message
        alert.addButton(withTitle: "Send locally"); alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }
    func webView(_ sender: WebView!, decidePolicyForNavigationAction actionInformation: [AnyHashable: Any]!, request: URLRequest!, frame: WebFrame!, decisionListener listener: WebPolicyDecisionListener!) {
        if request.url?.absoluteString == "about:blank" { listener.use() } else { listener.ignore() }
    }
    func webView(_ sender: WebView!, createWebViewWith request: URLRequest!) -> WebView! { nil }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = LocalWebView()
app.delegate = delegate
app.run()
