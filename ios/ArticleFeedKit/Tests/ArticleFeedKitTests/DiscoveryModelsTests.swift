import Foundation
import XCTest
@testable import ArticleFeedKit

enum DiscoveryTestFixture {
    static var url: URL {
        var root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        for _ in 0..<4 {
            root.deleteLastPathComponent()
        }
        return root
            .appendingPathComponent("server")
            .appendingPathComponent("tests")
            .appendingPathComponent("fixtures")
            .appendingPathComponent("discovery_response.json")
    }

    static func response() throws -> DiscoveryResponse {
        try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: Data(contentsOf: url)
        )
    }
}

final class DiscoveryModelsTests: XCTestCase {
    func testCanonicalFixtureDecodesRepresentativeMultiRouteCatalog() throws {
        let response = try DiscoveryTestFixture.response()

        XCTAssertEqual(response.routes.count, 2)
        XCTAssertEqual(response.representativeRoute?.rank, 1)
        XCTAssertEqual(response.routes.map(\.items.count), [4, 4])
        XCTAssertEqual(
            Set(response.routes.flatMap { $0.items.map(\.article.articleID) }).count,
            8
        )
        XCTAssertEqual(response.results.map(\.depth), [.quick, .deep])
        XCTAssertEqual(response.results.map(\.retrieval), [.keyword, .keyword])
        XCTAssertEqual(response.searchSuggestions.count, 4)
        XCTAssertLessThanOrEqual(response.trendingSearchSuggestions.count, 3)
        XCTAssertEqual(response.timelineStatus, .unavailable)
        XCTAssertNil(response.timelinesGeneratedAt)
        XCTAssertNil(response.timelinesRefreshedAt)
        XCTAssertTrue(response.featuredTimelines.isEmpty)
    }

    func testCanonicalFixtureRoundTripsThroughSwiftWireModels() throws {
        let response = try DiscoveryTestFixture.response()
        let encoded = try APIJSON.makeEncoder().encode(response)
        let decoded = try APIJSON.makeDecoder().decode(DiscoveryResponse.self, from: encoded)

        XCTAssertEqual(decoded, response)
    }

