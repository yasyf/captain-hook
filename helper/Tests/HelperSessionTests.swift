import Foundation
import Testing

@Suite struct HelperSessionTests {
    @Test func protocolUsesOneExactHostWireAndDistinctAuthorityRoles() {
        #expect(helperWireBuild == "captain-hook.host.v1")
        #expect(helperPingOperation == "captain.helper.ping.v1")
        #expect(helperNotifyOperation == "captain.helper.notify.v1")
        #expect(helperNextOperation == "captain.helper.next.v1")
        #expect(Set([
            helperConsumerRole,
            helperBrokerLifecycleRole,
            helperBrokerHandoffRole,
            helperClientRole,
        ]).count == 4)
    }

    @Test func notificationPayloadDerivesStablePresentationIdentity() throws {
        let request = NotifyRequest(
            kind: "pr_open",
            title: "Block force-pushes",
            subtitle: "captain-hook",
            body: "Rule guard-rm-rf opened",
            url: "https://github.com/yasyf/captain-hook/pull/12",
            repo: "github.com/yasyf/captain-hook"
        )
        let payload = try NotifyPayload(request)
        #expect(payload.identifier == "capt-hook.pr_open.https://github.com/yasyf/captain-hook/pull/12")
        #expect(payload.title == request.title)
        #expect(payload.repo == request.repo)
    }

    @Test func notificationPayloadRejectsInvalidIdentityAndURL() {
        #expect(throws: NotifyPayloadError.invalidIdentity) {
            try NotifyPayload(NotifyRequest(
                kind: "", title: "title", subtitle: nil, body: nil, url: nil, repo: nil
            ))
        }
        #expect(throws: NotifyPayloadError.invalidURL) {
            try NotifyPayload(NotifyRequest(
                kind: "pr_open", title: "title", subtitle: nil, body: nil,
                url: "file:///tmp/private", repo: nil
            ))
        }
    }

    @Test func helperReplyEncodingIsExact() throws {
        let data = try JSONEncoder().encode(HelperReply.ping(version: "1.2.3"))
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(object["ok"] as? Bool == true)
        #expect(object["version"] as? String == "1.2.3")
        #expect(object["error"] == nil)
    }
}
