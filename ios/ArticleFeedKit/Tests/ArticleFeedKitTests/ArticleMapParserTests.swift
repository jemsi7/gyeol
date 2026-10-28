import Foundation
import XCTest
@testable import ArticleFeedKit

final class ArticleMapParserTests: XCTestCase {
    private func fixture() throws -> FeedResponse {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "feed_response", withExtension: "json")
        )
        return try APIJSON.makeDecoder().decode(
            FeedResponse.self,
            from: Data(contentsOf: url)
        )
    }

    func testDecodesServerFixture() throws {
        let response = try fixture()
        XCTAssertEqual(response.formatVersion, 1)
        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.items[0].map.formatVersion, 3)
        XCTAssertEqual(response.items[0].map.images.count, 2)
        XCTAssertNil(response.items[0].map.coverImageRef)
        XCTAssertEqual(response.items[0].map.visualizations.count, 1)
        XCTAssertEqual(response.items[0].map.scenes.map(\.id), ["S-01", "S-02", "S-03", "S-04"])
        XCTAssertEqual(response.items[0].map.requiredClaimRefs, ["CL-001", "CL-002"])
        XCTAssertEqual(response.items[0].map.selectedClaimRefs.count, 4)
        XCTAssertEqual(response.items[0].map.editorial?.criticVerdict, "pass")
        XCTAssertEqual(response.items[0].map.headline, "도시의 여름을 바꾸는 새로운 그늘")
        XCTAssertEqual(response.cooldownSeconds, 1)
    }

    func testDecodesManualEditorialCompilerFixtureWithoutSpecialParserBranch() throws {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "manual_editor_map", withExtension: "json")
        )
        let map = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: Data(contentsOf: url)
        )

        XCTAssertEqual(map.formatVersion, 3)
        XCTAssertEqual(map.editorial?.pipelineVersion, "manual-editor-v1")
        XCTAssertEqual(map.scenes.map(\.id), ["S-01", "S-02", "S-03"])
        XCTAssertEqual(map.images.map(\.role), ["editorial", "editorial"])
        XCTAssertEqual(map.coverImageRef, 1)
        XCTAssertEqual(ArticleMapParser().parse(map).count, 3)
    }

    func testParserPreservesParagraphImageParagraphOrder() throws {
        let map = try fixture().items[0].map
        let blocks = ArticleMapParser().parse(map)
        XCTAssertEqual(blocks.count, 6)
        guard case .markdown(let first) = blocks[0] else { return XCTFail("first") }
        guard case .image(let lead) = blocks[1] else { return XCTFail("lead image") }
        guard case .markdown(let middle) = blocks[2] else { return XCTFail("middle") }
        guard case .visualization(let chart) = blocks[3] else { return XCTFail("chart") }
        guard case .image(let second) = blocks[4] else { return XCTFail("second image") }
        guard case .markdown(let last) = blocks[5] else { return XCTFail("last") }
        XCTAssertTrue(first.contains("첫 문단"))
        XCTAssertEqual(lead.ref, 0)
        XCTAssertTrue(middle.contains("둘째 문단"))
        XCTAssertEqual(chart.ref, 0)
        XCTAssertEqual(chart.kind, .bar)
        XCTAssertEqual(chart.series[0].points.map(\.rawValue), ["8분", "13분"])
        XCTAssertEqual(second.ref, 1)
        XCTAssertTrue(last.contains("마지막 문단"))
    }

    func testMissingImageAndUnknownDirectiveDoNotCrash() throws {
        let original = try fixture().items[0].map
        let json = try APIJSON.makeEncoder().encode(original)
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: json) as? [String: Any]
        )
        object["body_markdown"] = "앞\n<img:9>\n중간\n<chart:9>\n<graph:1>\n뒤"
        let changed = try JSONSerialization.data(withJSONObject: object)
        let map = try APIJSON.makeDecoder().decode(ArticleMap.self, from: changed)
        let blocks = ArticleMapParser().parse(map)
        XCTAssertEqual(
            blocks.filter {
                if case .unsupported = $0 { return true }
                return false
            }.count,
            3
        )
        XCTAssertTrue(blocks.contains(.unsupported("<img:9>")))
        XCTAssertTrue(blocks.contains(.unsupported("<chart:9>")))
        XCTAssertTrue(blocks.contains(.unsupported("<graph:1>")))
    }

    func testLegacyV1WithoutVisualizationFieldsDecodesAsEmpty() throws {
        let original = try fixture().items[0].map
        let data = try APIJSON.makeEncoder().encode(original)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object["format_version"] = 1
        object.removeValue(forKey: "visualizations")
        object.removeValue(forKey: "used_chart_refs")
        object.removeValue(forKey: "claim_ids")
        object.removeValue(forKey: "required_claim_refs")
        object.removeValue(forKey: "selected_claim_refs")
        object.removeValue(forKey: "scenes")
        object.removeValue(forKey: "editorial")
        object["body_markdown"] = "legacy paragraph\n<img:0>"

        let legacy = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(legacy.formatVersion, 1)
        XCTAssertTrue(legacy.visualizations.isEmpty)
        XCTAssertTrue(legacy.usedChartRefs.isEmpty)
        XCTAssertTrue(legacy.claimIDs.isEmpty)
        XCTAssertTrue(legacy.requiredClaimRefs.isEmpty)
        XCTAssertTrue(legacy.selectedClaimRefs.isEmpty)
        XCTAssertTrue(legacy.scenes.isEmpty)
        XCTAssertNil(legacy.editorial)
        XCTAssertEqual(ArticleMapParser().parse(legacy).count, 2)
    }

    func testOptionalCoverImageRefDecodesAndRoundTrips() throws {
        let original = try fixture().items[0].map
        let data = try APIJSON.makeEncoder().encode(original)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object["cover_image_ref"] = 1

        let decoded = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(decoded.coverImageRef, 1)

        let roundTrip = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: APIJSON.makeEncoder().encode(decoded)
        )
        XCTAssertEqual(roundTrip.coverImageRef, 1)

        object.removeValue(forKey: "cover_image_ref")
        let absent = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertNil(absent.coverImageRef)
    }

    func testFutureSceneRoleIsPreservedAndRoundTrips() throws {
        let original = try fixture().items[0].map
        let data = try APIJSON.makeEncoder().encode(original)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var scenes = try XCTUnwrap(object["scenes"] as? [[String: Any]])
        scenes[0]["role"] = "future-role"
        object["scenes"] = scenes

        let decoded = try APIJSON.makeDecoder().decode(
            ArticleMap.self,
            from: JSONSerialization.data(withJSONObject: object)
        )
        XCTAssertEqual(decoded.scenes[0].role, .unsupported("future-role"))

        let encoded = try APIJSON.makeEncoder().encode(decoded)
        let roundTripped = try APIJSON.makeDecoder().decode(ArticleMap.self, from: encoded)
        XCTAssertEqual(roundTripped.scenes[0].role.rawValue, "future-role")
    }

    func testCanonicalJSONUsesSortedKeysAndUnescapedKorean() throws {
        struct Golden: Encodable { let b: Int; let a: String }
        let data = try CanonicalJSON.encode(Golden(b: 1, a: "한글"))
        XCTAssertEqual(String(decoding: data, as: UTF8.self), #"{"a":"한글","b":1}"#)
    }
}
