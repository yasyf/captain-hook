import CryptoKit
import Foundation

// helper.sock v1 — captain-hook's own frozen protocol, pinned byte-for-byte by
// tests/fixtures/helper-sock-v1.golden.jsonl (STYLEGUIDE.md § Wire-protocol freeze).

let helperProtocolVersion = 1

struct HelperRequest: Decodable, Equatable {
    let v: Int
    let op: String
    let kind: String?
    let title: String?
    let subtitle: String?
    let body: String?
    let url: String?
    let repo: String?
}

// Hand-serialized in frozen order — swift-foundation's JSONEncoder emits keys in
// hash-seeded order, so it cannot hit the byte-pinned golden.
struct HelperReply: Equatable {
    let v: Int
    let ok: Bool
    let version: String?
    let error: String?

    static func ping(version: String) -> HelperReply {
        HelperReply(v: helperProtocolVersion, ok: true, version: version, error: nil)
    }

    static let ok = HelperReply(v: helperProtocolVersion, ok: true, version: nil, error: nil)

    static func failure(_ message: String) -> HelperReply {
        HelperReply(v: helperProtocolVersion, ok: false, version: nil, error: message)
    }

    // Wire order: v, ok, then version or error. Compact, slashes unescaped.
    func wireData() -> Data {
        var json = #"{"v":\#(v),"ok":\#(ok ? "true" : "false")"#
        if let version { json += #","version":\#(Self.jsonString(version))"# }
        if let error { json += #","error":\#(Self.jsonString(error))"# }
        json += "}"
        return Data(json.utf8)
    }

    private static func jsonString(_ value: String) -> String {
        var out = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": out += #"\""#
            case "\\": out += #"\\"#
            case "\n": out += #"\n"#
            case "\r": out += #"\r"#
            case "\t": out += #"\t"#
            default:
                if scalar.value < 0x20 { out += String(format: #"\u%04x"#, Int(scalar.value)) }
                else { out.unicodeScalars.append(scalar) }
            }
        }
        out += "\""
        return out
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
