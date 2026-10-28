import ArticleFeedKit
import Foundation

@MainActor
protocol ArticleFeedLoading: AnyObject {
    var items: [ArticleFeedItem] { get }
    var state: ArticleFeedLoadState { get }
    var latestItems: [ArticleFeedItem] { get }
    var latestState: ArticleFeedLoadState { get }
    var hasMorePages: Bool { get }
    func loadInitial() async
    func loadLatest(excludingArticleIDs: [String]) async
    func loadRandom(excludingArticleIDs: [String]) async
    func loadNextBatch() async
}

extension ArticleFeedLoader: ArticleFeedLoading {}

@MainActor
protocol DiscoveryLoading: AnyObject {
    var response: DiscoveryResponse? { get }
    var state: DiscoveryLoadState { get }
    var hasMoreEditorialPages: Bool { get }
    var hasMoreWeeklyInsightPages: Bool { get }
    func loadCatalog() async
    func search(_ query: String) async
    func loadEditorials() async
    func loadNextEditorialPage() async
    func loadWeeklyInsights() async
    func loadNextWeeklyInsightPage() async
    func findCollectionArticle(
        _ articleID: String,
        in collection: DiscoveryCollection
    ) async -> ArticleFeedItem?
}

extension DiscoveryLoader: DiscoveryLoading {}

@MainActor
protocol ArticleViewRecording: AnyObject {
    func record(_ event: ArticleViewEvent) async throws -> ArticleViewResponse
}

protocol ArticleViewAppAttestManaging: Sendable {
    func makeAssertion(payload: ArticleViewPayload) async throws -> AssertionEnvelope
}

extension AppAttestManager: ArticleViewAppAttestManaging {}

@MainActor
final class LiveArticleViewService: ArticleViewRecording {
    private let client: any ArticleViewAPIClientProtocol
    private let appAttest: any ArticleViewAppAttestManaging
    private let device: DeviceInfo

    init(
        client: any ArticleViewAPIClientProtocol,
        appAttest: any ArticleViewAppAttestManaging,
        device: DeviceInfo
    ) {
        self.client = client
        self.appAttest = appAttest
        self.device = device
    }

    func record(_ event: ArticleViewEvent) async throws -> ArticleViewResponse {
        guard event.isValid else { throw ArticleFeedError.invalidResponse }
        let payload = ArticleViewPayload(event: event, device: device)
        let assertion = try await appAttest.makeAssertion(payload: payload)
        let response = try await client.recordArticleView(
            payload: payload,
            auth: assertion
        )
        guard response.articleID == event.articleID, response.viewCount >= 0 else {
            throw ArticleFeedError.invalidResponse
        }
        return response
    }
}

@MainActor
final class MemoryArticleViewService: ArticleViewRecording {
    var result: Result<ArticleViewResponse, Error>?
    var handler: ((ArticleViewEvent) async throws -> ArticleViewResponse)?
    private(set) var requestedEvents: [ArticleViewEvent] = []

    init(result: Result<ArticleViewResponse, Error>? = nil) {
        self.result = result
    }

    func record(_ event: ArticleViewEvent) async throws -> ArticleViewResponse {
        requestedEvents.append(event)
        if let handler {
            return try await handler(event)
        }
        if let result {
            return try result.get()
        }
        return ArticleViewResponse(
            articleID: event.articleID,
            counted: true,
            viewCount: requestedEvents.count
        )
    }
}

@MainActor
final class UnavailableArticleViewService: ArticleViewRecording {
    func record(_ event: ArticleViewEvent) async throws -> ArticleViewResponse {
        throw ArticleFeedError.server(
            code: "article_view_unavailable",
            retryAfter: nil
        )
    }
}

@MainActor
final class UnavailableDiscoveryLoader: DiscoveryLoading {
    private(set) var response: DiscoveryResponse?
    private(set) var state: DiscoveryLoadState
    let hasMoreEditorialPages = false
    let hasMoreWeeklyInsightPages = false

