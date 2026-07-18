import Foundation
import Testing

@Suite struct NotificationIdentifierTests {
    @Test func urlBecomesTheSuffix() {
        let identifier = notificationIdentifier(
            kind: "pr_open",
            title: "Block force-pushes",
            body: "Rule guard-rm-rf opened",
            url: "https://github.com/yasyf/captain-hook/pull/12"
        )
        #expect(identifier == "capt-hook.pr_open.https://github.com/yasyf/captain-hook/pull/12")
    }

    // sha256("Review pipeline failing" + "3 consecutive failures")[:12].
    @Test func noURLUsesSHA256Prefix() {
        let identifier = notificationIdentifier(
            kind: "review_failure",
            title: "Review pipeline failing",
            body: "3 consecutive failures",
            url: nil
        )
        #expect(identifier == "capt-hook.review_failure.4bbe06081170")
    }

    @Test func emptyURLFallsBackToHash() {
        let identifier = notificationIdentifier(
            kind: "review_failure",
            title: "Review pipeline failing",
            body: "3 consecutive failures",
            url: ""
        )
        #expect(identifier == "capt-hook.review_failure.4bbe06081170")
    }
}
