import DaemonKit
import Foundation

private enum BridgeError: Error, CustomStringConvertible {
    case usage
    case input(String)
    case rejected(String)
    case remote(String)
    case missingPayload

    var description: String {
        switch self {
        case .usage:
            "usage: capt-hook-helper-client --socket PATH ping|notify"
        case let .input(message):
            message
        case let .rejected(message):
            "request rejected: \(message)"
        case let .remote(message):
            "helper error: \(message)"
        case .missingPayload:
            "helper returned no payload"
        }
    }
}

private struct Arguments {
    let socket: String
    let operation: String

    init(_ arguments: [String]) throws {
        guard arguments.count == 3, arguments[0] == "--socket",
              !arguments[1].isEmpty, ["ping", "notify"].contains(arguments[2])
        else { throw BridgeError.usage }
        socket = arguments[1]
        operation = arguments[2]
    }
}

@main
private enum CaptainHookBridge {
    static func main() async {
        do {
            let arguments = try Arguments(Array(CommandLine.arguments.dropFirst()))
            let input = try payload(for: arguments.operation)
            let client = try SocketClient(
                path: arguments.socket,
                build: buildVersion,
                trust: .sameEffectiveUser
            )
            defer { client.close() }
            let terminal = try await client.call(
                operation: arguments.operation,
                payload: input,
                deadline: Date().addingTimeInterval(5)
            )
            guard !terminal.rejected else {
                throw BridgeError.rejected(terminal.reason ?? "unspecified")
            }
            if let error = terminal.error { throw BridgeError.remote(error) }
            guard let payload = terminal.payload else { throw BridgeError.missingPayload }
            let reply = try JSONDecoder().decode(HelperReply.self, from: payload)
            FileHandle.standardOutput.write(try JSONEncoder().encode(reply))
            FileHandle.standardOutput.write(Data("\n".utf8))
            if !reply.ok { exit(3) }
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(error is BridgeError ? 2 : 1)
        }
    }

    private static var buildVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }

    private static func payload(for operation: String) throws -> Data {
        guard operation == "notify" else { return Data() }
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard !data.isEmpty else { throw BridgeError.input("notify request is required on stdin") }
        do {
            let request = try JSONDecoder().decode(NotifyRequest.self, from: data)
            return try JSONEncoder().encode(request)
        } catch {
            throw BridgeError.input("invalid notify request: \(error)")
        }
    }
}
