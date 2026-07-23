import SwiftUI
import WidgetKit

extension Snapshot {
    static let sample = Snapshot(
        identity: SnapshotContract.identity,
        schemaVersion: 1,
        fingerprint: SnapshotContract.fingerprint,
        generatedAt: .now,
        captHookVersion: "9.4.0",
        repos: [
            RepoStatus(
                key: "github.com/yasyf/captain-hook",
                name: "captain-hook",
                watching: true,
                counts: Counts(watching: 2, eligible: 1, prOpen: 2, accepted: 4, rejected: 3, stale: 0),
                openPRs: [
                    OpenPR(
                        candidateID: 42, rule: "guard-rm-rf", kind: "create",
                        title: "[capt-hook] Block force-pushes",
                        url: "https://github.com/yasyf/captain-hook/pull/12",
                        openedAt: Date().addingTimeInterval(-1800)
                    ),
                    OpenPR(
                        candidateID: 43, rule: "no-bare-except", kind: "create",
                        title: "[capt-hook] Forbid bare except",
                        url: "https://github.com/yasyf/captain-hook/pull/13",
                        openedAt: Date().addingTimeInterval(-7200)
                    ),
                ]
            ),
        ],
        health: Health(ok: true, consecutiveFailures: 0, failingSince: nil, lastRunAt: .now, judgePending: 3)
    )

    static let sampleFailing = Snapshot(
        identity: SnapshotContract.identity,
        schemaVersion: 1,
        fingerprint: SnapshotContract.fingerprint,
        generatedAt: .now,
        captHookVersion: "9.4.0",
        repos: [
            RepoStatus(
                key: "github.com/yasyf/captain-hook",
                name: "captain-hook",
                watching: true,
                counts: Counts(watching: 2, eligible: 0, prOpen: 0, accepted: 4, rejected: 3, stale: 0),
                openPRs: []
            ),
        ],
        health: Health(ok: false, consecutiveFailures: 3, failingSince: .now, lastRunAt: .now, judgePending: 0)
    )
}

#Preview("small", as: .systemSmall) {
    CaptainHookWidget()
} timeline: {
    StatusEntry(date: .now, state: .ok(.sample))
    StatusEntry(date: .now, state: .stale(.sample))
    StatusEntry(date: .now, state: .noFile)
}

#Preview("medium", as: .systemMedium) {
    CaptainHookWidget()
} timeline: {
    StatusEntry(date: .now, state: .ok(.sample))
    StatusEntry(date: .now, state: .ok(.sampleFailing))
    StatusEntry(date: .now, state: .unreadable)
}

#Preview("large", as: .systemLarge) {
    CaptainHookWidget()
} timeline: {
    StatusEntry(date: .now, state: .ok(.sample))
    StatusEntry(date: .now, state: .stale(.sample))
}