    init(code: String, response: DiscoveryResponse? = nil) {
        self.response = response
        state = .failed(message: code, retryAt: nil)
    }

    func loadCatalog() async {}
    func search(_ query: String) async {}
    func loadEditorials() async {}
    func loadNextEditorialPage() async {}
    func loadWeeklyInsights() async {}
    func loadNextWeeklyInsightPage() async {}
    func findCollectionArticle(
        _ articleID: String,
        in collection: DiscoveryCollection
    ) async -> ArticleFeedItem? { nil }
}

@MainActor
final class StubDiscoveryLoader: DiscoveryLoading {
    struct Lookup: Equatable {
        let articleID: String
        let collection: DiscoveryCollection
    }

    var response: DiscoveryResponse?
    var state: DiscoveryLoadState
    private(set) var collectionLookups: [Lookup] = []
    private(set) var catalogLoadCount = 0
    private(set) var searches: [String] = []
    private(set) var editorialLoadCount = 0
    private(set) var editorialContinuationCount = 0
    private(set) var weeklyInsightLoadCount = 0
    private(set) var weeklyInsightContinuationCount = 0
    var catalogResult: (DiscoveryResponse?, DiscoveryLoadState)?
    var searchResult: (DiscoveryResponse?, DiscoveryLoadState)?
    var editorialResult: (DiscoveryResponse?, DiscoveryLoadState)?
    var editorialContinuationResults: [(DiscoveryResponse?, DiscoveryLoadState)] = []
    var weeklyInsightResult: (DiscoveryResponse?, DiscoveryLoadState)?
    var weeklyInsightContinuationResults: [(DiscoveryResponse?, DiscoveryLoadState)] = []
    var hasMoreEditorialPages: Bool {
        response?.collection == .editorial && response?.nextCursor != nil
    }
    var hasMoreWeeklyInsightPages: Bool {
        response?.collection == .weeklyInsight && response?.nextCursor != nil
    }

    @MainActor
    init(
        response: DiscoveryResponse? = nil,
        state: DiscoveryLoadState = .idle
    ) {
        self.response = response
        self.state = state
    }

    func loadCatalog() async {
        catalogLoadCount += 1
        if let catalogResult {
            response = catalogResult.0
            state = catalogResult.1
        }
    }

    func search(_ query: String) async {
        searches.append(query)
        if let searchResult {
            response = searchResult.0.map { result in
                DiscoveryResponse(
                    formatVersion: result.formatVersion,
                    generatedAt: result.generatedAt,
                    expiresAt: result.expiresAt,
                    representativeRouteID: result.representativeRouteID,
                    routes: result.routes,
                    searchSuggestions: result.searchSuggestions,
                    trendingSearchSuggestions: result.trendingSearchSuggestions,
                    query: query,
                    results: result.results,
                    cooldownSeconds: result.cooldownSeconds,
                    collection: result.collection,
                    timelineStatus: result.timelineStatus,
                    timelinesGeneratedAt: result.timelinesGeneratedAt,
                    timelinesRefreshedAt: result.timelinesRefreshedAt,
                    featuredTimelines: result.featuredTimelines,
                    timelineResults: result.timelineResults,
                    nextCursor: result.nextCursor
                )
            }
            state = searchResult.1
        }
    }

    func loadEditorials() async {
        editorialLoadCount += 1
        if let editorialResult {
            response = editorialResult.0
            state = editorialResult.1
        }
    }

    func findCollectionArticle(
        _ articleID: String,
        in collection: DiscoveryCollection
    ) async -> ArticleFeedItem? {
        collectionLookups.append(Lookup(articleID: articleID, collection: collection))
        let source = collection == .editorial ? editorialResult : weeklyInsightResult
        return source?.0?.results
            .first { $0.article.articleID == articleID }?
            .article
    }

    func loadNextEditorialPage() async {
        guard hasMoreEditorialPages else { return }
        editorialContinuationCount += 1
        if !editorialContinuationResults.isEmpty {
            let result = editorialContinuationResults.removeFirst()
            response = result.0
            state = result.1
        }
    }

