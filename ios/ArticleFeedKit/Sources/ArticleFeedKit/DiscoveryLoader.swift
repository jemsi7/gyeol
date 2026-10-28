import Combine
import Foundation

public protocol DiscoveryAppAttestManaging: Sendable {
    func ensureRegistered(device: DeviceInfo) async throws -> String
    func makeAssertion(payload: DiscoveryPayload) async throws -> AssertionEnvelope
    func resetRegistration() async
}

extension AppAttestManager: DiscoveryAppAttestManaging {}

public enum DiscoveryLoadState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case empty
    case failed(message: String, retryAt: Date?)
}

@MainActor
public final class DiscoveryLoader: ObservableObject {
    @Published public private(set) var response: DiscoveryResponse?
    @Published public private(set) var state: DiscoveryLoadState = .idle
    public var hasMoreEditorialPages: Bool {
        loadedCollection == .editorial && collectionNextCursor != nil
    }
    public var hasMoreWeeklyInsightPages: Bool {
        loadedCollection == .weeklyInsight && collectionNextCursor != nil
    }

    private let device: DeviceInfo
    private let client: any DiscoveryAPIClientProtocol
    private let appAttest: any DiscoveryAppAttestManaging
    private var collectionNextCursor: String?
    private var loadedCollection: DiscoveryCollection?

    private static let registrationInvalidatingCodes: Set<String> = [
        "attested_key_not_active",
    ]

    public init(
        device: DeviceInfo,
        client: any DiscoveryAPIClientProtocol,
        appAttest: any DiscoveryAppAttestManaging,
        initialResponse: DiscoveryResponse? = nil
    ) {
        self.device = device
        self.client = client
        self.appAttest = appAttest
        let deduplicatedInitialResponse = initialResponse.map {
            Self.deduplicatingExactHeadlines(in: $0)
        }
        response = deduplicatedInitialResponse
        if let deduplicatedInitialResponse {
            state = deduplicatedInitialResponse.routes.isEmpty ? .empty : .loaded
        }
    }

    public func loadCatalog() async {
        resetCollectionPagination()
        await load(query: nil, collection: nil, cursor: nil, appending: false)
    }

    public func search(_ query: String) async {
        let normalized = query
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        guard !normalized.isEmpty else {
            await loadCatalog()
            return
        }
        resetCollectionPagination()
        await load(query: normalized, collection: nil, cursor: nil, appending: false)
    }

    public func loadEditorials() async {
        await loadCollection(.editorial)
    }

    public func loadNextEditorialPage() async {
        guard loadedCollection == .editorial else { return }
        await loadNextCollectionPage(.editorial)
    }

    public func loadWeeklyInsights() async {
        await loadCollection(.weeklyInsight)
    }

    public func loadNextWeeklyInsightPage() async {
        guard loadedCollection == .weeklyInsight else { return }
        await loadNextCollectionPage(.weeklyInsight)
    }

    public func findCollectionArticle(
        _ articleID: String,
        in collection: DiscoveryCollection
    ) async -> ArticleFeedItem? {
        let payload = DiscoveryPayload(
            device: device,
            query: nil,
            limit: 20,
            collection: collection,
            cursor: nil
        )
        do {
            _ = try await appAttest.ensureRegistered(device: device)
            let assertion = try await appAttest.makeAssertion(payload: payload)
            let incoming = try await client.loadDiscovery(payload: payload, auth: assertion)
            guard incoming.collection == collection else { return nil }
            return incoming.results
                .first { $0.article.articleID == articleID }?
                .article
        } catch let ArticleFeedError.server(code, _) {
            if Self.registrationInvalidatingCodes.contains(code.lowercased()) {
                await appAttest.resetRegistration()
            }
            return nil
        } catch {
            return nil
        }
    }

    private func loadCollection(_ collection: DiscoveryCollection) async {
        resetCollectionPagination()
        await load(query: nil, collection: collection, cursor: nil, appending: false)
    }

    private func loadNextCollectionPage(_ collection: DiscoveryCollection) async {
        guard state != .loading,
              loadedCollection == collection,
              let collectionNextCursor
        else { return }
        await load(
            query: nil,
            collection: collection,
            cursor: collectionNextCursor,
            appending: true
        )
    }

    private func resetCollectionPagination() {
        collectionNextCursor = nil
        loadedCollection = nil
    }

