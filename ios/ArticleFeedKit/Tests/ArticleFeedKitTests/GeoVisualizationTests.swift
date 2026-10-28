import CoreGraphics
import Foundation
import Testing
@testable import ArticleFeedKit

@Test func bundledCountryResourceLoadsExpectedCoverage() {
    let repository = ArticleGeoRepository.shared
    #expect(repository.features.count == 173)
    #expect(repository.feature(code: "KR")?.scope == .asia)
    #expect(repository.feature(code: "US")?.scope == .northAmerica)
    #expect(Set(repository.features.map(\.scope)) == Set(ArticleGeoScope.allCases.filter { $0 != .world }))
}

@Test func everyViewportProjectsItsRepresentativeCountryIntoFrame() throws {
    let representatives: [(ArticleGeoScope, String)] = [
        (.africa, "ZA"), (.asia, "KR"), (.europe, "FR"),
        (.northAmerica, "US"), (.southAmerica, "BR"), (.oceania, "AU"),
    ]
    let size = CGSize(width: 320, height: 180)
    for (scope, code) in representatives {
        let feature = try #require(ArticleGeoRepository.shared.feature(code: code))
        let coordinate = try #require(feature.polygons.first?.first)
        let point = ArticleGeoViewport.forScope(scope).project(coordinate, into: size)
        #expect(point.x.isFinite && point.y.isFinite)
        #expect(point.x > -size.width && point.x < size.width * 2)
        #expect(point.y > -size.height && point.y < size.height * 2)
    }
}

@Test func viewportAspectFitPreservesGeographicRatioInCompactAndWideFrames() {
    let viewport = ArticleGeoViewport.forScope(.world)
    let expectedAspect = (viewport.maxLongitude - viewport.minLongitude)
        / (viewport.maxLatitude - viewport.minLatitude)
    for size in [CGSize(width: 320, height: 220), CGSize(width: 1_000, height: 220)] {
        let rect = viewport.fittedRect(in: size)
        #expect(abs(rect.width / rect.height - expectedAspect) < 0.000_001)
        #expect(rect.minX >= 0 && rect.maxX <= size.width)
        #expect(rect.minY >= 0 && rect.maxY <= size.height)
    }
}

@Test func colorScaleHandlesContinuousQuantizedConstantAndDivergingDomains() {
    let continuous = ArticleColorScale(scheme: "ocean", mode: "continuous")
    #expect(ArticleColorScaleSupport.normalized(value: 10, values: [10, 20], scale: continuous) == 0)
    #expect(ArticleColorScaleSupport.normalized(value: 20, values: [10, 20], scale: continuous) == 1)
    #expect(ArticleColorScaleSupport.normalized(value: 10, values: [10, 10], scale: continuous) == 0.5)

    let quantized = ArticleColorScale(scheme: "forest", mode: "quantized")
    #expect(ArticleColorScaleSupport.normalized(value: 14, values: [10, 20], scale: quantized) == 0.5)

    let diverging = ArticleColorScale(scheme: "diverging", mode: "continuous")
    #expect(ArticleColorScaleSupport.normalized(value: 0, values: [-20, 10], scale: diverging) == 0.5)
    #expect(ArticleColorScaleSupport.normalized(value: -20, values: [-20, 10], scale: diverging) == 0)
}

@Test func legacyVisualizationDecodesWithoutGeoOrColorFields() throws {
    let data = Data(#"{"ref":0,"kind":"bar","title":"T","unit":"%","alt_text":"A","series":[{"name":"S","mark":"bar","points":[{"label":"A","value":1,"raw_value":"1%","evidence":"A 1%"}]}]}"#.utf8)
    let value = try JSONDecoder().decode(ArticleVisualization.self, from: data)
    #expect(value.geoScope == nil)
    #expect(value.colorScale == nil)
    #expect(value.series[0].points[0].regionCode == nil)
}
