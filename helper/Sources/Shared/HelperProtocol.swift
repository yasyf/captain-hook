import CryptoKit
import Foundation

let helperWireBuild = "captain-hook.host.v1"
let helperPingOperation = "captain.helper.ping.v1"
let helperNotifyOperation = "captain.helper.notify.v1"
let helperNextOperation = "captain.helper.next.v1"
let helperConsumerRole = "com.yasyf.captain-hook.helper.consumer.v1"
let helperBrokerLifecycleRole = "com.yasyf.captain-hook.helper.broker-lifecycle.v1"
let helperBrokerHandoffRole = "com.yasyf.captain-hook.helper.broker-handoff.v1"
let helperClientRole = "com.yasyf.captain-hook.helper.client.v1"

struct NotifyRequest: Codable, Equatable, Sendable {
    let kind: String
    let title: String
    let subtitle: String?
    let body: String?
    let url: String?
    let repo: String?
}

struct HelperReply: Codable, Equatable, Sendable {
    let ok: Bool
    let version: String?
    let error: String?

    static func ping(version: String) -> HelperReply {
        HelperReply(ok: true, version: version, error: nil)
    }

    static let ok = HelperReply(ok: true, version: nil, error: nil)

    static func failure(_ message: String) -> HelperReply {
        HelperReply(ok: false, version: nil, error: message)
    }
}

func isHTTPURL(_ string: String) -> Bool {
    guard let url = URL(string: string), let scheme = url.scheme?.lowercased() else { return false }
    return scheme == "http" || scheme == "https"
}

// capt-hook.<kind>.<url, or sha256(title+body)[:12] when no url>.
func notificationIdentifier(kind: String, title: String, body: String, url: String?) -> String {
    let suffix: String
    if let url, !url.isEmpty {
        suffix = url
    } else {
        let digest = SHA256.hash(data: Data((title + body).utf8))
        suffix = String(digest.map { String(format: "%02x", $0) }.joined().prefix(12))
    }
    return "capt-hook.\(kind).\(suffix)"
}