    private func load(
        query: String?,
        collection: DiscoveryCollection?,
        cursor: String?,
        appending: Bool
    ) async {
        guard state != .loading else { return }
        let previous = response
        state = .loading
        do {
            let payload = DiscoveryPayload(
                device: device,
                query: query,
                limit: collection == nil ? 12 : 20,
                collection: collection,
                cursor: cursor
            )
            _ = try await appAttest.ensureRegistered(device: device)
            let assertion = try await appAttest.makeAssertion(payload: payload)
            let incoming = try await client.loadDiscovery(payload: payload, auth: assertion)
            guard incoming.collection == collection else {
                throw ArticleFeedError.invalidResponse
            }
            let deduplicatedIncoming = Self.deduplicatingExactHeadlines(in: incoming)
            if appending,
               deduplicatedIncoming.collection == collection,
               let previous,
               previous.collection == collection {
                var seenArticleIDs = Set(previous.results.map(\.article.articleID))
                var seenHeadlines = Set(previous.results.map(\.article.map.headline))
                let appended = deduplicatedIncoming.results.filter {
                    guard seenArticleIDs.insert($0.article.articleID).inserted else {
                        return false
                    }
                    return seenHeadlines.insert($0.article.map.headline).inserted
                }
                response = DiscoveryResponse(
                    formatVersion: deduplicatedIncoming.formatVersion,
                    generatedAt: deduplicatedIncoming.generatedAt,
                    expiresAt: deduplicatedIncoming.expiresAt,
                    representativeRouteID: deduplicatedIncoming.representativeRouteID,
                    routes: deduplicatedIncoming.routes,
                    searchSuggestions: deduplicatedIncoming.searchSuggestions,
                    trendingSearchSuggestions: deduplicatedIncoming.trendingSearchSuggestions,
                    query: nil,
                    results: previous.results + appended,
                    cooldownSeconds: deduplicatedIncoming.cooldownSeconds,
                    collection: collection,
                    timelineStatus: deduplicatedIncoming.timelineStatus,
                    timelinesGeneratedAt: deduplicatedIncoming.timelinesGeneratedAt,
                    timelinesRefreshedAt: deduplicatedIncoming.timelinesRefreshedAt,
                    featuredTimelines: deduplicatedIncoming.featuredTimelines,
                    timelineResults: deduplicatedIncoming.timelineResults,
                    nextCursor: deduplicatedIncoming.nextCursor
                )
            } else {
                response = deduplicatedIncoming
            }
            if let collection {
                collectionNextCursor = incoming.nextCursor
                loadedCollection = collection
                state = response?.results.isEmpty == false ? .loaded : .empty
            } else {
                state = incoming.routes.isEmpty
                    && incoming.results.isEmpty
                    && incoming.featuredTimelines.isEmpty
                    ? .empty
                    : .loaded
            }
        } catch is CancellationError {
            response = previous
            state = previous == nil ? .idle : .loaded
        } catch let ArticleFeedError.server(code, retryAfter) {
            if Self.registrationInvalidatingCodes.contains(code.lowercased()) {
                await appAttest.resetRegistration()
            }
            response = previous
            state = .failed(
                message: code,
                retryAt: retryAfter.map { Date().addingTimeInterval($0) }
            )
        } catch let error as ArticleFeedError {
            response = previous
            state = .failed(message: String(describing: error), retryAt: nil)
        } catch {
            response = previous
            state = .failed(message: "unknown_error", retryAt: nil)
        }
    }

    private static func deduplicatingExactHeadlines(
        in response: DiscoveryResponse
    ) -> DiscoveryResponse {
        var seenHeadlines = Set<String>()
        let results = response.results.filter {
            seenHeadlines.insert($0.article.map.headline).inserted
        }
        guard results.count != response.results.count else {
            return response
        }
        return DiscoveryResponse(
            formatVersion: response.formatVersion,
            generatedAt: response.generatedAt,
            expiresAt: response.expiresAt,
            representativeRouteID: response.representativeRouteID,
            routes: response.routes,
            searchSuggestions: response.searchSuggestions,
            trendingSearchSuggestions: response.trendingSearchSuggestions,
            query: response.query,
            results: results,
            cooldownSeconds: response.cooldownSeconds,
            collection: response.collection,
            timelineStatus: response.timelineStatus,
            timelinesGeneratedAt: response.timelinesGeneratedAt,
            timelinesRefreshedAt: response.timelinesRefreshedAt,
            featuredTimelines: response.featuredTimelines,
            timelineResults: response.timelineResults,
            nextCursor: response.nextCursor
        )
    }
}
