import Foundation
import DuskCore

private var failures = 0

@MainActor
private func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if condition() {
        print("PASS: \(message)")
    } else {
        failures += 1
        fputs("FAIL: \(message)\n", stderr)
    }
}

check(CaptureMode(route: "capture") == .smart, "capture route selects Smart")
check(CaptureMode(route: "private") == .privateEntry, "private route selects Private")
check(CaptureMode(route: "ask") == .ask, "ask route selects Ask")
check(CaptureMode(route: "link") == .link, "link route selects Link")
check(CaptureMode(route: "unknown") == .smart, "unknown route falls back to Smart")

let healthRoute = PanelRoute(url: URL(string: "dusk://capture?healthcheck=abc-123_DEF")!)
check(healthRoute.mode == .smart, "panel route selects Smart")
check(healthRoute.healthCheckToken == "abc-123_DEF", "panel route accepts a safe health token")

let regularRoute = PanelRoute(url: URL(string: "dusk://private")!)
check(regularRoute.mode == .privateEntry, "panel route selects Private")
check(regularRoute.healthCheckToken == nil, "panel route permits a missing health token")

let unsafeHealthRoute = PanelRoute(url: URL(string: "dusk://capture?healthcheck=bad%2Ftoken")!)
check(unsafeHealthRoute.healthCheckToken == nil, "panel route rejects an unsafe health token")

check(
    AttachmentReference.relativeDirectory(for: .smart) == "SYSTEM/MEDIA/Dusk",
    "regular attachments use the synced media directory"
)
check(
    AttachmentReference.relativeDirectory(for: .privateEntry) == "Private/Attachments",
    "private attachments stay outside git"
)
check(
    AttachmentReference.markdown(relativePath: "SYSTEM/MEDIA/Dusk/example.png")
        == "![[SYSTEM/MEDIA/Dusk/example.png]]",
    "attachments use Obsidian embed syntax"
)

do {
    let vault = URL(fileURLWithPath: "/vault")
    let smart = try CaptureRequest(mode: .smart, text: "  записать идею  ", vaultURL: vault)
    let task = try CaptureRequest(
        mode: .task,
        text: "купить кабель, USB-C\nзаписаться к врачу",
        vaultURL: vault
    )
    let ask = try CaptureRequest(mode: .ask, text: "воронка", vaultURL: vault)
    let privateEntry = try CaptureRequest(mode: .privateEntry, text: "секрет", vaultURL: vault)
    let preview = try CaptureRequest(mode: .smart, text: "Сделать плагин", vaultURL: vault, preview: true)

    check(
        smart.arguments == ["/vault/bot/dispatch.py", "--text", "записать идею"],
        "Smart trims and passes input through"
    )
    check(
        task.arguments.last == "день: купить кабель, USB-C\nзаписаться к врачу",
        "Task preserves commas and line separators"
    )
    check(ask.arguments.last == "?воронка", "Ask uses the dispatcher prefix")
    check(
        preview.arguments == ["/vault/bot/dispatch.py", "--text", "Сделать плагин", "--preview"],
        "Preview is explicit and preserves the capture payload"
    )
    check(
        privateEntry.arguments == ["/vault/bot/dispatch.py", "--private", "--text", "секрет"],
        "Private uses the explicit dispatcher flag"
    )
} catch {
    failures += 1
    fputs("FAIL: valid capture request threw \(error)\n", stderr)
}

do {
    _ = try CaptureRequest(
        mode: .smart,
        text: " \n\t ",
        vaultURL: URL(fileURLWithPath: "/vault")
    )
    failures += 1
    fputs("FAIL: empty input was accepted\n", stderr)
} catch CaptureRequestError.emptyInput {
    print("PASS: empty input is rejected")
} catch {
    failures += 1
    fputs("FAIL: empty input returned unexpected error \(error)\n", stderr)
}

let screens = [
    ScreenGeometry(
        frame: CGRect(x: 0, y: 0, width: 1920, height: 1080),
        visibleFrame: CGRect(x: 0, y: 24, width: 1920, height: 1056)
    ),
    ScreenGeometry(
        frame: CGRect(x: 1920, y: 0, width: 2560, height: 1440),
        visibleFrame: CGRect(x: 1920, y: 24, width: 2560, height: 1416)
    ),
]

if let frame = PanelPlacement.frame(
    panelSize: CGSize(width: 680, height: 170),
    screens: screens,
    mouseLocation: CGPoint(x: 3000, y: 700)
) {
    check(frame.minX >= 1920 && frame.maxX <= 4480, "panel uses the mouse display")
    check(frame.minY >= 24 && frame.maxY <= 1440, "panel stays vertically visible")
} else {
    failures += 1
    fputs("FAIL: placement returned nil for two screens\n", stderr)
}

let negativeScreen = ScreenGeometry(
    frame: CGRect(x: -1200, y: -200, width: 1200, height: 900),
    visibleFrame: CGRect(x: -1200, y: -176, width: 1200, height: 876)
)
if let frame = PanelPlacement.frame(
    panelSize: CGSize(width: 680, height: 170),
    screens: [negativeScreen],
    mouseLocation: CGPoint(x: 9000, y: 9000)
) {
    check(
        frame.minX >= negativeScreen.visibleFrame.minX &&
            frame.maxX <= negativeScreen.visibleFrame.maxX &&
            frame.minY >= negativeScreen.visibleFrame.minY &&
            frame.maxY <= negativeScreen.visibleFrame.maxY,
        "fallback screen frame is clamped"
    )
} else {
    failures += 1
    fputs("FAIL: fallback placement returned nil\n", stderr)
}

check(
    PanelPlacement.frame(panelSize: CGSize(width: 680, height: 170), screens: [], mouseLocation: .zero) == nil,
    "placement requires a screen"
)

if failures > 0 {
    fputs("\(failures) test(s) failed\n", stderr)
    exit(1)
}

print("All DuskCore tests passed")
