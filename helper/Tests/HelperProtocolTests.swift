import Foundation
import Testing

private final class BundleToken {}

// Each row is exactly the bytes crossing helper.sock; the framing \n is the
// row separator (tests/fixtures/helper-sock-v1.golden.jsonl).
private func goldenRows() -> [Data] {
    guard let url = Bundle(for: BundleToken.self).url(forResource: "helper-sock-v1.golden", withExtension: "jsonl"),
          let raw = try? Data(contentsOf: url)
    else { fatalError("helper-sock-v1.golden.jsonl missing from test bundle") }
    let text = String(decoding: raw, as: UTF8.self)
    return text.split(separator: "\n", omittingEmptySubsequences: true).map { Data($0.utf8) }
}

@Suite struct HelperProtocolWireTests {
    static let rows = goldenRows()

    @Test func decodesPingRequest() throws {
        let request = try JSONDecoder().decode(HelperRequest.self, from: Self.rows[0])
        #expect(request == HelperRequest(
            v: 1, op: "ping", kind: nil, title: nil, subtitle: nil, body: nil, url: nil, repo: nil
        ))
    }

    @Test func decodesNotifyRequest() throws {
        let request = try JSONDecoder().decode(HelperRequest.self, from: Self.rows[2])
        #expect(request == HelperRequest(
            v: 1,
            op: "notify",
            kind: "pr_open",
            title: "Block force-pushes",
            subtitle: "captain-hook",
            body: "Rule guard-rm-rf opened",
            url: "https://github.com/yasyf/captain-hook/pull/12",
            repo: "github.com/yasyf/captain-hook"
        ))
    }

    @Test func encodesPingReplyByteIdentical() {
        #expect(HelperReply.ping(version: "1.0.0").wireData() == Self.rows[1])
    }

    @Test func encodesNotifySuccessByteIdentical() {
        #expect(HelperReply.ok.wireData() == Self.rows[3])
    }

    @Test func encodesUnknownOpErrorByteIdentical() {
        #expect(HelperReply.failure("unknown op").wireData() == Self.rows[4])
    }

    // Canonical encoding: '/' and non-ASCII stay raw UTF-8 — never \/ or \uXXXX.
    @Test func encodesFailureSlashAndUnicodeRaw() {
        let reply = HelperReply.failure("bad url: https://ex\u{E4}mple.com/x")
        #expect(reply.wireData() == Data("{\"v\":1,\"ok\":false,\"error\":\"bad url: https://ex\u{E4}mple.com/x\"}".utf8))
    }
}
