import AppKit
import DuskCore
import Foundation

enum ClipboardAttachmentError: LocalizedError {
    case unreadableImage
    case imageTooLarge

    var errorDescription: String? {
        switch self {
        case .unreadableImage:
            return "Не удалось прочитать картинку из буфера"
        case .imageTooLarge:
            return "Картинка больше 50 МБ"
        }
    }
}

enum ClipboardAttachmentStore {
    private static let maximumImageBytes = 50 * 1_024 * 1_024

    static func saveImage(
        from pasteboard: NSPasteboard,
        mode: CaptureMode,
        vaultURL: URL
    ) throws -> String? {
        let clipboardImage = NSImage(pasteboard: pasteboard)
        guard pasteboard.availableType(from: [.png, .tiff]) != nil
                || clipboardImage != nil else {
            return nil
        }

        let pngData: Data
        if let data = pasteboard.data(forType: .png) {
            pngData = data
        } else {
            guard
                let image = clipboardImage,
                let tiffData = image.tiffRepresentation,
                let bitmap = NSBitmapImageRep(data: tiffData),
                let data = bitmap.representation(using: .png, properties: [:])
            else {
                throw ClipboardAttachmentError.unreadableImage
            }
            pngData = data
        }

        guard pngData.count <= maximumImageBytes else {
            throw ClipboardAttachmentError.imageTooLarge
        }

        let relativeDirectory = AttachmentReference.relativeDirectory(for: mode)
        if mode == .privateEntry {
            let privateDirectory = vaultURL.appendingPathComponent("Private", isDirectory: true)
            try FileManager.default.createDirectory(
                at: privateDirectory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: privateDirectory.path
            )
        }
        let destinationDirectory = vaultURL.appendingPathComponent(
            relativeDirectory,
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: destinationDirectory,
            withIntermediateDirectories: true
        )

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let suffix = UUID().uuidString.prefix(8)
        let filename = "Dusk \(formatter.string(from: Date())) \(suffix).png"
        let destination = destinationDirectory.appendingPathComponent(filename)
        try pngData.write(to: destination, options: .atomic)

        return AttachmentReference.markdown(
            relativePath: "\(relativeDirectory)/\(filename)"
        )
    }
}
