import DaemonKit
import Foundation

private enum BridgeError: Error, CustomStringConvertible {
    case usage
    case input(String)
    case rejected(String)
    case remote(String)
    case missingPayload
    case runtimeSkew(expected: String, found: String)

    var description: String {
        switch self {
        case .usage:
            "usage: capt-hook-helper-client ping|notify"
        case let .input(message):
            message
        case let .rejected(message):
            "request rejected: \(message)"
        case let .remote(message):
            "helper error: \(message)"
        case .missingPayload:
            "helper returned no payload"
        case let .runtimeSkew(expected, found):
            "runtime build \(found) is not this bridge's build \(expected)"
        }
    }
}

private struct Arguments {
    let operation: String

    init(_ arguments: [String]) throws {
        guard arguments.count == 1, ["ping", "notify"].contains(arguments[0])
        else { throw BridgeError.usage }
        operation = arguments[0]
    }
}

@main
private enum CaptainHookBridge {
    static func main() async {
        do {
            let arguments = try Arguments(Array(CommandLine.arguments.dropFirst()))
            let input = try payload(for: arguments.operation)
            let deadline = Date().addingTimeInterval(5)
            let client = try await SocketClient(
                path: HelperPaths.hostSocket.path,
                schema: helperSchema,
                lane: .business
            )
            let reply: HelperReply
            do {
                try await client.waitReady(deadline: deadline)
                try await requireExactRuntime(client, deadline: deadline)
                reply = try await call(
                    client,
                    operation: operation(for: arguments.operation),
                    payload: input,
                    deadline: deadline
                )
                await client.close()
            } catch {
                await client.close()
                throw error
            }
            FileHandle.standardOutput.write(try JSONEncoder().encode(reply))
            FileHandle.standardOutput.write(Data("\n".utf8))
            if !reply.ok { exit(3) }
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(error is BridgeError ? 2 : 1)
        }
    }

    // The bridge and the runtime ship as one signed generation. A bridge that
    // enqueues into a runtime whose helper app refuses to consume delivers
    // nothing, so the skew is refused on this session before anything is sent.
    private static func requireExactRuntime(_ client: SocketClient, deadline: Date) async throws {
        let health: RuntimeHealth = try await call(
            client, operation: hostRuntimeHealthOperation, payload: Data(), deadline: deadline
        )
        guard health.runtimeBuild == StampedBuild.current else {
            throw BridgeError.runtimeSkew(expected: StampedBuild.current, found: health.runtimeBuild)
        }
    }

    private static func call<Reply: Decodable>(
        _ client: SocketClient,
        operation: String,
        payload: Data,
        deadline: Date
    ) async throws -> Reply {
        let terminal = try await client.call(operation: operation, payload: payload, deadline: deadline)
        guard !terminal.rejected else {
            throw BridgeError.rejected(terminal.reason ?? "unspecified")
        }
        if let error = terminal.error { throw BridgeError.remote(error) }
        guard let body = terminal.payload else { throw BridgeError.missingPayload }
        return try JSONDecoder().decode(Reply.self, from: businessReplyBody(body, operation: operation))
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

    private static func operation(for command: String) -> String {
        command == "ping" ? helperPingOperation : helperNotifyOperation
    }
}
