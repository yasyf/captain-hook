import Foundation
import Testing
@testable import DaemonKit

private final class SessionBundleToken {}
private let buildVersion = Bundle(for: SessionBundleToken.self)
    .object(forInfoDictionaryKey: "CFBundleShortVersionString") as! String
private let signedBridgeExecutable = ProcessInfo.processInfo.environment["CAPT_HOOK_SIGNED_BRIDGE_PATH"]
    .map { URL(fileURLWithPath: $0) }

private final class NotificationRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [NotifyPayload] = []

    func append(_ value: NotifyPayload) {
        lock.lock()
        values.append(value)
        lock.unlock()
    }

    var payloads: [NotifyPayload] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }
}

private struct BridgeResult {
    let status: Int32
    let output: Data
    let error: Data
}

private func temporarySocket() throws -> (URL, URL) {
    let directory = URL(fileURLWithPath: "/tmp/capt-hook-\(UUID().uuidString.prefix(8))", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return (directory, directory.appendingPathComponent("helper.sock"))
}

private func unsignedBridgeExecutable() -> URL {
    Bundle(for: SessionBundleToken.self).bundleURL
        .deletingLastPathComponent()
        .appendingPathComponent("capt-hook-helper-client")
}

private func runBridge(
    executable: URL = unsignedBridgeExecutable(),
    socket: URL,
    operation: String,
    input: Data = Data()
) async throws -> BridgeResult {
    let process = Process()
    process.executableURL = executable
    process.arguments = ["--socket", socket.path, operation]
    let standardInput = Pipe()
    let standardOutput = Pipe()
    let standardError = Pipe()
    process.standardInput = standardInput
    process.standardOutput = standardOutput
    process.standardError = standardError
    return try await withCheckedThrowingContinuation { continuation in
        process.terminationHandler = { process in
            continuation.resume(returning: BridgeResult(
                status: process.terminationStatus,
                output: standardOutput.fileHandleForReading.readDataToEndOfFile(),
                error: standardError.fileHandleForReading.readDataToEndOfFile()
            ))
        }
        do {
            try process.run()
            standardInput.fileHandleForWriting.write(input)
            try standardInput.fileHandleForWriting.close()
        } catch {
            process.terminationHandler = nil
            continuation.resume(throwing: error)
        }
    }
}

private func withStartedServer<Result>(
    _ server: SocketServer,
    body: () async throws -> Result
) async throws -> Result {
    try await server.start()
    do {
        let result = try await body()
        await server.stop()
        return result
    } catch {
        await server.stop()
        throw error
    }
}

@Suite(.serialized) struct HelperSessionTests {
    @Test func bridgePingAndNotifyUsePersistentDaemonKitSessions() async throws {
        let (directory, socket) = try temporarySocket()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = NotificationRecorder()
        let handler = HelperHandler(version: buildVersion) { payload in recorder.append(payload) }
        let server = SocketServer(
            path: socket.path,
            wireBuild: helperWireBuild,
            trust: .sameEffectiveUser,
            handler: { await handler.handle($0) }
        )
        try await withStartedServer(server) {
            let ping = try await runBridge(socket: socket, operation: "ping")
            #expect(ping.status == 0)
            #expect(try JSONDecoder().decode(HelperReply.self, from: ping.output) == .ping(version: buildVersion))

            let request = NotifyRequest(
                kind: "pr_open",
                title: "Block force-pushes",
                subtitle: "captain-hook",
                body: "Rule guard-rm-rf opened",
                url: "https://github.com/yasyf/captain-hook/pull/12",
                repo: "github.com/yasyf/captain-hook"
            )
            let notify = try await runBridge(
                socket: socket,
                operation: "notify",
                input: try JSONEncoder().encode(request)
            )
            #expect(notify.status == 0)
            #expect(try JSONDecoder().decode(HelperReply.self, from: notify.output) == .ok)
            #expect(recorder.payloads.map(\.title) == ["Block force-pushes"])
        }
    }

    @Test(.enabled(
        if: signedBridgeExecutable != nil,
        "set CAPT_HOOK_SIGNED_BRIDGE_PATH to a Developer ID signed Release bridge"
    ))
    func developerIDSignedBridgePassesProductionTrust() async throws {
        let executable = try #require(signedBridgeExecutable)
        let (directory, socket) = try temporarySocket()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = NotificationRecorder()
        let handler = HelperHandler(version: buildVersion) { payload in recorder.append(payload) }
        let requirement = try PeerTrust.Requirement(
            teamIdentifier: "SXKCTF23Q2",
            signingIdentifier: "com.yasyf.capt-hook.helper.bridge"
        )
        let server = SocketServer(
            path: socket.path,
            wireBuild: helperWireBuild,
            trust: PeerTrust(requirement: requirement),
            handler: { await handler.handle($0) }
        )
        try await withStartedServer(server) {
            let ping = try await runBridge(executable: executable, socket: socket, operation: "ping")
            #expect(ping.status == 0)
            #expect(try JSONDecoder().decode(HelperReply.self, from: ping.output) == .ping(version: buildVersion))

            let request = NotifyRequest(
                kind: "pr_open",
                title: "Signed bridge",
                subtitle: nil,
                body: nil,
                url: nil,
                repo: nil
            )
            let notify = try await runBridge(
                executable: executable,
                socket: socket,
                operation: "notify",
                input: try JSONEncoder().encode(request)
            )
            #expect(notify.status == 0)
            #expect(try JSONDecoder().decode(HelperReply.self, from: notify.output) == .ok)
            #expect(recorder.payloads.map(\.title) == ["Signed bridge"])
        }
    }

    @Test(arguments: [
        "com.yasyf.capt-hook.helper.bridge",
        "com.yasyf.capt-hook.helper.wrong",
    ])
    func productionTrustRejectsUntrustedBridgeBeforeDispatch(signingIdentifier: String) async throws {
        let (directory, socket) = try temporarySocket()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = NotificationRecorder()
        let handler = HelperHandler(version: "0.0.0") { payload in recorder.append(payload) }
        let requirement = try PeerTrust.Requirement(
            teamIdentifier: "SXKCTF23Q2",
            signingIdentifier: signingIdentifier
        )
        let server = SocketServer(
            path: socket.path,
            wireBuild: helperWireBuild,
            trust: PeerTrust(requirement: requirement),
            handler: { await handler.handle($0) }
        )
        try await withStartedServer(server) {
            let result = try await runBridge(
                executable: unsignedBridgeExecutable(),
                socket: socket,
                operation: "ping"
            )
            #expect(result.status != 0)
            #expect(result.output.isEmpty)
            #expect(recorder.payloads.isEmpty)
        }
    }
}
