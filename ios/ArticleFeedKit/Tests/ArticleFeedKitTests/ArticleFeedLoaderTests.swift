import Foundation
import XCTest
@testable import ArticleFeedKit

private final class FakeManager: AppAttestManaging, @unchecked Sendable {
    private(set) var resets = 0

    func ensureRegistered(device: DeviceInfo) async throws -> String {
        "apple-app-attest-key-id"
    }

    func makeAssertion(payload: FeedPayload) async throws -> AssertionEnvelope {
        AssertionEnvelope(
            challengeID: UUID(),
            keyID: "apple-app-attest-key-id",
            assertion: "YXNzZXJ0aW9u",
            issuedAt: 1_700_000_000
        )
    }

    func resetRegistration() async {
        resets += 1
    }
}

private final class FakeFeedClient: ArticleAPIClientProtocol, @unchecked Sendable {
    var responses: [FeedResponse]
    var error: Error?
    private(set) var payloads: [FeedPayload] = []

    init(responses: [FeedResponse]) { self.responses = responses }

    func loadFeed(payload: FeedPayload, auth: AssertionEnvelope) async throws -> FeedResponse {
        payloads.append(payload)
        if let error { throw error }
        return responses.removeFirst()
    }
}

@MainActor
final class ArticleFeedLoaderTests: XCTestCase {
    private func fixture() throws -> FeedResponse {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "feed_response", withExtension: "json")
        )
        return try APIJSON.makeDecoder().decode(
            FeedResponse.self,
            from: Data(contentsOf: url)
        )
    }

    private func device() -> DeviceInfo {
        DeviceInfo(
            installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
            vendorID: nil,
            platform: .ipados,
            model: "iPad14,5",
            osVersion: "26.0",
            appVersion: "1.0",
            buildNumber: "1",
            locale: "ko-KR"
        )
    }

    func testLoaderLoadsInitialPage() async throws {
        let client = FakeFeedClient(responses: [try fixture()])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )
        await loader.loadInitial()
        XCTAssertEqual(loader.items.count, 1)
        XCTAssertEqual(loader.state, .loaded)
        XCTAssertEqual(client.payloads.first?.limit, 5)
        XCTAssertNil(client.payloads.first?.cursor)
    }

    func testLoaderRequestsFiveAndReplacesWithCursorNextBatch() async throws {
        let fixture = try fixture()
        let initial = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: "cursor-page-2",
            cooldownSeconds: 1
        )
        let next = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: [],
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let client = FakeFeedClient(responses: [initial, next])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )

        await loader.loadInitial()
        XCTAssertTrue(loader.hasMorePages)
        await loader.loadNextBatch()

        XCTAssertEqual(client.payloads.map(\.limit), [5, 5])
        XCTAssertEqual(client.payloads.map(\.cursor), [nil, "cursor-page-2"])
        XCTAssertTrue(loader.items.isEmpty)
        XCTAssertEqual(loader.state, .empty)
        XCTAssertFalse(loader.hasMorePages)
    }

    func testLoaderExposesHasMorePagesAcrossInitialAndNextBatch() async throws {
        let fixture = try fixture()
        let initial = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: "cursor-page-2",
            cooldownSeconds: 1
        )
        let next = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let loader = ArticleFeedLoader(
            device: device(),
            client: FakeFeedClient(responses: [initial, next]),
            appAttest: FakeManager()
        )

        XCTAssertFalse(loader.hasMorePages)
        await loader.loadInitial()
        XCTAssertTrue(loader.hasMorePages)
        await loader.loadNextBatch()
        XCTAssertFalse(loader.hasMorePages)
    }

    func testRandomLoadSendsModeAndExclusionsWhilePreservingPaginationCursor() async throws {
        let fixture = try fixture()
        let initial = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: "cursor-page-2",
            cooldownSeconds: 1
        )
        let random = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let next = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: [],
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let client = FakeFeedClient(responses: [initial, random, next])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )

        await loader.loadInitial()
        await loader.loadRandom(excludingArticleIDs: ["a", "a", "b"])

        XCTAssertEqual(client.payloads[1].mode, .random)
        XCTAssertEqual(client.payloads[1].excludeArticleIDs, ["a", "b"])
        XCTAssertNil(client.payloads[1].cursor)
        XCTAssertTrue(loader.hasMorePages)

        await loader.loadNextPage()
        XCTAssertEqual(client.payloads.map(\.mode), [.recent, .random, .recent])
        XCTAssertEqual(client.payloads.map(\.cursor), [nil, nil, "cursor-page-2"])
    }

    func testRandomLoadEmptyAndFailurePreservePaginationCursor() async throws {
        let fixture = try fixture()
        let initial = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: "cursor-page-2",
            cooldownSeconds: 1
        )
        let empty = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: [],
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let client = FakeFeedClient(responses: [initial, empty])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )

        await loader.loadInitial()
        await loader.loadRandom(excludingArticleIDs: [])
        XCTAssertEqual(loader.state, .empty)
        XCTAssertTrue(loader.hasMorePages)

        client.error = ArticleFeedError.server(
            code: "network_unavailable",
            retryAfter: nil
        )
        await loader.loadRandom(excludingArticleIDs: [])
        XCTAssertTrue(loader.hasMorePages)
        guard case .failed(let message, _) = loader.state else {
            return XCTFail("Expected failure state")
        }
        XCTAssertEqual(message, "network_unavailable")
    }

    func testLatestLoadSendsExclusionsAndPreservesMainFeedStateAndCursor() async throws {
        let fixture = try fixture()
        let initial = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: "cursor-page-2",
            cooldownSeconds: 1
        )
        let latest = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: fixture.items,
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let next = FeedResponse(
            formatVersion: fixture.formatVersion,
            items: [],
            nextCursor: nil,
            cooldownSeconds: 1
        )
        let client = FakeFeedClient(responses: [initial, latest, next])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )

        await loader.loadInitial()
        await loader.loadLatest(excludingArticleIDs: ["a", "a", "b"])

        XCTAssertEqual(client.payloads[1].mode, .latest)
        XCTAssertEqual(client.payloads[1].excludeArticleIDs, ["a", "b"])
        XCTAssertNil(client.payloads[1].cursor)
        XCTAssertEqual(loader.latestItems, fixture.items)
        XCTAssertEqual(loader.latestState, .loaded)
        XCTAssertEqual(loader.items, fixture.items)
        XCTAssertEqual(loader.state, .loaded)
        XCTAssertTrue(loader.hasMorePages)

        await loader.loadNextPage()
        XCTAssertEqual(client.payloads.map(\.mode), [.recent, .latest, .recent])
        XCTAssertEqual(client.payloads.map(\.cursor), [nil, nil, "cursor-page-2"])
    }

    func testLatestFailureClearsLatestWithoutChangingMainFeed() async throws {
        let fixture = try fixture()
        let client = FakeFeedClient(responses: [fixture])
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: FakeManager()
        )
        await loader.loadInitial()
        client.error = ArticleFeedError.server(
            code: "network_unavailable",
            retryAfter: nil
        )

        await loader.loadLatest(excludingArticleIDs: [])

        XCTAssertTrue(loader.latestItems.isEmpty)
        XCTAssertEqual(
            loader.latestState,
            .failed(message: "network_unavailable", retryAt: nil)
        )
        XCTAssertEqual(loader.items, fixture.items)
        XCTAssertEqual(loader.state, .loaded)
    }

    func testLoaderExposesCooldownRetryDateWithoutDroppingItems() async throws {
        let client = FakeFeedClient(responses: [try fixture()])
        let manager = FakeManager()
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: manager
        )
        await loader.loadInitial()
        client.error = ArticleFeedError.server(code: "feed_cooldown", retryAfter: 1.0)
        await loader.loadInitial()
        XCTAssertEqual(loader.items.count, 1)
        guard case .failed(let message, let retryAt) = loader.state else {
            return XCTFail("Expected failure state")
        }
        XCTAssertEqual(message, "feed_cooldown")
        XCTAssertNotNil(retryAt)
        XCTAssertEqual(manager.resets, 0)
    }

    func testLoaderPreservesRegistrationAfterChallengeExpires() async throws {
        let client = FakeFeedClient(responses: [try fixture()])
        client.error = ArticleFeedError.server(
            code: "invalid_or_expired_challenge",
            retryAfter: nil
        )
        let manager = FakeManager()
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: manager
        )

        await loader.loadInitial()

        XCTAssertEqual(manager.resets, 0)
        XCTAssertEqual(
            loader.state,
            .failed(message: "invalid_or_expired_challenge", retryAt: nil)
        )
        XCTAssertEqual(client.payloads.count, 1, "The loader must not retry silently")
    }

    func testLoaderClearsStaleRegistrationWhenServerMarksKeyInactive() async throws {
        let client = FakeFeedClient(responses: [try fixture()])
        client.error = ArticleFeedError.server(
            code: "attested_key_not_active",
            retryAfter: nil
        )
        let manager = FakeManager()
        let loader = ArticleFeedLoader(
            device: device(),
            client: client,
            appAttest: manager
        )

        await loader.loadInitial()

        XCTAssertEqual(manager.resets, 1)
        XCTAssertEqual(client.payloads.count, 1)
    }
}
