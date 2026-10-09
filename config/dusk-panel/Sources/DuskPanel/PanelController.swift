import AppKit
import DuskCore

private final class CapturePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class PanelController: NSObject, NSWindowDelegate, NSTextFieldDelegate {
    private let panelSize = NSSize(width: 760, height: 252)
    private let panel: CapturePanel
    private let modeControl: NSSegmentedControl
    private let inputField: NSTextField
    private let statusLabel: NSTextField
    private let accentView: NSView
    private var currentMode: CaptureMode = .smart
    private var busy = false
    private var eventMonitor: Any?
    private var hideGeneration = UUID()
    private var previewGeneration = UUID()
    private var previewTask: Task<Void, Never>?

    private let modeOrder: [CaptureMode] = [.smart, .task, .ask, .privateEntry, .link]
    private let modeTitles = ["Умно", "Задача", "Спросить", "Личное", "Ссылка"]

    override init() {
        panel = CapturePanel(
            contentRect: NSRect(origin: .zero, size: panelSize),
            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        modeControl = NSSegmentedControl(
            labels: modeTitles,
            trackingMode: .selectOne,
            target: nil,
            action: nil
        )
        inputField = NSTextField(string: "")
        statusLabel = NSTextField(labelWithString: "")
        accentView = NSView(frame: .zero)
        super.init()

        configurePanel()
        configureContent()
        installKeyboardMonitor()
        select(mode: .smart)
    }

    func tearDown() {
        cancelPreview()
        if let eventMonitor {
            NSEvent.removeMonitor(eventMonitor)
            self.eventMonitor = nil
        }
    }

    func show(mode: CaptureMode) {
        hideGeneration = UUID()
        if !busy {
            select(mode: mode)
            statusLabel.stringValue = ""
            inputField.stringValue = ""
            schedulePreview()
        }
        positionOnMouseScreen()
        activateAndFocus()
        DispatchQueue.main.async { [weak self] in
            self?.activateAndFocus()
        }
    }

    func verifyPresentation(completion: @escaping (Bool) -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self else {
                completion(false)
                return
            }
            completion(
                self.panel.isVisible
                    && self.panel.isKeyWindow
                    && self.inputField.currentEditor() != nil
            )
        }
    }

    func hide() {
        hideGeneration = UUID()
        cancelPreview()
        panel.orderOut(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }

    private func configurePanel() {
        panel.title = "Dusk"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .floating
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .transient,
            .ignoresCycle,
        ]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.isMovableByWindowBackground = true
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none
        panel.delegate = self
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
    }

    private func configureContent() {
        let effectView = NSVisualEffectView()
        effectView.material = .hudWindow
        effectView.blendingMode = .behindWindow
        effectView.state = .active
        effectView.wantsLayer = true
        effectView.layer?.cornerRadius = 10
        effectView.layer?.masksToBounds = true
        panel.contentView = effectView

        let titleLabel = NSTextField(labelWithString: "DUSK")
        titleLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        titleLabel.textColor = NSColor(white: 0.72, alpha: 1)

        modeControl.segmentStyle = .capsule
        modeControl.target = self
        modeControl.action = #selector(modeChanged)
        modeControl.controlSize = .large
        modeControl.font = .systemFont(ofSize: 14, weight: .medium)
        modeControl.selectedSegment = 0

        accentView.wantsLayer = true
        accentView.layer?.cornerRadius = 2

        inputField.font = .systemFont(ofSize: 18, weight: .regular)
        inputField.textColor = NSColor(white: 0.96, alpha: 1)
        inputField.backgroundColor = NSColor(white: 0.05, alpha: 0.42)
        inputField.isBezeled = false
        inputField.focusRingType = .none
        inputField.wantsLayer = true
        inputField.layer?.cornerRadius = 7
        inputField.layer?.borderWidth = 1
        inputField.layer?.borderColor = NSColor(white: 1, alpha: 0.10).cgColor
        inputField.cell?.usesSingleLineMode = false
        inputField.cell?.wraps = true
        inputField.cell?.isScrollable = true
        inputField.maximumNumberOfLines = 4
        inputField.lineBreakMode = .byWordWrapping
        inputField.delegate = self

        statusLabel.font = .systemFont(ofSize: 12, weight: .medium)
        statusLabel.textColor = NSColor(srgbRed: 0.45, green: 0.86, blue: 0.66, alpha: 1)
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.maximumNumberOfLines = 2

        [titleLabel, modeControl, accentView, inputField, statusLabel].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            effectView.addSubview($0)
        }

