import Foundation
import XCTest
@testable import ArticleFeedKit

final class DocumentMetadataTests: XCTestCase {
    func testDecodesMissingDocumentType() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "feed_response", withExtension: "json")
        )
        let data = try Data(contentsOf: fixtureURL)
        let response = try APIJSON.makeDecoder().decode(FeedResponse.self, from: data)

        XCTAssertFalse(response.items.isEmpty)
        XCTAssertTrue(response.items.allSatisfy { $0.documentType.isEmpty })
        XCTAssertTrue(response.items.allSatisfy { $0.sourcePublisher.isEmpty })
    }
}
