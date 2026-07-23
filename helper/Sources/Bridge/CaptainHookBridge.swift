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
            "usage: capt-hook-helper-client ping|notify"
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
            let container = try AppGroupContainer(identifier: HelperPaths.appGroupIdentifier)
            let socket = try container.socketPath(leaf: AppGroupContainer.SocketLeaf("helper.sock"))
            let client = try await SocketClient(
                path: socket,
                wireBuild: helperWireBuild,
                role: helperClientRole,
                configuration: .init(maximumFrameBytes: 64 * 1024)
            )
            let terminal: SocketTerminal
            do {
                terminal = try await client.call(
                    operation: operation(for: arguments.operation),
                    payload: input,
                    deadline: Date().addingTimeInterval(5)
                )
                await client.close()
            } catch {
                await client.close()
                throw error
            }
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