    func loadWeeklyInsights() async {
        weeklyInsightLoadCount += 1
        if let weeklyInsightResult {
            response = weeklyInsightResult.0
            state = weeklyInsightResult.1
        }
    }

    func loadNextWeeklyInsightPage() async {
        guard hasMoreWeeklyInsightPages else { return }
        weeklyInsightContinuationCount += 1
        if !weeklyInsightContinuationResults.isEmpty {
            let result = weeklyInsightContinuationResults.removeFirst()
            response = result.0
            state = result.1
        }
    }
}

@MainActor
final class UnavailableArticleFeedLoader: ArticleFeedLoading {
    private(set) var items: [ArticleFeedItem] = []
    private(set) var state: ArticleFeedLoadState
    private(set) var latestItems: [ArticleFeedItem] = []
    private(set) var latestState: ArticleFeedLoadState
    let hasMorePages = false

    init(code: String) {
        state = .failed(message: code, retryAt: nil)
        latestState = .failed(message: code, retryAt: nil)
    }

    func loadInitial() async {}
    func loadLatest(excludingArticleIDs: [String]) async {}
    func loadRandom(excludingArticleIDs: [String]) async {}
    func loadNextBatch() async {}
}

struct StubFeedPage {
    let items: [ArticleFeedItem]
    let state: ArticleFeedLoadState
    let hasMorePages: Bool
}

@MainActor
final class StubArticleFeedLoader: ArticleFeedLoading {
    var items: [ArticleFeedItem]
    var state: ArticleFeedLoadState
    var latestItems: [ArticleFeedItem]
    var latestState: ArticleFeedLoadState
    var hasMorePages: Bool
    private(set) var loadCount = 0
    private(set) var latestLoadCount = 0
    private(set) var latestExclusions: [[String]] = []
    private(set) var randomLoadCount = 0
    private(set) var randomExclusions: [[String]] = []
    private(set) var nextBatchLoadCount = 0
    private var randomResults: [StubFeedPage]
    private var latestResults: [StubFeedPage]
    private var nextBatchResults: [StubFeedPage]
    private let initialResult: StubFeedPage?
    private let nextBatchDelayNanoseconds: UInt64

    init(
        items: [ArticleFeedItem] = [],
        state: ArticleFeedLoadState = .idle,
        latestItems: [ArticleFeedItem] = [],
        latestState: ArticleFeedLoadState = .idle,
        hasMorePages: Bool = false,
        initialResult: StubFeedPage? = nil,
        latestResults: [StubFeedPage] = [],
        randomResults: [StubFeedPage] = [],
        nextBatchResults: [StubFeedPage] = [],
        nextBatchDelayNanoseconds: UInt64 = 0
    ) {
        self.items = items
        self.state = state
        self.latestItems = latestItems
        self.latestState = latestState
        self.hasMorePages = hasMorePages
        self.initialResult = initialResult
        self.latestResults = latestResults
        self.randomResults = randomResults
        self.nextBatchResults = nextBatchResults
        self.nextBatchDelayNanoseconds = nextBatchDelayNanoseconds
    }

    func loadInitial() async {
        loadCount += 1
        if let initialResult {
            items = initialResult.items
            state = initialResult.state
            hasMorePages = initialResult.hasMorePages
        }
    }
    func loadLatest(excludingArticleIDs: [String]) async {
        latestLoadCount += 1
        latestExclusions.append(excludingArticleIDs)
        if !latestResults.isEmpty {
            let result = latestResults.removeFirst()
            latestItems = result.items
            latestState = result.state
        }
    }
    func loadRandom(excludingArticleIDs: [String]) async {
        loadCount += 1
        randomLoadCount += 1
        randomExclusions.append(excludingArticleIDs)
        if !randomResults.isEmpty {
            let result = randomResults.removeFirst()
            items = result.items
            state = result.state
        }
    }
    func loadNextBatch() async {
        loadCount += 1
        nextBatchLoadCount += 1
        if nextBatchDelayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: nextBatchDelayNanoseconds)
        }
        if !nextBatchResults.isEmpty {
            let result = nextBatchResults.removeFirst()
            items = result.items
            state = result.state
            hasMorePages = result.hasMorePages
        }
    }
}

