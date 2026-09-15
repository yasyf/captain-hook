import AppKit
import Foundation

enum ExactInstalledAppStopError: Error {
    case runningIdentityMismatch(pid: pid_t, bundle: String, expected: String)
    case terminateRejected(pid: pid_t)
    case forceTerminateRejected(pid: pid_t)
    case applicationStillRunning(pids: [pid_t])
}

protocol StoppableApplication {
    var processIdentifier: pid_t { get }
    var isTerminated: Bool { get }
    var bundleURL: URL? { get }
    func terminate() -> Bool
    func forceTerminate() -> Bool
}

extension NSRunningApplication: StoppableApplication {}

enum ExactInstalledAppStop {
    private static let bundleIdentifier = "com.yasyf.capt-hook.helper"

    static func run(appPath: String) throws {
        let expectedBundle = URL(fileURLWithPath: appPath, isDirectory: true)
            .resolvingSymlinksInPath().standardizedFileURL
        let currentPID = ProcessInfo.processInfo.processIdentifier
        var applications = try runningGeneration(expectedBundle: expectedBundle, currentPID: currentPID)
        for application in applications where !application.terminate() {
            throw ExactInstalledAppStopError.terminateRejected(pid: application.processIdentifier)
        }

        let terminateDeadline = Date().addingTimeInterval(5)
        while Date() < terminateDeadline {
            applications = try runningGeneration(expectedBundle: expectedBundle, currentPID: currentPID)
            if applications.isEmpty {
                try proveQuietAbsence(expectedBundle: expectedBundle, currentPID: currentPID)
                return
            }
            Thread.sleep(forTimeInterval: 0.05)
        }

        for application in applications where !application.forceTerminate() {
            throw ExactInstalledAppStopError.forceTerminateRejected(pid: application.processIdentifier)
        }
        let forceDeadline = Date().addingTimeInterval(2)
        while Date() < forceDeadline {
            applications = try runningGeneration(expectedBundle: expectedBundle, currentPID: currentPID)
            if applications.isEmpty {
                try proveQuietAbsence(expectedBundle: expectedBundle, currentPID: currentPID)
                return
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw ExactInstalledAppStopError.applicationStillRunning(pids: applications.map(\.processIdentifier))
    }

    private static func proveQuietAbsence(expectedBundle: URL, currentPID: pid_t) throws {
        Thread.sleep(forTimeInterval: 0.1)
        let lingering = try runningGeneration(expectedBundle: expectedBundle, currentPID: currentPID)
        if !lingering.isEmpty {
            throw ExactInstalledAppStopError.applicationStillRunning(pids: lingering.map(\.processIdentifier))
        }
    }

    private static func runningGeneration(expectedBundle: URL, currentPID: pid_t) throws -> [StoppableApplication] {
        try running(
            among: NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier),
            expectedBundle: expectedBundle,
            currentPID: currentPID,
            isAlive: processExists
        )
    }

    // LaunchServices publishes an exited application for another 2-47ms, tearing
    // its bundleURL down before it flips isTerminated, so a generation that just
    // quit reads as one running from an unexpected path. Only a live process can
    // be a foreign generation, or hold the bundle against an inventory.
    static func running(
        among applications: [StoppableApplication],
        expectedBundle: URL,
        currentPID: pid_t,
        isAlive: (pid_t) -> Bool
    ) throws -> [StoppableApplication] {
        let candidates = applications.filter {
            $0.processIdentifier != currentPID && !$0.isTerminated && isAlive($0.processIdentifier)
        }
        for application in candidates {
            guard application.bundleURL?.resolvingSymlinksInPath().standardizedFileURL == expectedBundle else {
                throw ExactInstalledAppStopError.runningIdentityMismatch(
                    pid: application.processIdentifier,
                    bundle: application.bundleURL?.path ?? "<none>",
                    expected: expectedBundle.path
                )
            }
        }
        return candidates
    }

    static func processExists(_ pid: pid_t) -> Bool {
        if kill(pid, 0) == 0 { return true }
        return errno != ESRCH
    }
}
