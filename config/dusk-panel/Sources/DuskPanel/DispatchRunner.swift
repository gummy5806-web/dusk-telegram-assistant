import Foundation
import DuskCore
import Darwin

enum DispatchRunnerError: LocalizedError {
    case dispatcherMissing(String)
    case pythonMissing
    case launchFailed(String)
    case timedOut
    case failed(Int32, String)
    case emptyOutput

    var errorDescription: String? {
        switch self {
        case .dispatcherMissing(let path):
            return "Не найден dispatcher: \(path)"
        case .pythonMissing:
            return "Не найден Python 3"
        case .launchFailed(let message):
            return "Не удалось запустить: \(message)"
        case .timedOut:
            return "Dispatcher не ответил за 25 секунд"
        case .failed(let status, let message):
            return "Ошибка dispatcher (\(status)): \(message)"
        case .emptyOutput:
            return "Dispatcher вернул пустой ответ"
        }
    }
}

enum DispatchRunner {
    static func run(_ request: CaptureRequest) async throws -> String {
        try await Task.detached(priority: .userInitiated) {
            try runBlocking(request)
        }.value
    }

    private static func runBlocking(_ request: CaptureRequest) throws -> String {
        guard FileManager.default.isReadableFile(atPath: request.dispatcherURL.path) else {
            throw DispatchRunnerError.dispatcherMissing(request.dispatcherURL.path)
        }
        guard let pythonURL = pythonExecutable() else {
            throw DispatchRunnerError.pythonMissing
        }

        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("dusk-dispatch-\(UUID().uuidString)", isDirectory: true)
        let outputURL = temporaryDirectory.appendingPathComponent("stdout")
        let errorURL = temporaryDirectory.appendingPathComponent("stderr")

        do {
            try FileManager.default.createDirectory(
                at: temporaryDirectory,
                withIntermediateDirectories: false
            )
            guard
                FileManager.default.createFile(atPath: outputURL.path, contents: nil),
                FileManager.default.createFile(atPath: errorURL.path, contents: nil)
            else {
                throw CocoaError(.fileWriteUnknown)
            }
        } catch {
            throw DispatchRunnerError.launchFailed("Не удалось создать временные файлы: \(error.localizedDescription)")
        }
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }

        let standardOutput: FileHandle
        let standardError: FileHandle
        do {
            standardOutput = try FileHandle(forWritingTo: outputURL)
            standardError = try FileHandle(forWritingTo: errorURL)
        } catch {
            throw DispatchRunnerError.launchFailed("Не удалось открыть временные файлы: \(error.localizedDescription)")
        }
        defer {
            try? standardOutput.close()
            try? standardError.close()
        }

        let process = Process()
        process.executableURL = pythonURL
        process.arguments = request.arguments
        process.standardOutput = standardOutput
        process.standardError = standardError

        var environment = ProcessInfo.processInfo.environment
        environment["VAULT_PATH"] = request.dispatcherURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .path
        process.environment = environment

        do {
            try process.run()
        } catch {
            throw DispatchRunnerError.launchFailed(error.localizedDescription)
        }

        let deadline = DispatchTime.now().uptimeNanoseconds + 25_000_000_000
        while process.isRunning && DispatchTime.now().uptimeNanoseconds < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }

        guard !process.isRunning else {
            process.terminate()
            let terminationDeadline = DispatchTime.now().uptimeNanoseconds + 1_000_000_000
            while process.isRunning && DispatchTime.now().uptimeNanoseconds < terminationDeadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
            process.waitUntilExit()
            throw DispatchRunnerError.timedOut
        }

        try? standardOutput.synchronize()
        try? standardError.synchronize()
        let outputData = (try? Data(contentsOf: outputURL)) ?? Data()
        let errorData = (try? Data(contentsOf: errorURL)) ?? Data()
        let output = String(data: outputData, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let errorOutput = String(data: errorData, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        guard process.terminationStatus == 0 else {
            throw DispatchRunnerError.failed(process.terminationStatus, clipped(errorOutput))
        }
        guard !output.isEmpty else {
            throw DispatchRunnerError.emptyOutput
        }
        return clipped(output)
    }

    private static func pythonExecutable() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        let candidates = [
            environment["DUSK_PYTHON_PATH"],
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3",
        ].compactMap { $0 }

        return candidates.first(where: FileManager.default.isExecutableFile(atPath:))
            .map { URL(fileURLWithPath: $0) }
    }

    private static func clipped(_ text: String) -> String {
        String(text.prefix(1_200))
    }
}
