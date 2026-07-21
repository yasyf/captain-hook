import AppKit
import Foundation
import ServiceManagement

enum ExactAppServiceStopError: Error {
    case serviceDidNotUnregister
    case runningIdentityMismatch(pid: pid_t)
    case terminateRejected(pid: pid_t)
    case forceTerminateRejected(pid: pid_t)
    case applicationStillRunning
}

enum ExactAppServiceStop {
    private static let bundleIdentifier = "com.yasyf.capt-hook.helper"
    private static let loginItemPlist = "com.yasyf.capt-hook.helper.plist"

    static func run() throws {
        let service = SMAppService.agent(plistName: loginItemPlist)
        switch service.status {
        case .enabled, .requiresApproval:
            try service.unregister()
        case .notFound, .notRegistered:
            break
        @unknown default:
            throw ExactAppServiceStopError.serviceDidNotUnregister
        }
        try waitForServiceAbsence(service)
        try stopExactInstalledApplications()
    }

    private static func waitForServiceAbsence(_ service: SMAppService) throws {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            switch service.status {
            case .notFound, .notRegistered:
                return
            case .enabled, .requiresApproval:
                Thread.sleep(forTimeInterval: 0.05)
            @unknown default:
                throw ExactAppServiceStopError.serviceDidNotUnregister
            }
        }
        throw ExactAppServiceStopError.serviceDidNotUnregister
    }

    private static func stopExactInstalledApplications() throws {
        let expectedBundle = Bundle.main.bundleURL.resolvingSymlinksInPath().standardizedFileURL
        let currentPID = ProcessInfo.processInfo.processIdentifier
        var applications = try exactApplications(expectedBundle: expectedBundle, excluding: currentPID)
        for application in applications where !application.terminate() {
            throw ExactAppServiceStopError.terminateRejected(pid: application.processIdentifier)
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
            throw ExactAppServiceStopError.forceTerminateRejected(pid: application.processIdentifier)
        }
        let forceDeadline = Date().addingTimeInterval(2)
        while Date() < forceDeadline {
            if try exactApplications(expectedBundle: expectedBundle, excluding: currentPID).isEmpty {
                try proveQuietAbsence(expectedBundle: expectedBundle, currentPID: currentPID)
                return
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw ExactAppServiceStopError.applicationStillRunning
    }

    private static func proveQuietAbsence(expectedBundle: URL, currentPID: pid_t) throws {
        Thread.sleep(forTimeInterval: 0.1)
        if try !exactApplications(expectedBundle: expectedBundle, excluding: currentPID).isEmpty {
            throw ExactAppServiceStopError.applicationStillRunning
        }
    }

    private static func exactApplications(expectedBundle: URL, excluding currentPID: pid_t) throws -> [NSRunningApplication] {
        let applications = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { $0.processIdentifier != currentPID && !$0.isTerminated }
        for application in applications {
            guard application.bundleURL?.resolvingSymlinksInPath().standardizedFileURL == expectedBundle else {
                throw ExactAppServiceStopError.runningIdentityMismatch(pid: application.processIdentifier)
            }
        }
        return applications
    }
}
