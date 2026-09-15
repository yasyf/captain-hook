import Foundation
import Testing

private struct FakeApplication: StoppableApplication {
    let processIdentifier: pid_t
    var isTerminated = false
    var bundleURL: URL?
    func terminate() -> Bool { true }
    func forceTerminate() -> Bool { true }
}

@Suite struct ExactInstalledAppStopTests {
    private let installed = URL(fileURLWithPath: "/Users/someone/Applications/Captain Hook.app", isDirectory: true)

    private func running(
        _ applications: [FakeApplication],
        alive: Set<pid_t>
    ) throws -> [StoppableApplication] {
        try ExactInstalledAppStop.running(
            among: applications,
            expectedBundle: installed.resolvingSymlinksInPath().standardizedFileURL,
            currentPID: 1,
            isAlive: { alive.contains($0) }
        )
    }

    @Test func exitedGenerationWhoseBundleURLIsGoneIsAbsent() throws {
        let torndown = FakeApplication(processIdentifier: 42, isTerminated: false, bundleURL: nil)

        #expect(try running([torndown], alive: []).isEmpty)
    }

    @Test func liveGenerationAtAnUnexpectedPathIsRefused() {
        let foreign = FakeApplication(
            processIdentifier: 42,
            bundleURL: URL(fileURLWithPath: "/Applications/Captain Hook.app", isDirectory: true)
        )

        #expect(throws: ExactInstalledAppStopError.self) { try running([foreign], alive: [42]) }
    }

    @Test func liveGenerationWhoseBundleURLIsGoneIsRefused() {
        let unreadable = FakeApplication(processIdentifier: 42, bundleURL: nil)

        #expect(throws: ExactInstalledAppStopError.self) { try running([unreadable], alive: [42]) }
    }

    @Test func liveInstalledGenerationIsReturnedForStopping() throws {
        let installedApp = FakeApplication(processIdentifier: 42, bundleURL: installed)

        let stoppable = try running([installedApp], alive: [42])

        #expect(stoppable.map(\.processIdentifier) == [42])
    }

    @Test func theControllerItselfIsNeverStopped() throws {
        let controller = FakeApplication(
            processIdentifier: 1,
            bundleURL: URL(fileURLWithPath: "/tmp/staged/Captain Hook.app", isDirectory: true)
        )

        #expect(try running([controller], alive: [1]).isEmpty)
    }

    @Test func aTerminatedRecordIsAbsentEvenWhilePIDReuseKeepsItAlive() throws {
        let reaped = FakeApplication(processIdentifier: 42, isTerminated: true, bundleURL: nil)

        #expect(try running([reaped], alive: [42]).isEmpty)
    }
}
