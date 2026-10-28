import Foundation
import XCTest
@testable import ArticleFeedKit

private final class FakeDiscoveryClient: DiscoveryAPIClientProtocol, @unchecked Sendable {
    var response: DiscoveryResponse?
    var error: Error?
    private(set) var payloads: [DiscoveryPayload] = []

    func loadDiscovery(
        payload: DiscoveryPayload,
        auth: AssertionEnvelope
    ) async throws -> DiscoveryResponse {
        payloads.append(payload)
        if let error { throw error }
        return try XCTUnwrap(response)
    }
}

private final class FakeDiscoveryAttest: DiscoveryAppAttestManaging, @unchecked Sendable {
    private(set) var registrations = 0
    private(set) var assertionPayloads: [DiscoveryPayload] = []
    private(set) var resets = 0

    func ensureRegistered(device: DeviceInfo) async throws -> String {
        registrations += 1
        return "apple-app-attest-key-id"
    }

    func makeAssertion(payload: DiscoveryPayload) async throws -> AssertionEnvelope {
        assertionPayloads.append(payload)
        return AssertionEnvelope(
            challengeID: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            keyID: "apple-app-attest-key-id",
            assertion: "YXNzZXJ0aW9uLW9iamVjdA",
            issuedAt: 1_700_000_000
        )
    }

    func resetRegistration() async {
        resets += 1
    }
}

@MainActor
final class DiscoveryLoaderTests: XCTestCase {
    private let device = DeviceInfo(
        installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
        vendorID: nil,
        platform: .ipados,
        model: "iPad14,5",
        osVersion: "26.0",
        appVersion: "1.0",
        buildNumber: "1",
        locale: "ko-KR"
    )

    private func articleCopy(
        of article: ArticleFeedItem,
        articleID: String,
        domain: String,
        headline: String? = nil
    ) throws -> ArticleFeedItem {
        let data = try APIJSON.makeEncoder().encode(article)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        object["article_id"] = articleID
        object["domain"] = domain
        if let headline {
            var map = try XCTUnwrap(object["map"] as? [String: Any])
            map["headline"] = headline
            object["map"] = map
        }
        return try APIJSON.makeDecoder().decode(
            ArticleFeedItem.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
    }

    func testInitialResponseKeepsFirstResultPerExactHeadline() throws {
        let fixture = try DiscoveryTestFixture.response()
        let first = fixture.results[0]
        let duplicate = try articleCopy(
            of: first.article,
            articleID: String(repeating: "9", count: 64),
            domain: "another-source.example"
        )
        let initial = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            trendingSearchSuggestions: ["도시 재생", "전력망 전환", "기후 적응"],
            query: "AI",
            results: [
                first,
                DiscoverySearchResult(depth: .deep, article: duplicate),
                fixture.results[1],
            ],
            cooldownSeconds: fixture.cooldownSeconds
        )

        let loader = DiscoveryLoader(
            device: device,
            client: FakeDiscoveryClient(),
            appAttest: FakeDiscoveryAttest(),
            initialResponse: initial
        )

        XCTAssertEqual(
            loader.response?.results.map(\.article.articleID),
            [first.article.articleID, fixture.results[1].article.articleID]
        )
        XCTAssertEqual(loader.response?.results.map(\.depth), [.quick, .deep])
        XCTAssertEqual(
            loader.response?.trendingSearchSuggestions,
            ["도시 재생", "전력망 전환", "기후 적응"]
        )
    }

