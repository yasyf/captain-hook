import Foundation

// Daemonkit v0.21 wraps every business-lane reply in this envelope: a product
// failure is delivered as data so the session's own error keeps meaning the
// session failed. Body is base64 in the JSON because Go marshals `[]byte` that
// way, which is exactly what JSONDecoder's default data strategy reads.
struct BusinessTerminal: Decodable, Equatable, Sendable {
    let body: Data?
    let error: ProductError?

    struct ProductError: Decodable, Equatable, Sendable {
        let code: String?
        let message: String?
    }

    private enum CodingKeys: String, CodingKey {
        case body = "Body"
        case error
    }
}

enum BusinessTerminalError: Error, Equatable, CustomStringConvertible {
    case product(code: String, message: String)
    case emptyBody(operation: String)

    var description: String {
        switch self {
        case let .product(code, message):
            code.isEmpty ? message : "\(code): \(message)"
        case let .emptyBody(operation):
            "\(operation) returned a business terminal with no reply body"
        }
    }
}

/// Unwraps one business terminal into the product reply body it carries,
/// surfacing a product failure as a thrown error rather than as reply bytes.
func businessReplyBody(_ payload: Data, operation: String) throws -> Data {
    let terminal = try JSONDecoder().decode(BusinessTerminal.self, from: payload)
    if let error = terminal.error {
        throw BusinessTerminalError.product(code: error.code ?? "", message: error.message ?? "")
    }
    guard let body = terminal.body, !body.isEmpty else {
        throw BusinessTerminalError.emptyBody(operation: operation)
    }
    return body
}
