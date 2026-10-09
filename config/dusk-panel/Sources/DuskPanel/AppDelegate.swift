import AppKit
import Carbon.HIToolbox
import DuskCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panelController: PanelController?
    private var pendingMode: CaptureMode?
    private var pendingHealthCheckToken: String?

    override init() {
        super.init()
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        panelController = PanelController()
        if let pendingMode {
            let healthCheckToken = pendingHealthCheckToken
            self.pendingMode = nil
            pendingHealthCheckToken = nil
            show(mode: pendingMode, healthCheckToken: healthCheckToken)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        panelController?.tearDown()
        NSAppleEventManager.shared().removeEventHandler(
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.last else { return }
        show(url: url)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    @objc private func handleGetURLEvent(
        _ event: NSAppleEventDescriptor,
        withReplyEvent replyEvent: NSAppleEventDescriptor
    ) {
        guard
            let rawURL = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
            let url = URL(string: rawURL)
        else {
            show(mode: .smart)
            return
        }
        show(url: url)
    }

    private func show(url: URL) {
        let route = PanelRoute(url: url)
        show(mode: route.mode, healthCheckToken: route.healthCheckToken)
    }

    private func show(mode: CaptureMode, healthCheckToken: String? = nil) {
        guard let panelController else {
            pendingMode = mode
            pendingHealthCheckToken = healthCheckToken
            return
        }
        panelController.show(mode: mode)
        guard let healthCheckToken else { return }

        panelController.verifyPresentation { [weak self, weak panelController] ready in
            if ready {
                self?.writeHealthCheckMarker(healthCheckToken)
            }
            panelController?.hide()
        }
    }

    private func writeHealthCheckMarker(_ token: String) {
        do {
            let cacheRoot = try FileManager.default.url(
                for: .cachesDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let directory = cacheRoot.appendingPathComponent("com.asan.dusk-panel", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: nil
            )
            try token.write(
                to: directory.appendingPathComponent("healthcheck", isDirectory: false),
                atomically: true,
                encoding: .utf8
            )
        } catch {
            NSLog("Dusk healthcheck marker failed: %@", error.localizedDescription)
        }
    }
}