struct AppEnvironment {
    let loader: any ArticleFeedLoading
    let discoveryLoader: any DiscoveryLoading
    let persistence: any AppPersisting
    let mediaCache: any ArticleMediaCaching
    let connectivity: any ConnectivityMonitoring
    let accountService: any AccountServicing
    let editorialPushService: any EditorialPushServicing
    let privacyConsentService: any PrivacyConsentServicing
    let articleViewService: any ArticleViewRecording

    @MainActor
    init(
        loader: any ArticleFeedLoading,
        discoveryLoader: (any DiscoveryLoading)? = nil,
        persistence: any AppPersisting,
        mediaCache: any ArticleMediaCaching,
        connectivity: any ConnectivityMonitoring,
        accountService: any AccountServicing = MemoryAccountService(),
        editorialPushService: (any EditorialPushServicing)? = nil,
        privacyConsentService: (any PrivacyConsentServicing)? = nil,
        articleViewService: (any ArticleViewRecording)? = nil
    ) {
        self.loader = loader
        self.discoveryLoader = discoveryLoader
            ?? UnavailableDiscoveryLoader(code: "discovery_unavailable")
        self.persistence = persistence
        self.mediaCache = mediaCache
        self.connectivity = connectivity
        self.accountService = accountService
        self.editorialPushService = editorialPushService ?? MemoryEditorialPushService()
        self.privacyConsentService = privacyConsentService ?? MemoryPrivacyConsentService()
        self.articleViewService = articleViewService ?? MemoryArticleViewService()
    }

    @MainActor
    static func make(
        arguments: [String] = ProcessInfo.processInfo.arguments,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        bundle: Bundle = .main
    ) -> AppEnvironment {
#if DEBUG
        let fixtureAccountService = debugAccountService(arguments: arguments)
        if arguments.contains("-live-map-e2e") {
            do {
                return try LiveMapE2EEnvironment.make(environment: environment)
            } catch {
                return AppEnvironment(
                    loader: UnavailableArticleFeedLoader(
                        code: "live_map_e2e_invalid_configuration"
                    ),
                    persistence: MemoryAppPersistence(),
                    mediaCache: MemoryArticleMediaCache(),
                    connectivity: NoopConnectivityMonitor()
                )
            }
        }
        if arguments.contains("-boundary-width-fixture") {
            return IntegrationFixture.environment(
                boundaryWidthRegression: true,
                accountService: fixtureAccountService
            )
        }
        if arguments.contains("-home-hub-fixture") {
            return IntegrationFixture.environment(
                homeHub: true,
                accountService: fixtureAccountService
            )
        }
        if arguments.contains("-home-media-layout-fixture") {
            return IntegrationFixture.environment(
                homeMediaLayoutRegression: true,
                accountService: fixtureAccountService
            )
        }
        if arguments.contains("-home-width-layout-fixture") {
            return IntegrationFixture.environment(
                homeWidthLayoutRegression: true,
                accountService: fixtureAccountService
            )
        }
        if let index = arguments.firstIndex(of: "-crypto-marketing-fixture"),
           index + 1 < arguments.count,
           let feature = Int(arguments[index + 1]),
           (1...3).contains(feature) {
            return IntegrationFixture.environment(
                cryptoMarketing: feature,
                accountService: fixtureAccountService
            )
        }
        if arguments.contains("-integration-fixture") {
            return IntegrationFixture.environment(
                accountService: fixtureAccountService,
                editorialPushService: debugEditorialPushService(arguments: arguments)
            )
        }
#endif
        return live(bundle: bundle)
    }

