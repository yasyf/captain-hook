import DaemonKit
import Foundation

// Models for ~/.capt-hook/status.json (schema_version 1), pinned byte-for-byte by
// tests/fixtures/status-json-v1.golden.json. Written by Python, read by the widget.

enum SnapshotContract {
    static let identity = "captain-hook.status.v1"
    static let descriptor = "captain-hook.status.v1|identity:string|schema_version:uint64|fingerprint:sha256hex|" +
        "generated_at:rfc3339|capt_hook_version:string|repos:[{key:string,name:string,watching:bool," +
        "counts:{watching:int64,eligible:int64,pr_open:int64,accepted:int64,rejected:int64,stale:int64}," +
        "open_prs:[{candidate_id:int64,rule:string,kind:string,title:string,url:string,opened_at:rfc3339}]}]|" +
        "health:{ok:bool,consecutive_failures:int64,failing_since:rfc3339|null,last_run_at:rfc3339|null," +
        "judge_pending:int64}"
    static let fingerprint = "ef46e55d15f15bc622e6cbf032fbb23f7917e232e01a44a94f426643c10738bc"

    static func codec() throws -> SnapshotCodec<Snapshot> {
        let schema = try SnapshotSchema(identity: identity, fingerprint: fingerprint)
        return SnapshotCodec(schema: schema) { data, decoder in
            try decode(data, using: decoder)
        }
    }

    static func decode(_ data: Data, using decoder: JSONDecoder) throws -> Snapshot {
        let snapshot = try decoder.decode(Snapshot.self, from: data)
        guard snapshot.identity == identity,
              snapshot.schemaVersion == 1,
              snapshot.fingerprint == fingerprint
        else {
            throw SnapshotContractError.schemaSkew
        }
        return snapshot
    }
}

enum SnapshotContractError: Error {
    case schemaSkew
}

struct Snapshot: Decodable, Sendable {
    let identity: String
    let schemaVersion: Int
    let fingerprint: String
    let generatedAt: Date
    let captHookVersion: String
    let repos: [RepoStatus]
    let health: Health

    enum CodingKeys: String, CodingKey {
        case identity
        case schemaVersion = "schema_version"
        case fingerprint
        case generatedAt = "generated_at"
        case captHookVersion = "capt_hook_version"
        case repos, health
    }
}

struct RepoStatus: Decodable, Sendable {
    let key: String
    let name: String
    let watching: Bool
    let counts: Counts
    let openPRs: [OpenPR]

    enum CodingKeys: String, CodingKey {
        case key, name, watching, counts
        case openPRs = "open_prs"
    }
}

struct Counts: Decodable, Sendable {
    let watching: Int
    let eligible: Int
    let prOpen: Int
    let accepted: Int
    let rejected: Int
    let stale: Int

    enum CodingKeys: String, CodingKey {
        case watching, eligible, accepted, rejected, stale
        case prOpen = "pr_open"
    }
}

struct OpenPR: Decodable, Sendable, Identifiable {
    let candidateID: Int
    let rule: String
    let kind: String
    let title: String
    let url: String
    let openedAt: Date

    var id: Int { candidateID }

    enum CodingKeys: String, CodingKey {
        case rule, kind, title, url
        case candidateID = "candidate_id"
        case openedAt = "opened_at"
    }
}

struct Health: Decodable, Sendable {
    let ok: Bool
    let consecutiveFailures: Int
    let failingSince: Date?
    let lastRunAt: Date?
    let judgePending: Int

    enum CodingKeys: String, CodingKey {
        case ok
        case consecutiveFailures = "consecutive_failures"
        case failingSince = "failing_since"
        case lastRunAt = "last_run_at"
        case judgePending = "judge_pending"
    }
}

extension Snapshot {
    // Total open PRs across watched repos.
    var openPRCount: Int { repos.reduce(0) { $0 + $1.openPRs.count } }

    // Newest-first flattening for the medium/large widget rows.
    var allOpenPRs: [OpenPR] {
        repos.flatMap(\.openPRs).sorted { $0.openedAt > $1.openedAt }
    }
}

extension JSONDecoder {
    // Dual ISO8601: fractional-seconds form first, then plain — matching
    // DaemonKit's SnapshotWatcher decoder so a direct read agrees with the watch.
    static var snapshot: JSONDecoder {
        let decoder = JSONDecoder()
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        decoder.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            guard let date = fractional.date(from: raw) ?? plain.date(from: raw) else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "bad ISO8601: \(raw)")
                )
            }
            return date
        }
        return decoder
    }
}
