import DaemonKit
import Foundation

struct NotifyPayload: Sendable {
    let identifier: String
    let kind: String
    let title: String
    let subtitle: String?
    let body: String?
    let url: String?
    let repo: String?
}

final class HelperHandler: @unchecked Sendable {
    private let version: String
    private let onNotify: @Sendable (NotifyPayload) async -> Void

    init(version: String, onNotify: @escaping @Sendable (NotifyPayload) async -> Void) {
        self.version = version
        self.onNotify = onNotify
    }

    func handle(_ request: SocketRequest) async -> SocketResponse {
        do {
            return .terminal(SocketTerminal(payload: try JSONEncoder().encode(await process(request))))
        } catch {
            return .terminal(SocketTerminal(error: "encode reply: \(error)"))
        }
    }

    func process(_ request: SocketRequest) async -> HelperReply {
        switch request.operation {
        case "ping":
            guard request.payload.isEmpty else { return .failure("ping payload must be empty") }
            return .ping(version: version)
        case "notify":
            guard let payload = try? JSONDecoder().decode(NotifyRequest.self, from: request.payload) else {
                return .failure("bad notify request")
            }
            return await notify(payload)
        default:
            return .failure("unknown operation")
        }
    }

    private func notify(_ request: NotifyRequest) async -> HelperReply {
        guard !request.title.isEmpty else { return .failure("title required") }
        if let url = request.url, !isHTTPURL(url) { return .failure("url must be http or https") }
        let payload = NotifyPayload(
            identifier: notificationIdentifier(
                kind: request.kind,
                title: request.title,
                body: request.body ?? "",
                url: request.url
            ),
            kind: request.kind,
            title: request.title,
            subtitle: request.subtitle,
            body: request.body,
            url: request.url,
            repo: request.repo
        )
        await onNotify(payload)
        return .ok
    }
}
