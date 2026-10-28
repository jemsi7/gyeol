import Combine
import Foundation

public protocol AppAttestManaging: Sendable {
    func ensureRegistered(device: DeviceInfo) async throws -> String
    func makeAssertion(payload: FeedPayload) async throws -> AssertionEnvelope
    func resetRegistration() async
}

extension AppAttestManager: AppAttestManaging {}

public enum ArticleFeedLoadState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case empty
    case failed(message: String, retryAt: Date?)
}

@MainActor
public final class ArticleFeedLoader: ObservableObject {
    @Published public private(set) var items: [ArticleFeedItem] = []
    @Published public private(set) var state: ArticleFeedLoadState = .idle
    @Published public private(set) var latestItems: [ArticleFeedItem] = []
    @Published public private(set) var latestState: ArticleFeedLoadState = .idle
    public var hasMorePages: Bool { nextCursor != nil }

    private let device: DeviceInfo
    private let client: any ArticleAPIClientProtocol
    private let appAttest: any AppAttestManaging
    private var nextCursor: String?
    private var hasLoadedInitialPage = false

    private static let registrationInvalidatingCodes: Set<String> = [
        "attested_key_not_active",
    ]

    public init(
        device: DeviceInfo,
        client: any ArticleAPIClientProtocol,
        appAttest: any AppAttestManaging
    ) {
        self.device = device
        self.client = client
        self.appAttest = appAttest
    }

    public func loadInitial() async {
        guard state != .loading else { return }
        await load(cursor: nil, replacing: true)
    }

    public func loadNextPage() async {
        guard state != .loading, hasLoadedInitialPage, let nextCursor else { return }
        await load(cursor: nextCursor, replacing: false)
    }

    public func loadNextBatch() async {
        guard state != .loading else { return }
        guard hasLoadedInitialPage else {
            await loadInitial()
            return
        }
        guard let nextCursor else {
            state = .empty
            return
        }
        await load(cursor: nextCursor, replacing: true)
    }

    public func loadRandom(excludingArticleIDs: [String]) async {
        guard state != .loading else { return }
        let payload = FeedPayload(
            device: device,
            limit: 5,
            cursor: nil,
            mode: .random,
            excludeArticleIDs: excludingArticleIDs
        )
        await load(
            payload: payload,
            replacing: true,
            updatesPagination: false
        )
    }

    public func loadLatest(excludingArticleIDs: [String]) async {
        guard latestState != .loading else { return }
        let payload = FeedPayload(
            device: device,
            limit: 5,
            cursor: nil,
            mode: .latest,
            excludeArticleIDs: excludingArticleIDs
        )
        latestState = .loading
        do {
            _ = try await appAttest.ensureRegistered(device: device)
            let assertion = try await appAttest.makeAssertion(payload: payload)
            let response = try await client.loadFeed(payload: payload, auth: assertion)
            latestItems = response.items
            latestState = latestItems.isEmpty ? .empty : .loaded
        } catch is CancellationError {
            latestItems = []
            latestState = .idle
        } catch let ArticleFeedError.server(code, retryAfter) {
            if Self.registrationInvalidatingCodes.contains(code.lowercased()) {
                await appAttest.resetRegistration()
            }
            latestItems = []
            latestState = .failed(
                message: code,
                retryAt: retryAfter.map { Date().addingTimeInterval($0) }
            )
        } catch let error as ArticleFeedError {
            latestItems = []
            latestState = .failed(message: String(describing: error), retryAt: nil)
        } catch {
            latestItems = []
            latestState = .failed(message: "unknown_error", retryAt: nil)
        }
    }

    private func load(cursor: String?, replacing: Bool) async {
        let payload = FeedPayload(device: device, limit: 5, cursor: cursor)
        await load(
            payload: payload,
            replacing: replacing,
            updatesPagination: true
        )
    }

    private func load(
        payload: FeedPayload,
        replacing: Bool,
        updatesPagination: Bool
    ) async {
        state = .loading
        do {
            _ = try await appAttest.ensureRegistered(device: device)
            let assertion = try await appAttest.makeAssertion(payload: payload)
            let response = try await client.loadFeed(payload: payload, auth: assertion)
            let incoming = response.items
            if replacing {
                items = incoming
            } else {
                let known = Set(items.map(\.articleID))
                items.append(contentsOf: incoming.filter { !known.contains($0.articleID) })
            }
            if updatesPagination {
                nextCursor = response.nextCursor
                hasLoadedInitialPage = true
            }
            state = items.isEmpty ? .empty : .loaded
        } catch is CancellationError {
            state = items.isEmpty ? .idle : .loaded
        } catch let ArticleFeedError.server(code, retryAfter) {
            if Self.registrationInvalidatingCodes.contains(code.lowercased()) {
                await appAttest.resetRegistration()
            }
            let retryAt = retryAfter.map { Date().addingTimeInterval($0) }
            state = .failed(message: code, retryAt: retryAt)
        } catch let error as ArticleFeedError {
            state = .failed(message: String(describing: error), retryAt: nil)
        } catch {
            state = .failed(message: "unknown_error", retryAt: nil)
        }
    }
}