    func testSearchKeepsHeadlinesThatDifferByWhitespace() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let first = fixture.results[0]
        let whitespaceVariant = try articleCopy(
            of: first.article,
            articleID: String(repeating: "8", count: 64),
            domain: "whitespace-source.example",
            headline: first.article.map.headline + " "
        )
        let client = FakeDiscoveryClient()
        client.response = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: "AI",
            results: [
                first,
                DiscoverySearchResult(depth: .quick, article: whitespaceVariant),
            ],
            cooldownSeconds: fixture.cooldownSeconds
        )
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.search("AI")

        XCTAssertEqual(
            loader.response?.results.map(\.article.map.headline),
            [first.article.map.headline, first.article.map.headline + " "]
        )
    }

    func testCollectionContinuationDropsPreviouslySeenAndSamePageExactHeadlines() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let first = fixture.results[0]
        let next = fixture.results[1]
        let repeatedPrevious = try articleCopy(
            of: first.article,
            articleID: String(repeating: "7", count: 64),
            domain: "repeat-a.example"
        )
        let repeatedPreviousAgain = try articleCopy(
            of: first.article,
            articleID: String(repeating: "6", count: 64),
            domain: "repeat-b.example"
        )
        let repeatedNext = try articleCopy(
            of: next.article,
            articleID: String(repeating: "5", count: 64),
            domain: "repeat-c.example"
        )
        let firstPage = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: [first],
            cooldownSeconds: fixture.cooldownSeconds,
            collection: .editorial,
            nextCursor: "page-2"
        )
        let secondPage = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: [
                DiscoverySearchResult(depth: .deep, article: repeatedPrevious),
                DiscoverySearchResult(depth: .quick, article: repeatedPreviousAgain),
                next,
                DiscoverySearchResult(depth: .quick, article: repeatedNext),
            ],
            cooldownSeconds: fixture.cooldownSeconds,
            collection: .editorial,
            nextCursor: nil
        )
        let client = FakeDiscoveryClient()
        client.response = firstPage
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadEditorials()
        client.response = secondPage
        await loader.loadNextEditorialPage()

        XCTAssertEqual(
            loader.response?.results.map(\.article.articleID),
            [first.article.articleID, next.article.articleID]
        )
        XCTAssertEqual(loader.response?.results.map(\.depth), [.quick, .deep])
        XCTAssertFalse(loader.hasMoreEditorialPages)
    }

    func testLoadCatalogPublishesRepresentativeCatalog() async throws {
        let client = FakeDiscoveryClient()
        client.response = try DiscoveryTestFixture.response()
        let attest = FakeDiscoveryAttest()
        let loader = DiscoveryLoader(device: device, client: client, appAttest: attest)

        await loader.loadCatalog()

        XCTAssertEqual(loader.state, .loaded)
        XCTAssertEqual(loader.response?.routes.count, 2)
        XCTAssertEqual(client.payloads.map(\.query), [nil])
        XCTAssertEqual(attest.registrations, 1)
    }

    func testSearchNormalizesWhitespaceAndReplacesResponse() async throws {
        let client = FakeDiscoveryClient()
        client.response = try DiscoveryTestFixture.response()
        let attest = FakeDiscoveryAttest()
        let loader = DiscoveryLoader(device: device, client: client, appAttest: attest)

        await loader.search("  전력   인프라  ")

        XCTAssertEqual(client.payloads.last?.query, "전력 인프라")
        XCTAssertEqual(loader.response?.results.map(\.depth), [.quick, .deep])
        XCTAssertEqual(loader.state, .loaded)
    }

    func testEditorialPagesAppendDeduplicateAndStopAtEnd() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let firstResults = fixture.results
        let first = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            trendingSearchSuggestions: ["첫 스냅샷", "전력망 전환", "기후 적응"],
            query: nil,
            results: firstResults,
            cooldownSeconds: 1,
            collection: .editorial,
            nextCursor: "cursor-page-2"
        )
        let second = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            trendingSearchSuggestions: ["다음 스냅샷", "반도체 공급망", "도시 재생"],
            query: nil,
            results: [
                firstResults[1],
                DiscoverySearchResult(
                    depth: .deep,
                    article: fixture.routes[0].items[1].article
                ),
            ],
            cooldownSeconds: 1,
            collection: .editorial,
            nextCursor: nil
        )
        let client = FakeDiscoveryClient()
        client.response = first
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadEditorials()
        XCTAssertTrue(loader.hasMoreEditorialPages)
        XCTAssertEqual(client.payloads.last?.collection, .editorial)
        XCTAssertEqual(client.payloads.last?.limit, 20)
        XCTAssertNil(client.payloads.last?.cursor)

        client.response = second
        await loader.loadNextEditorialPage()
        XCTAssertEqual(client.payloads.last?.cursor, "cursor-page-2")
        XCTAssertEqual(
            loader.response?.results.map(\.article.articleID),
            firstResults.map(\.article.articleID) + [
                fixture.routes[0].items[1].article.articleID
            ]
        )
        XCTAssertFalse(loader.hasMoreEditorialPages)
        XCTAssertEqual(
            loader.response?.trendingSearchSuggestions,
            ["다음 스냅샷", "반도체 공급망", "도시 재생"]
        )

        let requestCount = client.payloads.count
        await loader.loadNextEditorialPage()
        XCTAssertEqual(client.payloads.count, requestCount)
    }

    func testWeeklyInsightPagesUseDedicatedCollectionAppendAndStopAtEnd() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let firstResults = fixture.results
        let first = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: firstResults,
            cooldownSeconds: 1,
            collection: .weeklyInsight,
            nextCursor: "weekly-cursor-page-2"
        )
        let appendedArticle = fixture.routes[0].items[1].article
        let second = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: [
                firstResults[1],
                DiscoverySearchResult(depth: .deep, article: appendedArticle),
            ],
            cooldownSeconds: 1,
            collection: .weeklyInsight,
            nextCursor: nil
        )
        let client = FakeDiscoveryClient()
        client.response = first
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadWeeklyInsights()

        XCTAssertTrue(loader.hasMoreWeeklyInsightPages)
        XCTAssertFalse(loader.hasMoreEditorialPages)
        XCTAssertEqual(client.payloads.last?.collection, .weeklyInsight)
        XCTAssertEqual(client.payloads.last?.limit, 20)
        XCTAssertNil(client.payloads.last?.query)
        XCTAssertNil(client.payloads.last?.cursor)

        client.response = second
        await loader.loadNextWeeklyInsightPage()

        XCTAssertEqual(client.payloads.last?.cursor, "weekly-cursor-page-2")
        XCTAssertEqual(
            loader.response?.results.map(\.article.articleID),
            firstResults.map(\.article.articleID) + [appendedArticle.articleID]
        )
        XCTAssertFalse(loader.hasMoreWeeklyInsightPages)
        let requestCount = client.payloads.count
        await loader.loadNextWeeklyInsightPage()
        XCTAssertEqual(client.payloads.count, requestCount)
    }

    func testWeeklyInsightRejectsMismatchedCollectionResponse() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let client = FakeDiscoveryClient()
        client.response = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: fixture.results,
            cooldownSeconds: 1,
            collection: .editorial,
            nextCursor: nil
        )
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadWeeklyInsights()

        XCTAssertNil(loader.response)
        XCTAssertFalse(loader.hasMoreWeeklyInsightPages)
        guard case .failed(message: "invalidResponse", retryAt: nil) = loader.state else {
            return XCTFail("Expected mismatched collection failure")
        }
    }

    func testEditorialContinuationFailurePreservesAccumulatedPageAndCursor() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let first = DiscoveryResponse(
            formatVersion: fixture.formatVersion,
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: fixture.results,
            cooldownSeconds: 1,
            collection: .editorial,
            nextCursor: "cursor-page-2"
        )
        let client = FakeDiscoveryClient()
        client.response = first
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )
        await loader.loadEditorials()
        client.error = ArticleFeedError.server(code: "discovery_unavailable", retryAfter: nil)

        await loader.loadNextEditorialPage()

        XCTAssertEqual(loader.response, first)
        XCTAssertTrue(loader.hasMoreEditorialPages)
        guard case .failed(message: "discovery_unavailable", retryAt: nil) = loader.state else {
            return XCTFail("Expected preserved continuation failure")
        }
    }

    func testCooldownPreservesPreviousCatalogAndExposesRetryDate() async throws {
        let previous = try DiscoveryTestFixture.response()
        let client = FakeDiscoveryClient()
        client.error = ArticleFeedError.server(code: "discovery_cooldown", retryAfter: 1.25)
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest(),
            initialResponse: previous
        )

        await loader.search("전력")

        XCTAssertEqual(loader.response, previous)
        guard case let .failed(message, retryAt) = loader.state else {
            return XCTFail("Expected cooldown failure")
        }
        XCTAssertEqual(message, "discovery_cooldown")
        XCTAssertNotNil(retryAt)
    }

    func testInvalidRegistrationResetsAndPreservesCatalog() async throws {
        let previous = try DiscoveryTestFixture.response()
        let client = FakeDiscoveryClient()
        client.error = ArticleFeedError.server(
            code: "attested_key_not_active",
            retryAfter: nil
        )
        let attest = FakeDiscoveryAttest()
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: attest,
            initialResponse: previous
        )

        await loader.loadCatalog()

        XCTAssertEqual(attest.resets, 1)
        XCTAssertEqual(loader.response, previous)
    }

    func testExpiredChallengePreservesRegistrationAndCatalog() async throws {
        let previous = try DiscoveryTestFixture.response()
        let client = FakeDiscoveryClient()
        client.error = ArticleFeedError.server(
            code: "invalid_or_expired_challenge",
            retryAfter: nil
        )
        let attest = FakeDiscoveryAttest()
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: attest,
            initialResponse: previous
        )

        await loader.loadCatalog()

        XCTAssertEqual(attest.resets, 0)
        XCTAssertEqual(loader.response, previous)
    }

    func testCancellationReturnsToIdleWithoutDroppingState() async {
        let client = FakeDiscoveryClient()
        client.error = CancellationError()
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadCatalog()

        XCTAssertEqual(loader.state, .idle)
        XCTAssertNil(loader.response)
    }

    func testGenericFailurePreservesExistingCatalog() async throws {
        let previous = try DiscoveryTestFixture.response()
        let client = FakeDiscoveryClient()
        client.error = ArticleFeedError.decoding
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest(),
            initialResponse: previous
        )

        await loader.loadCatalog()

        XCTAssertEqual(loader.response, previous)
        guard case let .failed(message, _) = loader.state else {
            return XCTFail("Expected failure")
        }
        XCTAssertEqual(message, "decoding")
    }

    func testCatalogMergePreservesTimelinesAndSearchDoesNotInventThem() async throws {
        let fixture = try DiscoveryTestFixture.response()
        let articles = fixture.routes[0].items.map(\.article)
        let ids = (1...4).map {
            UUID(uuidString: String(format: "40000000-0000-0000-0000-%012d", $0))!
        }
        let dates = (0..<4).map { Date(timeIntervalSince1970: 1_700_000_000 + Double($0 * 60)) }
        let nodes = [
            FeaturedTimelineNode(
                id: ids[0], lane: .main, anchorNodeID: nil, updateKind: .existing,
                publishedAt: dates[0], dateSource: .provider, article: articles[0]
            ),
            FeaturedTimelineNode(
                id: ids[1], lane: .main, anchorNodeID: nil, updateKind: .existing,
                publishedAt: dates[1], dateSource: .provider, article: articles[1]
            ),
            FeaturedTimelineNode(
                id: ids[2], lane: .main, anchorNodeID: nil, updateKind: .inserted,
                publishedAt: dates[2], dateSource: .provider, article: articles[2]
            ),
            FeaturedTimelineNode(
                id: ids[3], lane: .branch, anchorNodeID: ids[1], updateKind: .occurred,
                publishedAt: dates[3], dateSource: .firstSeen, article: articles[3]
            ),
        ]
        let timeline = FeaturedTimeline(
            id: UUID(uuidString: "50000000-0000-0000-0000-000000000001")!,
            rank: 1,
            stableKey: "stable-1",
            title: "AI 인프라 변화의 주요 흐름",
            summary: "수요와 전력, 공급망 변화가 서로 이어진 사건을 시간 순서대로 정리한 타임라인입니다.",
            topic: "AI 인프라",
            startedAt: dates[0],
            endedAt: dates[3],
            nodes: nodes
        )
        let catalog = DiscoveryResponse(
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: nil,
            results: [],
            cooldownSeconds: 0,
            timelineStatus: .fresh,
            timelinesGeneratedAt: dates[0],
            timelinesRefreshedAt: dates[3],
            featuredTimelines: [timeline]
        )
        let client = FakeDiscoveryClient()
        client.response = catalog
        let loader = DiscoveryLoader(
            device: device,
            client: client,
            appAttest: FakeDiscoveryAttest()
        )

        await loader.loadCatalog()
        XCTAssertEqual(loader.response?.featuredTimelines, [timeline])
        XCTAssertEqual(loader.state, .loaded)

        client.response = DiscoveryResponse(
            generatedAt: fixture.generatedAt,
            expiresAt: fixture.expiresAt,
            representativeRouteID: fixture.representativeRouteID,
            routes: fixture.routes,
            searchSuggestions: fixture.searchSuggestions,
            query: "AI",
            results: fixture.results,
            cooldownSeconds: 0
        )
        await loader.search("AI")
        XCTAssertTrue(loader.response?.featuredTimelines.isEmpty == true)
        XCTAssertEqual(loader.response?.timelineStatus, .unavailable)
    }
}