    func testDiscoveryModelDecodesMissingTrendingSuggestionsAsEmpty() throws {
        let response = try DiscoveryTestFixture.response()
        let encoded = try APIJSON.makeEncoder().encode(response)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        object.removeValue(forKey: "trending_search_suggestions")

        let decoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: object)
        )

        XCTAssertEqual(decoded.trendingSearchSuggestions, [])
    }

    func testC11602DiscoveryResultRetrievalDefaultsToKeywordAndDecodesRAG() throws {
        let response = try DiscoveryTestFixture.response()
        let encoded = try APIJSON.makeEncoder().encode(response)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        var missingResults = try XCTUnwrap(object["results"] as? [[String: Any]])
        missingResults = missingResults.map { result in
            var result = result
            result.removeValue(forKey: "retrieval")
            return result
        }
        object["results"] = missingResults

        let missingDecoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(missingDecoded.results.map(\.retrieval), [.keyword, .keyword])

        var ragObject = object
        var ragResults = missingResults
        ragResults[1]["retrieval"] = "rag"
        ragObject["results"] = ragResults
        let ragDecoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: ragObject)
        )
        XCTAssertEqual(ragDecoded.results.map(\.retrieval), [.keyword, .rag])
    }

    func testDiscoveryModelValidatesTrendingSuggestionWireContract() throws {
        let response = try DiscoveryTestFixture.response()
        let encoded = try APIJSON.makeEncoder().encode(response)
        let base = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        var valid = base
        valid["trending_search_suggestions"] = ["도시 재생", "전력망 전환", "기후 적응"]

        let decoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: valid)
        )
        XCTAssertEqual(
            decoded.trendingSearchSuggestions,
            ["도시 재생", "전력망 전환", "기후 적응"]
        )

        let invalidValues: [Any] = [
            ["하나", "둘", "셋", "넷"],
            ["중복", "중복"],
            ["한"],
            [" 앞뒤 공백"],
            ["줄\n바꿈"],
            [1, 2],
        ]
        for invalidValue in invalidValues {
            var invalid = base
            invalid["trending_search_suggestions"] = invalidValue
            let data = try JSONSerialization.data(withJSONObject: invalid)
            XCTAssertThrowsError(
                try APIJSON.makeDecoder().decode(DiscoveryResponse.self, from: data),
                "Invalid trending value should fail: \(invalidValue)"
            )
        }
    }

    func testDiscoveryPayloadCanonicalJSONIncludesExplicitNullDiscoveryMode() throws {
        let payload = DiscoveryPayload(
            device: DeviceInfo(
                installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
                vendorID: nil,
                platform: .ipados,
                model: "iPad14,5",
                osVersion: "26.0",
                appVersion: "1.0",
                buildNumber: "1",
                locale: "ko-KR"
            ),
            query: nil,
            limit: 12
        )
        let encoded = String(decoding: try CanonicalJSON.encode(payload), as: UTF8.self)

        XCTAssertTrue(encoded.contains(#""query":null"#))
        XCTAssertTrue(encoded.contains(#""collection":null"#))
        XCTAssertTrue(encoded.contains(#""cursor":null"#))
        XCTAssertTrue(encoded.contains(#""vendor_id":null"#))
    }

    func testPublicationPayloadRoundTripsCollectionCursor() throws {
        for collection in [DiscoveryCollection.editorial, .weeklyInsight] {
            let payload = DiscoveryPayload(
                device: DeviceInfo(
                    installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
                    vendorID: nil,
                    platform: .ipados,
                    model: "iPad14,5",
                    osVersion: "26.0",
                    appVersion: "1.0",
                    buildNumber: "1",
                    locale: "ko-KR"
                ),
                limit: 20,
                collection: collection,
                cursor: "cursor-page-2"
            )
            let encoded = try CanonicalJSON.encode(payload)
            let decoded = try APIJSON.makeDecoder().decode(DiscoveryPayload.self, from: encoded)

            XCTAssertEqual(decoded.collection, collection)
            XCTAssertEqual(decoded.cursor, "cursor-page-2")
            XCTAssertNil(decoded.query)
            XCTAssertEqual(decoded.limit, 20)
        }
    }

    func testFeaturedTimelineDecodesStrictTreeAndDefaultsMissingAdditiveFields() throws {
        let response = try DiscoveryTestFixture.response()
        let encoded = try APIJSON.makeEncoder().encode(response)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        let routes = try XCTUnwrap(object["routes"] as? [[String: Any]])
        let items = try XCTUnwrap(routes.first?["items"] as? [[String: Any]])
        var articles = try items.prefix(4).map { item in
            try XCTUnwrap(item["article"] as? [String: Any])
        }
        for index in articles.indices {
            articles[index]["article_id"] = String(repeating: String(index), count: 64)
        }
        let mainIDs = [
            "10000000-0000-0000-0000-000000000001",
            "10000000-0000-0000-0000-000000000002",
            "10000000-0000-0000-0000-000000000003",
        ]
        let nodes: [[String: Any]] = [
            [
                "id": mainIDs[0], "lane": "main", "anchor_node_id": NSNull(),
                "update_kind": "existing", "published_at": "2026-08-02T12:00:00Z",
                "date_source": "provider", "article": articles[0],
            ],
            [
                "id": mainIDs[1], "lane": "main", "anchor_node_id": NSNull(),
                "update_kind": "inserted", "published_at": "2026-08-01T14:00:00Z",
                "date_source": "provider", "article": articles[1],
            ],
            [
                "id": mainIDs[2], "lane": "main", "anchor_node_id": NSNull(),
                "update_kind": "occurred", "published_at": "2026-08-01T16:00:00Z",
                "date_source": "first_seen", "article": articles[2],
            ],
            [
                "id": "10000000-0000-0000-0000-000000000004", "lane": "branch",
                "anchor_node_id": mainIDs[1], "update_kind": "existing",
                "published_at": "2026-08-02T15:00:00Z", "date_source": "provider",
                "article": articles[3],
            ],
        ]
        object["timeline_status"] = "fresh"
        object["timelines_generated_at"] = "2026-07-20T02:00:00Z"
        object["timelines_refreshed_at"] = "2026-07-20T03:00:00Z"
        object["featured_timelines"] = [[
            "id": "20000000-0000-0000-0000-000000000001",
            "rank": 1,
            "stable_key": "stable-timeline-1",
            "title": "AI 인프라 변화의 주요 흐름",
            "summary": "수요와 전력, 공급망 변화가 서로 이어진 사건을 시간 순서대로 정리한 타임라인입니다.",
            "topic": "AI 인프라",
            "started_at": "2026-07-17T01:00:00Z",
            "ended_at": "2026-07-20T01:00:00Z",
            "nodes": nodes,
        ]]

        let decoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(decoded.timelineStatus, .fresh)
        XCTAssertEqual(decoded.featuredTimelines.count, 1)
        XCTAssertEqual(decoded.featuredTimelines[0].nodes.map(\.lane), [.main, .main, .main, .branch])
        XCTAssertEqual(
            decoded.featuredTimelines[0].chronologicalNodes.map(\.updateKind),
            [.inserted, .existing, .occurred, .existing]
        )
        XCTAssertEqual(
            decoded.featuredTimelines[0].chronologicalNodes.map(\.article.articleID),
            [
                String(repeating: "1", count: 64),
                String(repeating: "0", count: 64),
                String(repeating: "2", count: 64),
                String(repeating: "3", count: 64),
            ]
        )

        object["timeline_results"] = object["featured_timelines"]
        let searchDecoded = try APIJSON.makeDecoder().decode(
            DiscoveryResponse.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(searchDecoded.timelineResults.count, 1)
        XCTAssertEqual(searchDecoded.timelineResults[0].stableKey, "stable-timeline-1")

        var invalid = object
        var timelines = try XCTUnwrap(invalid["featured_timelines"] as? [[String: Any]])
        var timeline = timelines[0]
        var invalidNodes = nodes
        invalidNodes[3]["anchor_node_id"] = "30000000-0000-0000-0000-000000000099"
        timeline["nodes"] = invalidNodes
        timelines[0] = timeline
        invalid["featured_timelines"] = timelines
        XCTAssertThrowsError(
            try APIJSON.makeDecoder().decode(
                DiscoveryResponse.self,
                from: JSONSerialization.data(withJSONObject: invalid)
            )
        )
    }

    func testPushPreferencesDecodeMissingTimelineUpdatesAsFalseAndRoundTripTrue() throws {
        let old = try APIJSON.makeDecoder().decode(
            PushNotificationPreferences.self,
            from: Data(#"{"editorial":false,"weekly_insight":false}"#.utf8)
        )
        XCTAssertFalse(old.timelineUpdates)
        XCTAssertFalse(old.timelineRefreshes)
        XCTAssertFalse(old.timelineAdditions)
        XCTAssertFalse(old.anyEnabled)

        let legacy = try APIJSON.makeDecoder().decode(
            PushNotificationPreferences.self,
            from: Data(#"{"editorial":false,"weekly_insight":false,"timeline_updates":true}"#.utf8)
        )
        XCTAssertTrue(legacy.timelineRefreshes)
        XCTAssertTrue(legacy.timelineAdditions)

        let value = PushNotificationPreferences(
            timelineRefreshes: true,
            timelineAdditions: false
        )
        let encoded = try APIJSON.makeEncoder().encode(value)
        let decoded = try APIJSON.makeDecoder().decode(
            PushNotificationPreferences.self,
            from: encoded
        )
        XCTAssertEqual(decoded, value)
        XCTAssertTrue(decoded.anyEnabled)
        XCTAssertTrue(decoded.timelineRefreshes)
        XCTAssertFalse(decoded.timelineAdditions)
        XCTAssertTrue(decoded.timelineUpdates)
    }
}
