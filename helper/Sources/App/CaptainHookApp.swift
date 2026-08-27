import AppKit
import DaemonKit
import SwiftUI
import UserNotifications
import WidgetKit

@main
struct CaptainHookApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        Settings { EmptyView() }
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    private let notifications = NotificationController()
    private let coalescer = ReloadCoalescer(interval: 30) { _ in
        WidgetCenter.shared.reloadAllTimelines()
    }
    private let refresher = SnapshotRefresher()
    private var notificationClient: ServiceSocketClient?
    private var notificationTask: Task<Void, Never>?
    private var watcher: SnapshotWatcher<Snapshot>?

    private var runtimeBuild: String { StampedBuild.current }

    func applicationWillFinishLaunching(_: Notification) {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments.count == 2, arguments[0] == "--deployment-stop-installed-generation" {
            do {
                try ExactInstalledAppStop.run(appPath: arguments[1])
                exit(0)
            } catch {
                let message = "Captain Hook exact deployment stop failed: \(error)\n"
                FileHandle.standardError.write(Data(message.utf8))
                exit(1)
            }
        }
        UNUserNotificationCenter.current().delegate = notifications
    }

    func applicationDidFinishLaunching(_: Notification) {
        notifications.requestAuthorizationIfNeeded()
        notifications.registerCategories()
        notificationTask = Task { await runNotificationConsumer() }
        startWatcher()
        refresher.start()
    }

    private func runNotificationConsumer() async {
        while !Task.isCancelled {
            var client: ServiceSocketClient?
            do {
                let connected = try ServiceSocketClient(
                    path: HelperPaths.hostSocket().path,
                    schema: helperSchema
                )
                client = connected
                notificationClient = connected
                try await requireExactRuntime(connected)
                try await consumeNotifications(from: connected)
            } catch is CancellationError {
                if let client { await client.close() }
                return
            } catch {
                Log.socket.error("notification consumer failed: \(String(describing: error), privacy: .public)")
                if let client { await client.close() }
                notificationClient = nil
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    // The installed runtime and this app ship as one signed generation, so a
    // build that is not this one is an upgrade the deployment has yet to
    // finish restarting: refuse the session and reconnect.
    private func requireExactRuntime(_ client: ServiceSocketClient) async throws {
        let terminal = try await client.call(ServiceSocketCall(
            operation: hostRuntimeHealthOperation,
            replay: .idempotent,
            deadline: Date().addingTimeInterval(30)
        ))
        let data = try payload(of: terminal, operation: hostRuntimeHealthOperation)
        let health = try JSONDecoder().decode(RuntimeHealth.self, from: data)
        guard health.runtimeBuild == runtimeBuild else {
            throw HelperConsumerError.runtimeSkew(expected: runtimeBuild, found: health.runtimeBuild)
        }
    }

    private func consumeNotifications(from client: ServiceSocketClient) async throws {
        while !Task.isCancelled {
            let terminal = try await client.call(ServiceSocketCall(
                operation: helperNextOperation,
                replay: .provenNonDispatch,
                deadline: Date().addingTimeInterval(300)
            ))
            let data = try payload(of: terminal, operation: helperNextOperation)
            deliver(try NotifyPayload(JSONDecoder().decode(NotifyRequest.self, from: data)))
        }
    }

    private func payload(of terminal: SocketTerminal, operation: String) throws -> Data {
        guard !terminal.rejected else {
            throw HelperConsumerError.rejected(terminal.reason ?? "\(operation) rejected")
        }
        if let error = terminal.error { throw HelperConsumerError.remote(error) }
        guard let payload = terminal.payload else { throw HelperConsumerError.missingPayload }
        return try businessReplyBody(payload, operation: operation)
    }

    private func deliver(_ payload: NotifyPayload) {
        notifications.post(payload)
        coalescer.record(trigger: payload.kind)
        refresher.debouncedRefresh()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let notificationTask else { return .terminateNow }
        let notificationClient = notificationClient
        self.notificationTask = nil
        self.notificationClient = nil
        notificationTask.cancel()
        Task {
            await notificationClient?.close()
            await notificationTask.value
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func startWatcher() {
        let codec: SnapshotCodec<Snapshot>
        do {
            codec = try SnapshotContract.codec()
        } catch {
            Log.app.error("snapshot codec invalid: \(String(describing: error), privacy: .public)")
            return
        }
        let watcher = SnapshotWatcher<Snapshot>(
            fileURL: HelperPaths.status,
            codec: codec,
            callbackQueue: .main
        ) { state in
            switch state {
            case .loaded: Log.app.debug("snapshot loaded")
            case .missing: Log.app.debug("snapshot missing")
            case let .malformed(error): Log.app.error("snapshot malformed: \(error.description, privacy: .public)")
            case let .schemaSkew(expected, foundIdentity, foundVersion, foundFingerprint):
                Log.app.error("snapshot skew expected=\(expected.identity, privacy: .public)/\(expected.version)/\(expected.fingerprint, privacy: .public) found=\(foundIdentity, privacy: .public)/\(foundVersion)/\(foundFingerprint, privacy: .public)")
            }
            WidgetCenter.shared.reloadAllTimelines()
        }
        do {
            try watcher.start()
            self.watcher = watcher
        } catch {
            Log.app.error("snapshot watch failed: \(String(describing: error), privacy: .public)")
        }
    }
}

private enum HelperConsumerError: Error {
    case rejected(String)
    case remote(String)
    case missingPayload
    case runtimeSkew(expected: String, found: String)
}
