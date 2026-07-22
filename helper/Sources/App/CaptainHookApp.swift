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
    } // agent app: no windows
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    static let loginItemPlist = "com.yasyf.capt-hook.helper.plist"

    private let notifications = NotificationController()
    private let coalescer = ReloadCoalescer(interval: 30) { _ in
        WidgetCenter.shared.reloadAllTimelines()
    }

    private let refresher = SnapshotRefresher()
    private var server: SocketServer?
    private var serverStartTask: Task<Void, Never>?
    private var watcher: SnapshotWatcher<Snapshot>?

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
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
        // Set before launch finishes so a cold-launch banner click still delivers.
        UNUserNotificationCenter.current().delegate = notifications
    }

    func applicationDidFinishLaunching(_: Notification) {
        notifications.requestAuthorizationIfNeeded()
        notifications.registerCategories()
        reconcileLoginItem()
        serverStartTask = Task { await startServer() }
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

    private func startServer() async {
        let deliver: @MainActor @Sendable (NotifyPayload) -> Void = { [weak self] payload in
            guard let self else { return }
            self.notifications.post(payload)
            self.coalescer.record(trigger: payload.kind)
            self.refresher.debouncedRefresh()
        }
        let handler = HelperHandler(version: appVersion) { payload in
            await deliver(payload)
        }
        do {
            let requirement = try PeerTrust.Requirement(
                teamIdentifier: "SXKCTF23Q2",
                signingIdentifier: "com.yasyf.capt-hook.helper.bridge"
            )
            let server = SocketServer(
                path: HelperPaths.socket.path,
                build: appVersion,
                configuration: .init(
                    maximumFrameBytes: 64 * 1024,
                    maximumActiveRequests: 8,
                    maximumSessions: 8,
                    streamQueueDepth: 4,
                    handshakeTimeout: 5,
                    writeTimeout: 5
                ),
                trust: PeerTrust(requirement: requirement),
                handler: { await handler.handle($0) }
            )
            try FileManager.default.createDirectory(at: HelperPaths.directory, withIntermediateDirectories: true)
            try await server.start()
            self.server = server
            Log.socket.notice("serving on \(HelperPaths.socket.path, privacy: .public)")
        } catch {
            Log.socket.error("socket start failed: \(String(describing: error), privacy: .public)")
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let serverStartTask else { return .terminateNow }
        self.serverStartTask = nil
        Task {
            await serverStartTask.value
            if let server {
                self.server = nil
                await server.stop()
            }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func startWatcher() {
        let watcher = SnapshotWatcher<Snapshot>(
            fileURL: HelperPaths.status,
            expectedSchemaVersion: 1,
            callbackQueue: .main
        ) { state in
            switch state {
            case .loaded: Log.app.debug("snapshot loaded")
            case .missing: Log.app.debug("snapshot missing")
            case let .malformed(error): Log.app.error("snapshot malformed: \(error.description, privacy: .public)")
            case let .versionSkew(expected, found):
                Log.app.error("snapshot skew expected=\(expected) found=\(found)")
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
