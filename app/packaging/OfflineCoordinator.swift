import AppKit
import Foundation
import Darwin

@_silgen_name("sandbox_init")
func sandboxInit(_ profile: UnsafePointer<CChar>, _ flags: UInt64, _ error: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> Int32
@_silgen_name("sandbox_free_error")
func sandboxFreeError(_ error: UnsafeMutablePointer<CChar>)

// This launcher only chooses a path and relays private pipes. It neither
// renders chat nor opens dataset contents. Both children enforce OS isolation.
final class Coordinator: NSObject, NSApplicationDelegate {
    var worker: Process!, renderer: Process!
    var workerInput = Pipe(), workerOutput = Pipe(), uiInput = Pipe(), uiOutput = Pipe()
    var workerBuffer = Data(), uiBuffer = Data()
    var queued = [Data](), requests = [Data]()
    var uiVerified = false, workerVerified = false, idle = false, visible = false
    var sent = false, answered = false, stopping = false
    let smoke = CommandLine.arguments.contains("--smoke-test")

    func applicationDidFinishLaunching(_ notification: Notification) {
        var input: String?
        if !smoke {
            NSApp.activate(ignoringOtherApps: true)
            let welcome = NSAlert()
            welcome.messageText = "KaroSpace Offline"
            welcome.informativeText = "Choose a dataset or start a local chat."
            welcome.addButton(withTitle: "Start chat")
            welcome.addButton(withTitle: "Choose dataset…")
            welcome.addButton(withTitle: "Quit")
            let choice = welcome.runModal()
            if choice == .alertThirdButtonReturn { exit(0) }
            if choice == .alertSecondButtonReturn {
                let picker = NSOpenPanel()
                picker.canChooseDirectories = true; picker.allowsMultipleSelection = false
                picker.message = "Select an .h5ad, .rds, .RData file or a .zarr directory."
                guard picker.runModal() == .OK, let url = picker.url,
                      ["h5ad", "rds", "rdata", "zarr"].contains(url.pathExtension.lowercased()) else { exit(0) }
                input = url.path
            }
        }
        do {
            let resources = Bundle.main.resourceURL!
            let config = try JSONSerialization.jsonObject(with: Data(contentsOf: resources.appendingPathComponent("offline-config.json"))) as! [String: String]
            let python = config["python"]!, baseBin = config["base_bin"]!
            worker = Process(); renderer = Process()
            worker.executableURL = URL(fileURLWithPath: python)
            worker.arguments = ["-I", "-B", resources.appendingPathComponent("offline_launch.py").path] + (input.map { ["--input", $0] } ?? [])
            worker.environment = ["PATH": URL(fileURLWithPath: python).deletingLastPathComponent().path + ":" + baseBin + ":/usr/bin:/bin:/usr/sbin:/sbin",
                                  "HOME": FileManager.default.homeDirectoryForCurrentUser.path, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                                  "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"]
            worker.currentDirectoryURL = resources
            worker.standardInput = workerInput; worker.standardOutput = workerOutput
            worker.standardError = FileHandle.nullDevice
            renderer.executableURL = resources.appendingPathComponent("Offline UI.app/Contents/MacOS/OfflineUI")
            renderer.arguments = smoke ? ["--smoke-test"] : []
            renderer.environment = ["PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"]
            renderer.standardInput = uiInput; renderer.standardOutput = uiOutput
            renderer.standardError = smoke ? FileHandle.standardError : FileHandle.nullDevice
            workerOutput.fileHandleForReading.readabilityHandler = { [weak self] h in
                let data = h.availableData
                if data.isEmpty { h.readabilityHandler = nil; return }
                DispatchQueue.main.async { self?.receive(data, fromUI: false) }
            }
            uiOutput.fileHandleForReading.readabilityHandler = { [weak self] h in
                let data = h.availableData
                if data.isEmpty { h.readabilityHandler = nil; return }
                DispatchQueue.main.async { self?.receive(data, fromUI: true) }
            }
            worker.terminationHandler = { [weak self] _ in DispatchQueue.main.async { self?.stop(code: 2) } }
            renderer.terminationHandler = { [weak self] _ in DispatchQueue.main.async {
                guard let self = self else { return }
                self.stop(code: self.smoke ? 2 : 0)
            } }
            try worker.run(); try renderer.run()
            var error: UnsafeMutablePointer<CChar>?
            let policy = "(version 1)(allow default)(deny network*)(deny mach-lookup)(deny appleevent-send)(deny file-read-data)(deny file-write*)"
            let result = policy.withCString { sandboxInit($0, 0, &error) }
            if let error = error { sandboxFreeError(error) }
            guard result == 0 else { stop(code: 2); return }
            if smoke { DispatchQueue.main.asyncAfter(deadline: .now() + 120) { [weak self] in self?.stop(code: 2) } }
        } catch {
            if smoke { print("Offline launch failed before isolation"); fflush(stdout) }
            stop(code: 2)
        }
    }

    func receive(_ data: Data, fromUI: Bool) {
        if fromUI { uiBuffer.append(data) } else { workerBuffer.append(data) }
        var buffer = fromUI ? uiBuffer : workerBuffer
        guard buffer.count <= 16 * 1024 * 1024 else { stop(code: 2); return }
        while let end = buffer.firstIndex(of: 10) {
            var line = Data(buffer.prefix(upTo: end)); buffer.removeSubrange(...end)
            guard let value = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
            line.append(10)
            if fromUI {
                if value["ui_error"] != nil { if smoke { print("UI diagnostic:", value["ui_error"]!); fflush(stdout) }; stop(code: 2); return }
                if value["ui_ready"] as? Bool == true {
                    uiVerified = true
                    if smoke { print("Shared UI: page loaded"); fflush(stdout) }
                    for item in queued { uiInput.fileHandleForWriting.write(item) }; queued.removeAll()
                    for item in requests { workerInput.fileHandleForWriting.write(item) }; requests.removeAll()
                } else if value["webview_smoke"] != nil {
                    guard value["webview_smoke"] as? Bool == true else { stop(code: 2); return }
                    visible = true
                } else if value["id"] is Int, let path = value["path"] as? String,
                          ["/auth", "/opening", "/preview", "/send", "/interrupt", "/history", "/recovery/preview"].contains(path) {
                    if path == "/interrupt" { stop(code: 0); return }
                    if uiVerified { workerInput.fileHandleForWriting.write(line) }
                    else { requests.append(line) }
                }
            } else {
                if value["worker_verified"] as? Bool == true { workerVerified = true; if smoke { print("Shared UI: worker isolation verified"); fflush(stdout) }; continue }
                guard workerVerified else { stop(code: 2); return }
                if smoke {
                    if value["type"] as? String == "assistant" {
                        answered = (value["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters)).lowercased() == "ready"
                    }
                    if value["type"] as? String == "status" { idle = value["state"] as? String == "idle" }
                }
                if uiVerified { uiInput.fileHandleForWriting.write(line) } else { queued.append(line) }
            }
            if queued.count + requests.count > 1000 { stop(code: 2); return }
        }
        if fromUI { uiBuffer = buffer } else { workerBuffer = buffer }
        if smoke && visible && uiVerified && workerVerified && idle {
            if !sent {
                sent = true
                uiInput.fileHandleForWriting.write(Data("{\"smoke_send\":true}\n".utf8))
            } else if answered {
                print("{\"shared_ui_smoke_test\":\"passed\",\"window_onscreen\":true,\"ui_network_denied\":true,\"worker_isolation_verified\":true,\"local_reply\":true}")
                fflush(stdout); stop(code: 0)
            }
        }
    }

    func stop(code: Int32) {
        if stopping { return }; stopping = true
        if worker?.isRunning == true { worker.interrupt() }
        if renderer?.isRunning == true { renderer.terminate() }
        if let worker = worker { worker.waitUntilExit() }
        if smoke && code != 0 { print("Shared offline UI validation failed"); fflush(stdout) }
        exit(code)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { stop(code: 0); return .terminateNow }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = Coordinator()
app.delegate = delegate
app.run()
