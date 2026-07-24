import AppKit
import UserNotifications

// UNUserNotificationCenter delegate. Categories carry NO action buttons — the
// whole banner is the click target. No .timeSensitive (restricted entitlement).
final class NotificationController: NSObject, UNUserNotificationCenterDelegate {
    private static let categories = ["pr_open", "pr_merged", "review_failure", "update_installed", "update_failed"]

    func requestAuthorizationIfNeeded() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if let error {
                Log.notify.error("authorization failed: \(String(describing: error), privacy: .public)")
            } else {
                Log.notify.notice("authorization granted=\(granted)")
            }
        }
    }

    func registerCategories() {
        let cats = Self.categories.map {
            UNNotificationCategory(identifier: $0, actions: [], intentIdentifiers: [], options: [])
        }
        UNUserNotificationCenter.current().setNotificationCategories(Set(cats))
    }

    func post(_ payload: NotifyPayload) {
        let content = UNMutableNotificationContent()
        content.title = payload.title
        if let subtitle = payload.subtitle { content.subtitle = subtitle }
        if let body = payload.body { content.body = body }
        // Unknown kind falls through to the default (empty) category.
        content.categoryIdentifier = Self.categories.contains(payload.kind) ? payload.kind : ""
        if let repo = payload.repo { content.threadIdentifier = repo }
        if let url = payload.url { content.userInfo = ["url": url] }
        let request = UNNotificationRequest(identifier: payload.identifier, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error {
                Log.notify.error("post failed: \(String(describing: error), privacy: .public)")
            }
        }
    }

    func userNotificationCenter(
        _: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        guard let urlString = response.notification.request.content.userInfo["url"] as? String,
              isHTTPURL(urlString), let url = URL(string: urlString)
        else { return }
        NSWorkspace.shared.open(url)
    }

    func userNotificationCenter(
        _: UNUserNotificationCenter,
        willPresent _: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }
}
