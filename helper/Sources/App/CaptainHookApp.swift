import AppKit
import DaemonKit
import ServiceManagement
import SwiftUI
import UserNotifications
import WidgetKit

@main
struct CaptainHookApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene { Settings { EmptyView() } } // agent app: no windows
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    static let loginItemPlist = "com.yasyf.capt-hook.helper.plist"

    private let notifications = NotificationController()
    private let coalescer = ReloadCoalescer(interval: 30) { _ in
        WidgetCenter.shared.reloadAllTimelines()
    }
    private let refresher = SnapshotRefresher()
    private var server: SocketServer?
    private var watcher: SnapshotWatcher<Snapshot>?

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }

    func applicationWillFinishLaunching(_: Notification) {
        if CommandLine.arguments.contains("--unregister") {
            unregisterLoginItem()
            exit(0)
        }
        // Set before launch finishes so a cold-launch banner click still delivers.
        UNUserNotificationCenter.current().delegate = notifications
    }

    func applicationDidFinishLaunching(_: Notification) {
        notifications.requestAuthorizationIfNeeded()
        notifications.registerCategories()
        reconcileLoginItem()
        startServer()
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

    // DaemonKit's LoginItem has no unregister seam; go through SMAppService.
    private func unregisterLoginItem() {
        do {
            try SMAppService.agent(plistName: Self.loginItemPlist).unregister()
            Log.app.notice("login item unregistered")
        } catch {
            Log.app.error("login item unregister failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func startServer() {
        let handler = HelperHandler(version: appVersion) { [weak self] payload in
            guard let self else { return }
            self.notifications.post(payload)
            self.coalescer.record(trigger: payload.kind)
            self.refresher.debouncedRefresh()
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
            try server.start()
            self.server = server
            Log.socket.notice("serving on \(HelperPaths.socket.path, privacy: .public)")
        } catch {
            Log.socket.error("socket start failed: \(String(describing: error), privacy: .public)")
        }
    }

    func applicationWillTerminate(_: Notification) {
        server?.stop()
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
            case .malformed(let error): Log.app.error("snapshot malformed: \(error.description, privacy: .public)")
            case .versionSkew(let expected, let found):
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
