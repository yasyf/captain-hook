import DaemonKit
import Foundation

// RealHome, not NSHomeDirectory: correct inside the sandboxed appex, where
// NSHomeDirectory is the container. CAPT_HOOK_HELPER_DIR overrides for tests only.
enum HelperPaths {
    static let appGroupIdentifier = "SXKCTF23Q2.com.yasyf.capt-hook.helper"

    static var directory: URL {
        if let override = ProcessInfo.processInfo.environment["CAPT_HOOK_HELPER_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return RealHome.directory().appendingPathComponent(".capt-hook", isDirectory: true)
    }

    static var status: URL { directory.appendingPathComponent("status.json") }
    static var hostSocket: URL {
        RealHome.directory()
            .appendingPathComponent("Library/Caches/captain-hook/host-v1", isDirectory: true)
            .appendingPathComponent("capt-hookd.sock")
    }

    static func appGroup() throws -> AppGroupContainer {
        try AppGroupContainer(identifier: appGroupIdentifier)
    }

    static func brokerSocket() throws -> AppGroupContainer.SocketLeaf {
        try AppGroupContainer.SocketLeaf("helper.sock")
    }
}
