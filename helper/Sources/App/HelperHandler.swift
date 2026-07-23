import Foundation

struct NotifyPayload: Sendable {
    let identifier: String
    let kind: String
    let title: String
    let subtitle: String?
    let body: String?
    let url: String?
    let repo: String?

    init(_ request: NotifyRequest) throws {
        guard !request.kind.isEmpty, !request.title.isEmpty else {
            throw NotifyPayloadError.invalidIdentity
        }
        if let url = request.url, !isHTTPURL(url) {
            throw NotifyPayloadError.invalidURL
        }
        identifier = notificationIdentifier(
            kind: request.kind,
            title: request.title,
            body: request.body ?? "",
            url: request.url
        )
        kind = request.kind
        title = request.title
        subtitle = request.subtitle
        body = request.body
        url = request.url
        repo = request.repo
    }
}

enum NotifyPayloadError: Error, Equatable {
    case invalidIdentity
    case invalidURL
}
