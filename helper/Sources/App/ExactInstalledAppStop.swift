import AppKit
import Foundation

enum ExactInstalledAppStopError: Error {
    case runningIdentityMismatch(pid: pid_t)
    case terminateRejected(pid: pid_t)
    case forceTerminateRejected(pid: pid_t)
    case applicationStillRunning
}

enum ExactInstalledAppStop {
    private static let bundleIdentifier = "com.yasyf.capt-hook.helper"

    static func run(appPath: String) throws {
        let expectedBundle = URL(fileURLWithPath: appPath, isDirectory: true)
            .resolvingSymlinksInPath().standardizedFileURL
        let currentPID = ProcessInfo.processInfo.processIdentifier
        var applications = try exactApplications(expectedBundle: expectedBundle, excluding: currentPID)
        for application in applications where !application.terminate() {
            throw ExactInstalledAppStopError.terminateRejected(pid: application.processIdentifier)
        }

        let terminateDeadline = Date().addingTimeInterval(5)
        while Date() < terminateDeadline {
            applications = try exactApplications(expectedBundle: expectedBundle, excluding: currentPID)
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
            if try exactApplications(expectedBundle: expectedBundle, excluding: currentPID).isEmpty {
                try proveQuietAbsence(expectedBundle: expectedBundle, currentPID: currentPID)
                return
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw ExactInstalledAppStopError.applicationStillRunning
    }

    private static func proveQuietAbsence(expectedBundle: URL, currentPID: pid_t) throws {
        Thread.sleep(forTimeInterval: 0.1)
        if try !exactApplications(expectedBundle: expectedBundle, excluding: currentPID).isEmpty {
            throw ExactInstalledAppStopError.applicationStillRunning
        }
    }

    private static func exactApplications(expectedBundle: URL, excluding currentPID: pid_t) throws -> [NSRunningApplication] {
        let applications = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { $0.processIdentifier != currentPID && !$0.isTerminated }
        for application in applications {
            guard application.bundleURL?.resolvingSymlinksInPath().standardizedFileURL == expectedBundle else {
                throw ExactInstalledAppStopError.runningIdentityMismatch(pid: application.processIdentifier)
            }
        }
        return applications
    }
}