    @MainActor
    static func live(bundle: Bundle = .main) -> AppEnvironment {
        let persistence = FileAppPersistence.live()
        let mediaCache = ArticleMediaCache.shared
        let connectivity = NetworkConnectivityMonitor()
        do {
            let configuration = try AppConfiguration.live(bundle: bundle)
            let client = try ArticleFeedKit.ArticleAPIClient(
                baseURL: configuration.apiBaseURL
            )
            let device = DeviceInfoProvider.current(namespace: "TextShortsApp")
            let keyStore = UserDefaultsAppAttestKeyStore(namespace: "TextShortsApp")
            let appAttest = AppAttestManager(
                service: SystemAppAttestService(),
                keyStore: keyStore,
                transport: client
            )
            let loader = ArticleFeedLoader(device: device, client: client, appAttest: appAttest)
            let discoveryLoader = DiscoveryLoader(
                device: device,
                client: client,
                appAttest: appAttest
            )
            let credentials = KeychainAccountCredentialStore()
            let accountService = LiveAccountService(
                api: AccountAPIClient(baseURL: configuration.apiBaseURL),
                credentials: credentials,
                credentialState: SystemAppleCredentialStateChecker()
            )
            let editorialPushService = LiveEditorialPushService(
                client: client,
                appAttest: appAttest,
                credentials: credentials,
                device: device,
                environment: configuration.apnsEnvironment
            )
            let privacyConsentService = LivePrivacyConsentService(
                client: client,
                appAttest: appAttest,
                device: device
            )
            let articleViewService = LiveArticleViewService(
                client: client,
                appAttest: appAttest,
                device: device
            )
            return AppEnvironment(
                loader: loader,
                discoveryLoader: discoveryLoader,
                persistence: persistence,
                mediaCache: mediaCache,
                connectivity: connectivity,
                accountService: accountService,
                editorialPushService: editorialPushService,
                privacyConsentService: privacyConsentService,
                articleViewService: articleViewService
            )
        } catch {
            return AppEnvironment(
                loader: UnavailableArticleFeedLoader(code: "invalid_api_configuration"),
                persistence: persistence,
                mediaCache: mediaCache,
                connectivity: connectivity,
                privacyConsentService: UnavailablePrivacyConsentService(),
                articleViewService: UnavailableArticleViewService()
            )
        }
    }

#if DEBUG
    @MainActor
    private static func debugEditorialPushService(
        arguments: [String]
    ) -> (any EditorialPushServicing)? {
        switch value(after: "-push-fixture", in: arguments) {
        case "unregistered":
            return MemoryEditorialPushService(
                status: .notDetermined,
                preferences: .allEnabled,
                enableResult: .disabled
            )
        case "denied":
            return MemoryEditorialPushService(
                status: .denied,
                preferences: .allEnabled,
                enableResult: .denied
            )
        case "failed":
            return MemoryEditorialPushService(
                status: .authorized,
                preferences: .allEnabled,
                enableResult: .failed("registration_failed")
            )
        default:
            return nil
        }
    }

    private static func debugAccountService(arguments: [String]) -> any AccountServicing {
        guard value(after: "-account-fixture", in: arguments) == "signed-in" else {
            return MemoryAccountService()
        }
        let profile = AccountProfile(
            id: UUID(uuidString: "22222222-2222-2222-2222-222222222222")!,
            displayName: "읽는 사람",
            email: "contact@example.com"
        )
        let session = AccountSession(
            sessionToken: "signed-in-ui-fixture",
            expiresAt: Date(timeIntervalSince1970: 2_000_000_000),
            appleUserID: "apple-ui-fixture",
            account: profile
        )
        // Matches the rank-1 timeline in `IntegrationFixture`, so the settings
        // subscription row can be driven against a timeline Explore can open.
        let subscriptions: Set<String> =
            value(after: "-timeline-subscription-fixture", in: arguments) == "subscribed"
                ? ["10000000-0000-0000-0000-000000000001"]
                : []
        return MemoryAccountService(
            session: session,
            timelineSubscriptionKeys: subscriptions
        )
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag),
              arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
#endif
}
