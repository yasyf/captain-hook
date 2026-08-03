import DaemonKit
import Foundation

// RealHome, not NSHomeDirectory: correct inside the sandboxed appex, where
// NSHomeDirectory is the container. CAPT_HOOK_HELPER_DIR overrides for tests only.
enum HelperPaths {
    static var directory: URL {
        if let override = ProcessInfo.processInfo.environment["CAPT_HOOK_HELPER_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return RealHome.directory().appendingPathComponent(".capt-hook", isDirectory: true)
    }

    static var status: URL { directory.appendingPathComponent("status.json") }

    // daemonkit derives the serving socket from the label alone: ~/<label>/daemon.sock.
    static var hostSocket: URL {
        RealHome.directory()
            .appendingPathComponent(hostServiceLabel, isDirectory: true)
            .appendingPathComponent("daemon.sock")
    }
}
