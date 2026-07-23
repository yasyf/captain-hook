import CryptoKit
import Foundation
import Testing

private final class SnapshotBundleToken {}

@Suite struct SnapshotDecodeTests {
    private func golden() throws -> Data {
        let bundle = Bundle(for: SnapshotBundleToken.self)
        let url = try #require(bundle.url(forResource: "status-json-v1.golden", withExtension: "json"))
        return try Data(contentsOf: url)
    }

    @Test func decodesGoldenSnapshotFully() throws {
        let snapshot = try SnapshotContract.decode(golden(), using: .snapshot)

        #expect(snapshot.identity == SnapshotContract.identity)
        #expect(snapshot.schemaVersion == 1)
        #expect(snapshot.fingerprint == SnapshotContract.fingerprint)
        #expect(snapshot.captHookVersion == "9.4.0")
        #expect(snapshot.generatedAt == ISO8601DateFormatter().date(from: "2026-07-15T12:00:00Z"))

        #expect(snapshot.repos.count == 1)
        let repo = try #require(snapshot.repos.first)
        #expect(repo.key == "github.com/yasyf/captain-hook")
        #expect(repo.name == "captain-hook")
        #expect(repo.watching)
        #expect(repo.counts.watching == 2)
        #expect(repo.counts.eligible == 1)
        #expect(repo.counts.prOpen == 1)
        #expect(repo.counts.accepted == 4)
        #expect(repo.counts.rejected == 3)
        #expect(repo.counts.stale == 0)

        #expect(repo.openPRs.count == 1)
        let pr = try #require(repo.openPRs.first)
        #expect(pr.candidateID == 42)
        #expect(pr.rule == "guard-rm-rf")
        #expect(pr.kind == "create")
        #expect(pr.title == "[capt-hook] Block force-pushes")
        #expect(pr.url == "https://github.com/yasyf/captain-hook/pull/12")
        #expect(pr.openedAt == ISO8601DateFormatter().date(from: "2026-07-15T11:30:00Z"))

        #expect(snapshot.health.ok)
        #expect(snapshot.health.consecutiveFailures == 0)
        #expect(snapshot.health.failingSince == nil)
        #expect(snapshot.health.lastRunAt == ISO8601DateFormatter().date(from: "2026-07-15T11:59:00Z"))
        #expect(snapshot.health.judgePending == 3)
    }

    @Test func derivedHelpersMatchGolden() throws {
        let snapshot = try SnapshotContract.decode(golden(), using: .snapshot)
        #expect(snapshot.openPRCount == 1)
        #expect(snapshot.allOpenPRs.map(\.candidateID) == [42])
    }

    @Test func decodesFractionalTimestamp() throws {
        let text = String(decoding: try golden(), as: UTF8.self)
            .replacingOccurrences(of: "2026-07-15T11:30:00Z", with: "2026-07-15T11:30:00.500Z")
        let snapshot = try SnapshotContract.decode(Data(text.utf8), using: .snapshot)
        let pr = try #require(snapshot.repos.first?.openPRs.first)
        let whole = try #require(ISO8601DateFormatter().date(from: "2026-07-15T11:30:00Z"))
        #expect(pr.openedAt == whole.addingTimeInterval(0.5))
    }

    @Test func contractFingerprintMatchesDescriptor() {
        let digest = SHA256.hash(data: Data(SnapshotContract.descriptor.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
        #expect(digest == SnapshotContract.fingerprint)
    }

    @Test func rejectsSchemaIdentityAndFingerprintSkew() throws {
        let data = try golden()
        for (old, replacement) in [
            (SnapshotContract.identity, "captain-hook.foreign.v1"),
            (SnapshotContract.fingerprint, String(repeating: "0", count: 64)),
        ] {
            let text = String(decoding: data, as: UTF8.self).replacingOccurrences(of: old, with: replacement)
            #expect(throws: SnapshotContractError.self) {
                try SnapshotContract.decode(Data(text.utf8), using: .snapshot)
            }
        }
    }
}
