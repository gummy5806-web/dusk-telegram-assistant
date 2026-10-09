import Foundation

public enum CaptureMode: String, CaseIterable, Sendable {
    case smart
    case task
    case ask
    case privateEntry = "private"
    case link

    public init(route: String) {
        switch route.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")).lowercased() {
        case "private": self = .privateEntry
        case "ask": self = .ask
        case "task": self = .task
        case "link": self = .link
        default: self = .smart
        }
    }
}

public struct PanelRoute: Equatable, Sendable {
    public let mode: CaptureMode
    public let healthCheckToken: String?

    public init(url: URL) {
        let route = url.host(percentEncoded: false) ?? url.path
        mode = CaptureMode(route: route)

        let token = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first(where: { $0.name == "healthcheck" })?
            .value
        healthCheckToken = token.flatMap(Self.validatedHealthCheckToken)
    }

    private static func validatedHealthCheckToken(_ token: String) -> String? {
        guard (1...128).contains(token.count) else { return nil }
        let allowed = CharacterSet(
            charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        return token.unicodeScalars.allSatisfy(allowed.contains) ? token : nil
    }
}

public enum AttachmentReference {
    public static func relativeDirectory(for mode: CaptureMode) -> String {
        mode == .privateEntry ? "Private/Attachments" : "SYSTEM/MEDIA/Dusk"
    }

    public static func markdown(relativePath: String) -> String {
        "![[\(relativePath)]]"
    }
}

public enum CaptureRequestError: Error, Equatable, Sendable {
    case emptyInput
}

public struct CaptureRequest: Equatable, Sendable {
    public let mode: CaptureMode
    public let text: String
    public let dispatcherURL: URL
    public let arguments: [String]

    public init(mode: CaptureMode, text: String, vaultURL: URL, preview: Bool = false) throws {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            throw CaptureRequestError.emptyInput
        }

        let dispatcherURL = vaultURL
            .appendingPathComponent("bot", isDirectory: true)
            .appendingPathComponent("dispatch.py", isDirectory: false)

        self.mode = mode
        self.text = normalized
        self.dispatcherURL = dispatcherURL

        var arguments: [String]
        switch mode {
        case .privateEntry:
            arguments = [dispatcherURL.path, "--private", "--text", normalized]
        case .task:
            arguments = [dispatcherURL.path, "--text", "день: \(normalized)"]
        case .ask:
            arguments = [dispatcherURL.path, "--text", "?\(normalized)"]
        case .smart, .link:
            arguments = [dispatcherURL.path, "--text", normalized]
        }
        if preview { arguments.append("--preview") }
        self.arguments = arguments
    }
}
