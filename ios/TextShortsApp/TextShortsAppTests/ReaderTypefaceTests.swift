import SwiftUI
import UIKit
import XCTest
@testable import TextShortsApp

final class ReaderTypefaceTests: XCTestCase {
    func testBundledSerifFontIsRegistered() {
        let font = UIFont(name: ReaderTypeface.bodySerifName, size: ReaderTypeface.bodySerifSize)
        XCTAssertNotNil(font, "번들 명조 폰트가 UIAppFonts로 등록되어야 합니다")
        XCTAssertEqual(font?.familyName, "Noto Serif KR")
        XCTAssertEqual(font?.pointSize, ReaderTypeface.bodySerifSize)
        XCTAssertTrue(ReaderTypeface.isBodySerifAvailable)
    }

    func testBundledSerifFontCoversKoreanAndLatinBody() throws {
        let font = try XCTUnwrap(UIFont(name: ReaderTypeface.bodySerifName, size: 17))
        let sample = "주 정부는 전자거버넌스 센터와 함께 AI 활용 사례를 식별한다. (2026)"
        let coverage = font.fontDescriptor.object(forKey: .characterSet) as? NSCharacterSet
        let characterSet = try XCTUnwrap(coverage) as CharacterSet
        for scalar in sample.unicodeScalars where scalar != " " {
            XCTAssertTrue(
                characterSet.contains(scalar),
                "본문 서브셋에 U+\(String(scalar.value, radix: 16, uppercase: true))가 없습니다"
            )
        }
    }

    func testBodyTrackingMatchesResolvedTypeface() {
        XCTAssertEqual(
            ReaderTypeface.bodyTracking,
            ReaderTypeface.isBodySerifAvailable ? -0.1 : -0.35,
            accuracy: 0.0001
        )
    }

    func testReaderBodyFontScalesWithTypeSizeSetting() {
        XCTAssertEqual(Font.readerBody(), Font.readerBody(scale: 1))
        XCTAssertNotEqual(Font.readerBody(scale: 1), Font.readerBody(scale: ReaderTypeSize.large.scale))
        XCTAssertNotEqual(Font.readerBody(scale: 1), Font.readerBody(scale: ReaderTypeSize.small.scale))
    }
}
