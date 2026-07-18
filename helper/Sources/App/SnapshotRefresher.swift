import DaemonKit
import Foundation

// Refreshes status.json by execing capt-hook: every 10 min plus a 30s post-notify
// debounce, a 120s watchdog on a wedged child, and never a crash.
final class SnapshotRefresher: @unchecked Sendable {
    private static let interval: TimeInterval = 600
    private static let debounceDelay: TimeInterval = 30
    private static let watchdog: TimeInterval = 120
    private static let killGrace: TimeInterval = 10

    private let queue = DispatchQueue(label: "com.yasyf.capt-hook.helper.refresher")
    private var timer: DispatchSourceTimer?
    private var debounce: DispatchWorkItem?

    func start() {
        queue.async { [weak self] in
            guard let self else { return }
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now() + 5, repeating: Self.interval)
            timer.setEventHandler { [weak self] in self?.runRefresh() }
            self.timer = timer
            timer.resume()
        }
    }

    func debouncedRefresh() {
        queue.async { [weak self] in
            guard let self else { return }
            self.debounce?.cancel()
            let item = DispatchWorkItem { [weak self] in self?.runRefresh() }
            self.debounce = item
            self.queue.asyncAfter(deadline: .now() + Self.debounceDelay, execute: item)
        }
    }

    private func runRefresh() {
        guard let argv = Self.resolveCommand() else {
            Log.refresh.notice("no capt-hook binary resolved; skipping refresh")
            return
        }
        Self.run(argv)
    }

    // Direct binary first (no uvx stale-wheel/network window), then uvx;
    // absolute prefixes because launchd's PATH is bare. RefreshCommand overrides.
    static func resolveCommand() -> [String]? {
        if let override = UserDefaults.standard.string(forKey: "RefreshCommand"), !override.isEmpty {
            let argv = override.split(separator: " ").map(String.init)
            return argv.isEmpty ? nil : argv
        }
        let prefixes = [
            RealHome.directory().appendingPathComponent(".local/bin").path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        let tail = ["review", "snapshot", "--refresh"]
        for prefix in prefixes {
            let direct = "\(prefix)/capt-hook"
            if FileManager.default.isExecutableFile(atPath: direct) { return [direct] + tail }
        }
        for prefix in prefixes {
            let uvx = "\(prefix)/uvx"
            if FileManager.default.isExecutableFile(atPath: uvx) { return [uvx, "capt-hook"] + tail }
        }
        return nil
    }

    private static func run(_ argv: [String]) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: argv[0])
        process.arguments = Array(argv.dropFirst())
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            Log.refresh.error("spawn failed: \(String(describing: error), privacy: .public)")
            return
        }
        let watchdog = DispatchWorkItem {
            guard process.isRunning else { return }
            Log.refresh.error("refresh exceeded \(Int(Self.watchdog))s; terminating")
            process.terminate()
            let pid = process.processIdentifier
            DispatchQueue.global().asyncAfter(deadline: .now() + Self.killGrace) {
                guard process.isRunning else { return }
                Log.refresh.error("refresh ignored SIGTERM; killing pid \(pid)")
                kill(pid, SIGKILL)
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + Self.watchdog, execute: watchdog)
        process.waitUntilExit()
        watchdog.cancel()
        Log.refresh.notice("refresh exited status=\(process.terminationStatus)")
    }
}
