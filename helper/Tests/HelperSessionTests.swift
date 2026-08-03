import Foundation
import Testing

@Suite struct HelperSessionTests {
    @Test func protocolPinsTheOneHostLabelSchemaAndOperations() {
        #expect(hostServiceLabel == "com.yasyf.captain-hook.host.v1")
        #expect(helperSchema == "captain-hook.host.v1")
        #expect(helperPingOperation == "captain.helper.ping.v1")
        #expect(helperNotifyOperation == "captain.helper.notify.v1")
        #expect(helperNextOperation == "captain.helper.next.v1")
        #expect(hostRuntimeHealthOperation == "captain.host.v1.runtime.health")
    }

    @Test func runtimeHealthDecodesTheServingHostBuild() throws {
        let data = Data(#"{"schema":1,"runtime_build":"1.2.3","runtime_protocol":1,"pid":4242}"#.utf8)
        #expect(try JSONDecoder().decode(RuntimeHealth.self, from: data) == RuntimeHealth(runtimeBuild: "1.2.3"))
    }

    @Test func businessTerminalUnwrapsTheProductReplyBody() throws {
        let inner = Data(#"{"schema":1,"runtime_build":"1.2.3","runtime_protocol":1,"pid":4242}"#.utf8)
        let envelope = Data(#"{"Body":"\#(inner.base64EncodedString())"}"#.utf8)
        let body = try businessReplyBody(envelope, operation: hostRuntimeHealthOperation)
        #expect(try JSONDecoder().decode(RuntimeHealth.self, from: body) == RuntimeHealth(runtimeBuild: "1.2.3"))
    }

    @Test func businessTerminalSurfacesTheProductErrorItCarriesAsData() {
        let envelope = Data(#"{"Body":null,"error":{"code":"captain.busy","message":"worker is gone"}}"#.utf8)
        #expect(throws: BusinessTerminalError.product(code: "captain.busy", message: "worker is gone")) {
            try businessReplyBody(envelope, operation: helperNotifyOperation)
        }
    }

    @Test func businessTerminalRefusesABodilessTerminalAndTheBareV020Reply() {
        #expect(throws: BusinessTerminalError.emptyBody(operation: helperNextOperation)) {
            try businessReplyBody(Data(#"{"Body":null}"#.utf8), operation: helperNextOperation)
        }
        #expect(throws: BusinessTerminalError.emptyBody(operation: helperPingOperation)) {
            try businessReplyBody(Data(#"{"ok":true,"version":"1.2.3"}"#.utf8), operation: helperPingOperation)
        }
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
