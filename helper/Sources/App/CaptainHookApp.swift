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
    static let loginItemPlist = "com.yasyf.capt-hook.helper.plist"

    private let notifications = NotificationController()
    private let coalescer = ReloadCoalescer(interval: 30) { _ in
        WidgetCenter.shared.reloadAllTimelines()
    }
    private let refresher = SnapshotRefresher()
    private var broker: BrokerSocketBridge?
    private var brokerTask: Task<Void, Never>?
    private var notificationClient: ServiceSocketClient?
    private var notificationTask: Task<Void, Never>?
    private var watcher: SnapshotWatcher<Snapshot>?

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }

    private var runtimeBuild: String {
        guard let stamped = Bundle.main.object(forInfoDictionaryKey: "CaptHookBuild") as? String,
              !stamped.isEmpty
        else { return appVersion }
        return stamped.hasPrefix("v") ? String(stamped.dropFirst()) : stamped
    }

    func applicationWillFinishLaunching(_: Notification) {
        if Array(CommandLine.arguments.dropFirst()) == ["--stop-and-uninstall-service"] {
            do {
                try ExactAppServiceStop.run()
                exit(0)
            } catch {
                let message = "Captain Hook exact stop failed: \(error)\n"
                FileHandle.standardError.write(Data(message.utf8))
                exit(1)
            }
        }
        UNUserNotificationCenter.current().delegate = notifications
    }

    func applicationDidFinishLaunching(_: Notification) {
        notifications.requestAuthorizationIfNeeded()
        notifications.registerCategories()
        reconcileLoginItem()
        brokerTask = Task { await runBroker() }
        notificationTask = Task { await runNotificationConsumer() }
        startWatcher()
        refresher.start()
    }

    private func reconcileLoginItem() {
        do {
            let state = try LoginItem(plistName: Self.loginItemPlist).reconcile()
            Log.app.notice("login item: \(String(describing: state), privacy: .public)")
        } catch {
            Log.app.error("login item reconcile failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func runBroker() async {
        do {
            let lifecycle = RuntimeClientConfiguration(
                path: HelperPaths.hostSocket.path,
                wireBuild: helperWireBuild,
                role: helperBrokerLifecycleRole,
                noProgressTimeout: 30,
                socket: .init(maximumFrameBytes: 64 * 1024)
            )
            let bridge = try BrokerSocketBridge(
                container: HelperPaths.appGroup(),
                socket: HelperPaths.brokerSocket(),
                lifecycle: lifecycle,
                handoffRole: helperBrokerHandoffRole,
                expectedRuntimeBuild: runtimeBuild
            )
            broker = bridge
            try await bridge.run()
        } catch is CancellationError {
            return
        } catch {
            Log.socket.error("broker failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func runNotificationConsumer() async {
        while !Task.isCancelled {
            var client: ServiceSocketClient?
            do {
                let connected = try ServiceSocketClient(
                    path: HelperPaths.hostSocket.path,
                    wireBuild: helperWireBuild,
                    role: helperConsumerRole,
                    noProgressTimeout: 30,
                    configuration: .init(maximumFrameBytes: 64 * 1024)
                )
                client = connected
                notificationClient = connected
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

    private func consumeNotifications(from client: ServiceSocketClient) async throws {
        while !Task.isCancelled {
            let terminal = try await client.call(ServiceSocketCall(
                operation: helperNextOperation,
                replay: .provenNonDispatch,
                runtimeTarget: .anyAuthenticatedSuccessor,
                deadline: Date().addingTimeInterval(300)
            ))
            guard !terminal.rejected else {
                throw HelperConsumerError.rejected(terminal.reason ?? "helper next rejected")
            }
            if let error = terminal.error { throw HelperConsumerError.remote(error) }
            guard let data = terminal.payload else { throw HelperConsumerError.missingPayload }
            deliver(try NotifyPayload(JSONDecoder().decode(NotifyRequest.self, from: data)))
        }
    }

    private func deliver(_ payload: NotifyPayload) {
        notifications.post(payload)
        coalescer.record(trigger: payload.kind)
        refresher.debouncedRefresh()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard brokerTask != nil || notificationTask != nil else { return .terminateNow }
        let brokerTask = brokerTask
        let notificationTask = notificationTask
        let broker = broker
        let notificationClient = notificationClient
        self.brokerTask = nil
        self.notificationTask = nil
        self.broker = nil
        self.notificationClient = nil
        brokerTask?.cancel()
        notificationTask?.cancel()
        Task {
            await broker?.shutdown()
            await notificationClient?.close()
            await brokerTask?.value
            await notificationTask?.value
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
}
