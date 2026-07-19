import Foundation
import Testing

@Suite struct HelperProtocolTests {
    @Test func notifyRequestRoundTrips() throws {
        let request = NotifyRequest(
            kind: "pr_open",
            title: "Block force-pushes",
            subtitle: "captain-hook",
            body: "Rule guard-rm-rf opened",
            url: "https://github.com/yasyf/captain-hook/pull/12",
            repo: "github.com/yasyf/captain-hook"
        )
        let encoded = try JSONEncoder().encode(request)
        #expect(try JSONDecoder().decode(NotifyRequest.self, from: encoded) == request)
    }

    @Test func repliesRoundTrip() throws {
        for reply in [HelperReply.ping(version: "1.2.3"), .ok, .failure("title required")] {
            let encoded = try JSONEncoder().encode(reply)
            #expect(try JSONDecoder().decode(HelperReply.self, from: encoded) == reply)
        }
    }

    @Test(arguments: ["https://x.test", "http://localhost", "HTTPS://X.TEST"])
    func acceptsHTTPURLs(_ value: String) {
        #expect(isHTTPURL(value))
    }

    @Test(arguments: ["file:///tmp/x", "javascript:alert(1)", "relative/path"])
    func rejectsNonHTTPURLs(_ value: String) {
        #expect(!isHTTPURL(value))
    }
}
