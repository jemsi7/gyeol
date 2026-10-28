#if DEBUG
import ArticleFeedKit
import Foundation
import SwiftUI

enum LiveMapE2EConfigurationError: Error, Equatable {
    case missing(String)
    case invalid(String)
}

struct LiveMapE2EExpectation: Equatable, Sendable {
    let baseURL: URL
    let articleID: String
    let headline: String
    let sceneCount: Int
    let mapSHA256: String

    init(environment: [String: String]) throws {
        func required(_ key: String) throws -> String {
            guard let value = environment[key]?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !value.isEmpty else {
                throw LiveMapE2EConfigurationError.missing(key)
            }
            return value
        }

        let rawBaseURL = try required("LIVE_MAP_BASE_URL")
        guard let url = URL(string: rawBaseURL),
              url.scheme?.lowercased() == "http",
              url.host == "127.0.0.1",
              url.port != nil,
              url.user == nil,
              url.password == nil,
              url.query == nil,
              url.fragment == nil,
              url.path.isEmpty || url.path == "/" else {
            throw LiveMapE2EConfigurationError.invalid("LIVE_MAP_BASE_URL")
        }
        let articleID = try required("LIVE_MAP_EXPECTED_ARTICLE_ID")
        guard Self.isLowercaseHex(articleID, count: 64) else {
            throw LiveMapE2EConfigurationError.invalid("LIVE_MAP_EXPECTED_ARTICLE_ID")
        }
        let headline = try required("LIVE_MAP_EXPECTED_HEADLINE")
        let rawSceneCount = try required("LIVE_MAP_EXPECTED_SCENE_COUNT")
        guard let sceneCount = Int(rawSceneCount), (3...9).contains(sceneCount) else {
            throw LiveMapE2EConfigurationError.invalid("LIVE_MAP_EXPECTED_SCENE_COUNT")
        }
        let mapSHA256 = try required("LIVE_MAP_SHA256")
        guard Self.isLowercaseHex(mapSHA256, count: 64) else {
            throw LiveMapE2EConfigurationError.invalid("LIVE_MAP_SHA256")
        }
        self.baseURL = url
        self.articleID = articleID
        self.headline = headline
        self.sceneCount = sceneCount
        self.mapSHA256 = mapSHA256
    }

    func matches(_ items: [ArticleFeedItem]) -> Bool {
        guard items.count == 1, let item = items.first else { return false }
        let expectedSceneIDs = (1...sceneCount).map { String(format: "S-%02d", $0) }
        return item.articleID == articleID
            && item.map.formatVersion == 3
            && item.map.headline == headline
            && item.map.scenes.map(\.id) == expectedSceneIDs
            && item.map.editorial?.criticVerdict == "pass"
    }

    private static func isLowercaseHex(_ value: String, count: Int) -> Bool {
        let allowed = Set("0123456789abcdef")
        return value.count == count && value.allSatisfy(allowed.contains)
    }
}

final class LiveMapE2EAppAttestService: AppAttestServicing, @unchecked Sendable {
    let isSupported = true

    func generateKey() async throws -> String { "live-map-e2e-key-id" }

    func attestKey(_ keyID: String, clientDataHash: Data) async throws -> Data {
        Data("live-map-e2e-attestation-object".utf8)
    }

    func generateAssertion(_ keyID: String, clientDataHash: Data) async throws -> Data {
        Data("live-map-e2e-assertion-object".utf8)
    }
}

final class LiveMapE2EKeyStore: AppAttestKeyStoring, @unchecked Sendable {
    private var keyID: String?
    private var registered = false

    func loadKeyID() -> String? { keyID }
    func isRegistered() -> Bool { registered }
    func saveKeyID(_ keyID: String) { self.keyID = keyID }
    func markRegistered() { registered = true }
    func clear() { keyID = nil; registered = false }
}

@MainActor
final class LiveMapValidatingFeedLoader: ArticleFeedLoading {
    private let base: ArticleFeedLoader
    private let expectation: LiveMapE2EExpectation
    private(set) var items: [ArticleFeedItem] = []
    private(set) var state: ArticleFeedLoadState = .idle
    var latestItems: [ArticleFeedItem] { base.latestItems }
    var latestState: ArticleFeedLoadState { base.latestState }
    var hasMorePages: Bool { base.hasMorePages }

    init(base: ArticleFeedLoader, expectation: LiveMapE2EExpectation) {
        self.base = base
        self.expectation = expectation
    }

    func loadInitial() async {
        await base.loadInitial()
        acceptBaseResult()
    }

    func loadRandom(excludingArticleIDs: [String]) async {
        await base.loadRandom(excludingArticleIDs: excludingArticleIDs)
        acceptBaseResult()
    }

    func loadLatest(excludingArticleIDs: [String]) async {
        await base.loadLatest(excludingArticleIDs: excludingArticleIDs)
    }

    private func acceptBaseResult() {
        guard base.state == .loaded else {
            items = base.items
            state = base.state
            return
        }
        guard expectation.matches(base.items) else {
            items = []
            state = .failed(message: "live_map_e2e_identity_mismatch", retryAt: nil)
            return
        }
        items = base.items
        state = .loaded
    }

    func loadNextBatch() async {
        await base.loadNextBatch()
        items = base.items
        state = base.state
    }
}

enum LiveMapE2EEnvironment {
    static var isEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains("-live-map-e2e")
    }

    @MainActor
    static func make(environment: [String: String]) throws -> AppEnvironment {
        let expectation = try LiveMapE2EExpectation(environment: environment)
        let client = try ArticleAPIClient(baseURL: expectation.baseURL)
        let appAttest = AppAttestManager(
            service: LiveMapE2EAppAttestService(),
            keyStore: LiveMapE2EKeyStore(),
            transport: client
        )
        let baseLoader = ArticleFeedLoader(
            device: DeviceInfoProvider.current(namespace: "TextShortsApp.LiveMapE2E"),
            client: client,
            appAttest: appAttest
        )
        return AppEnvironment(
            loader: LiveMapValidatingFeedLoader(
                base: baseLoader,
                expectation: expectation
            ),
            persistence: MemoryAppPersistence(),
            mediaCache: ArticleMediaCache.shared,
            connectivity: NoopConnectivityMonitor()
        )
    }
}

struct LiveMapE2ESentinelView: View {
    let article: PrototypeArticle
    let sceneIndex: Int

    var body: some View {
        VStack(spacing: 0) {
            sentinel(article.id, identifier: "LIVE-MAP-ARTICLE-ID")
            sentinel(article.headline, identifier: "LIVE-MAP-HEADLINE")
            sentinel(
                "\(sceneIndex + 1) / \(article.scenes.count)",
                identifier: "LIVE-MAP-SCENE-N-of-M"
            )
        }
        .frame(width: 2, height: 2)
        .clipped()
        .opacity(0.01)
        .allowsHitTesting(false)
    }

    private func sentinel(_ value: String, identifier: String) -> some View {
        Text(value)
            .font(.system(size: 1))
            .accessibilityIdentifier(identifier)
            .accessibilityLabel(value)
    }
}
#endif
