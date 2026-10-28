import Foundation
import Network

enum ConnectivityState: String, Equatable, Sendable {
    case unknown
    case online
    case offline
}

protocol ConnectivityMonitoring: Sendable {
    func updates() -> AsyncStream<ConnectivityState>
}

struct NetworkConnectivityMonitor: ConnectivityMonitoring {
    func updates() -> AsyncStream<ConnectivityState> {
        AsyncStream<ConnectivityState>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            let monitor = NWPathMonitor()
            let queue = DispatchQueue(label: "TextShortsApp.connectivity")
            monitor.pathUpdateHandler = { path in
                continuation.yield(path.status == .satisfied ? .online : .offline)
            }
            continuation.onTermination = { _ in monitor.cancel() }
            monitor.start(queue: queue)
        }
    }
}

struct NoopConnectivityMonitor: ConnectivityMonitoring {
    func updates() -> AsyncStream<ConnectivityState> {
        AsyncStream<ConnectivityState>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            continuation.finish()
        }
    }
}

struct StubConnectivityMonitor: ConnectivityMonitoring {
    let states: [ConnectivityState]

    func updates() -> AsyncStream<ConnectivityState> {
        AsyncStream<ConnectivityState>(bufferingPolicy: .unbounded) { continuation in
            states.forEach { _ = continuation.yield($0) }
            continuation.finish()
        }
    }
}
