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

// The SocketServer handler: one helper.sock v1 request line → one reply line.
final class HelperHandler: @unchecked Sendable {
    private let version: String
    private let onNotify: @Sendable (NotifyPayload) -> Void

    init(version: String, onNotify: @escaping @Sendable (NotifyPayload) -> Void) {
        self.version = version
        self.onNotify = onNotify
    }

    func handle(_ line: Data) -> Data {
        process(line).wireData()
    }

    func process(_ line: Data) -> HelperReply {
        guard let request = try? JSONDecoder().decode(HelperRequest.self, from: line) else {
            return .failure("bad request")
        }
        guard request.v == helperProtocolVersion else {
            return .failure("unsupported protocol version")
        }
        switch request.op {
        case "ping":
            return .ping(version: version)
        case "notify":
            return notify(request)
        default:
            return .failure("unknown op")
        }
    }

    private func notify(_ request: HelperRequest) -> HelperReply {
        guard let title = request.title, !title.isEmpty else { return .failure("title required") }
        if let url = request.url, !isHTTPURL(url) { return .failure("url must be http or https") }
        let kind = request.kind ?? "generic"
        let payload = NotifyPayload(
            identifier: notificationIdentifier(kind: kind, title: title, body: request.body ?? "", url: request.url),
            kind: kind,
            title: title,
            subtitle: request.subtitle,
            body: request.body,
            url: request.url,
            repo: request.repo
        )
        onNotify(payload)
        return .ok
    }
}