        NSLayoutConstraint.activate([
            accentView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: 18),
            accentView.topAnchor.constraint(equalTo: effectView.topAnchor, constant: 27),
            accentView.widthAnchor.constraint(equalToConstant: 4),
            accentView.heightAnchor.constraint(equalToConstant: 18),

            titleLabel.leadingAnchor.constraint(equalTo: accentView.trailingAnchor, constant: 8),
            titleLabel.centerYAnchor.constraint(equalTo: accentView.centerYAnchor),

            modeControl.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -18),
            modeControl.centerYAnchor.constraint(equalTo: accentView.centerYAnchor),
            modeControl.widthAnchor.constraint(equalToConstant: 580),
            modeControl.heightAnchor.constraint(equalToConstant: 42),

            inputField.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: 18),
            inputField.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -18),
            inputField.topAnchor.constraint(equalTo: modeControl.bottomAnchor, constant: 12),
            inputField.heightAnchor.constraint(equalToConstant: 96),

            statusLabel.leadingAnchor.constraint(equalTo: inputField.leadingAnchor, constant: 4),
            statusLabel.trailingAnchor.constraint(equalTo: inputField.trailingAnchor, constant: -4),
            statusLabel.topAnchor.constraint(equalTo: inputField.bottomAnchor, constant: 12),
        ])
    }

    private func installKeyboardMonitor() {
        eventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.panel.isKeyWindow else { return event }

            if event.keyCode == 9, event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command {
                self.pasteFromClipboard()
                return nil
            }

            switch event.keyCode {
            case 53:
                self.hide()
                return nil
            case 48:
                self.cycleMode(backwards: event.modifierFlags.contains(.shift))
                return nil
            case 36, 76:
                if (self.currentMode == .task || self.currentMode == .smart)
                    && event.modifierFlags.contains(.shift) {
                    self.insertTaskLineBreak()
                    return nil
                }
                self.submit(keepOpen: event.modifierFlags.contains(.command))
                return nil
            default:
                return event
            }
        }
    }

    private func pasteFromClipboard() {
        guard !busy else {
            NSSound.beep()
            return
        }

        let vaultURL = URL(
            fileURLWithPath: ProcessInfo.processInfo.environment["DUSK_VAULT_PATH"]
                ?? FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("Documents/obsidian/my dusk", isDirectory: true)
                    .path,
            isDirectory: true
        )

        do {
            if let reference = try ClipboardAttachmentStore.saveImage(
                from: .general,
                mode: currentMode,
                vaultURL: vaultURL
            ) {
                insertAtCursor(reference)
                statusLabel.textColor = NSColor(srgbRed: 0.45, green: 0.86, blue: 0.66, alpha: 1)
                statusLabel.stringValue = "Картинка прикреплена"
                return
            }
        } catch {
            showError(error.localizedDescription)
            NSSound.beep()
            return
        }

        guard let editor = inputField.currentEditor() as? NSTextView else {
            NSSound.beep()
            return
        }
        editor.paste(nil)
    }

    private func insertAtCursor(_ text: String) {
        guard let editor = inputField.currentEditor() as? NSTextView else {
            inputField.stringValue.append(text)
            return
        }

        let range = editor.selectedRange()
        let existing = editor.string as NSString
        let previousCharacter = range.location > 0
            ? existing.substring(with: NSRange(location: range.location - 1, length: 1))
            : ""
        let needsLeadingSpace = range.location > 0
            && previousCharacter.rangeOfCharacter(from: .whitespacesAndNewlines) == nil
        let insertion = needsLeadingSpace ? " \(text)" : text
        editor.insertText(insertion, replacementRange: range)
    }

    private func insertTaskLineBreak() {
        guard let editor = inputField.currentEditor() as? NSTextView else {
            inputField.stringValue.append("\n")
            return
        }
        editor.insertText("\n", replacementRange: editor.selectedRange())
    }

    @objc private func modeChanged() {
        guard modeOrder.indices.contains(modeControl.selectedSegment) else { return }
        select(mode: modeOrder[modeControl.selectedSegment])
        panel.makeFirstResponder(inputField)
    }

    private func select(mode: CaptureMode) {
        currentMode = mode
        modeControl.selectedSegment = modeOrder.firstIndex(of: mode) ?? 0
        inputField.placeholderString = placeholder(for: mode)
        accentView.layer?.backgroundColor = accentColor(for: mode).cgColor
        schedulePreview()
    }

    private func cycleMode(backwards: Bool) {
        let currentIndex = modeOrder.firstIndex(of: currentMode) ?? 0
        let offset = backwards ? -1 : 1
        let nextIndex = (currentIndex + offset + modeOrder.count) % modeOrder.count
        select(mode: modeOrder[nextIndex])
        panel.makeFirstResponder(inputField)
    }

    private func submit(keepOpen: Bool) {
        guard !busy else { return }

        let vaultPath = ProcessInfo.processInfo.environment["DUSK_VAULT_PATH"]
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Documents/obsidian/my dusk", isDirectory: true)
                .path

        let request: CaptureRequest
        do {
            request = try CaptureRequest(
                mode: currentMode,
                text: inputField.stringValue,
                vaultURL: URL(fileURLWithPath: vaultPath, isDirectory: true)
            )
        } catch CaptureRequestError.emptyInput {
            NSSound.beep()
            return
        } catch {
            showError(error.localizedDescription)
            return
        }

        cancelPreview()
        setBusy(true)
        statusLabel.stringValue = "Обрабатываю..."
        let generation = UUID()
        hideGeneration = generation

        Task { [weak self] in
            guard let self else { return }
            do {
                let result = try await DispatchRunner.run(request)
                self.setBusy(false)
                self.statusLabel.textColor = NSColor(srgbRed: 0.45, green: 0.86, blue: 0.66, alpha: 1)
                self.statusLabel.stringValue = result.replacingOccurrences(of: "\n", with: "  ")
                self.inputField.stringValue = ""

                if keepOpen {
                    self.panel.makeFirstResponder(self.inputField)
                } else {
                    try? await Task.sleep(for: .seconds(2))
                    guard self.hideGeneration == generation else { return }
                    self.hide()
                }
            } catch {
                self.setBusy(false)
                self.showError(error.localizedDescription)
                self.panel.makeFirstResponder(self.inputField)
            }
        }
    }

    func controlTextDidChange(_ notification: Notification) {
        hideGeneration = UUID()
        schedulePreview()
    }

    private func cancelPreview() {
        previewTask?.cancel()
        previewTask = nil
        previewGeneration = UUID()
    }

    private func schedulePreview() {
        cancelPreview()
        guard !busy else { return }
        statusLabel.stringValue = ""
        let text = inputField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let vaultPath = ProcessInfo.processInfo.environment["DUSK_VAULT_PATH"]
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Documents/obsidian/my dusk", isDirectory: true).path
        let generation = previewGeneration
        let mode = currentMode
        previewTask = Task { [weak self] in
            do {
                try await Task.sleep(for: .milliseconds(300))
                guard let self, !Task.isCancelled else { return }
                let request = try CaptureRequest(
                    mode: mode, text: text,
                    vaultURL: URL(fileURLWithPath: vaultPath, isDirectory: true),
                    preview: true
                )
                let result = try await DispatchRunner.run(request)
                // A previous subprocess can finish after the text has changed.
                guard self.previewGeneration == generation, !Task.isCancelled else { return }
                self.statusLabel.textColor = .secondaryLabelColor
                self.statusLabel.stringValue = result
            } catch is CancellationError {
                return
            } catch {
                guard let self, self.previewGeneration == generation else { return }
                self.showError(error.localizedDescription)
            }
        }
    }

    private func setBusy(_ isBusy: Bool) {
        busy = isBusy
        inputField.isEnabled = !isBusy
        modeControl.isEnabled = !isBusy
    }

    private func showError(_ message: String) {
        statusLabel.textColor = NSColor(srgbRed: 1, green: 0.43, blue: 0.42, alpha: 1)
        statusLabel.stringValue = message
    }

    private func activateAndFocus() {
        panel.orderFrontRegardless()
        panel.makeKey()
        panel.makeFirstResponder(inputField)
    }

    private func positionOnMouseScreen() {
        let screens = NSScreen.screens.map {
            ScreenGeometry(frame: $0.frame, visibleFrame: $0.visibleFrame)
        }
        guard let frame = PanelPlacement.frame(
            panelSize: panelSize,
            screens: screens,
            mouseLocation: NSEvent.mouseLocation
        ) else { return }
        panel.setFrame(frame, display: true)
    }

    private func placeholder(for mode: CaptureMode) -> String {
        switch mode {
        case .smart: return "Записать мысль, задачу или ссылку"
        case .task: return "Одна задача на строку · Shift+Enter — новая строка"
        case .ask: return "Что найти в Dusk и research-brain"
        case .privateEntry: return "Личная запись вне git"
        case .link: return "Ссылка на видео"
        }
    }

    private func accentColor(for mode: CaptureMode) -> NSColor {
        switch mode {
        case .smart: return NSColor(srgbRed: 0.25, green: 0.78, blue: 0.72, alpha: 1)
        case .task: return NSColor(srgbRed: 0.95, green: 0.68, blue: 0.28, alpha: 1)
        case .ask: return NSColor(srgbRed: 0.35, green: 0.67, blue: 0.96, alpha: 1)
        case .privateEntry: return NSColor(srgbRed: 0.96, green: 0.40, blue: 0.52, alpha: 1)
        case .link: return NSColor(srgbRed: 0.51, green: 0.82, blue: 0.40, alpha: 1)
        }
    }
}
