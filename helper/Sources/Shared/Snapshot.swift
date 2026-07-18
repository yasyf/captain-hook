import Foundation

// Models for ~/.capt-hook/status.json (schema_version 1), pinned byte-for-byte by
// tests/fixtures/status-json-v1.golden.json. Written by Python, read by the widget.

struct Snapshot: Decodable, Sendable {
    let schemaVersion: Int
    let generatedAt: Date
    let captHookVersion: String
    let repos: [RepoStatus]
    let health: Health

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
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
