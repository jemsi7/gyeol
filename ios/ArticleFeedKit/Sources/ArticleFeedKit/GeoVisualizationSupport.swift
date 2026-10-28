import CoreGraphics
import Foundation

public enum ArticleGeoScope: String, Codable, CaseIterable, Sendable {
    case world
    case africa
    case asia
    case europe
    case northAmerica = "north_america"
    case southAmerica = "south_america"
    case oceania
}

public struct ArticleGeoCoordinate: Codable, Equatable, Sendable {
    public let longitude: Double
    public let latitude: Double

    public init(from decoder: Decoder) throws {
        var values = try decoder.unkeyedContainer()
        longitude = try values.decode(Double.self)
        latitude = try values.decode(Double.self)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.unkeyedContainer()
        try values.encode(longitude)
        try values.encode(latitude)
    }
}

public struct ArticleGeoFeature: Codable, Equatable, Sendable {
    public let code: String
    public let name: String
    public let scope: ArticleGeoScope
    public let polygons: [[ArticleGeoCoordinate]]
}

private struct ArticleGeoResource: Codable {
    let version: Int
    let countries: [ArticleGeoFeature]
}

public struct ArticleGeoRepository: Sendable {
    public static let shared = ArticleGeoRepository.loadBundled()

    public let features: [ArticleGeoFeature]

    public func features(scope: ArticleGeoScope) -> [ArticleGeoFeature] {
        scope == .world ? features : features.filter { $0.scope == scope }
    }

    public func feature(code: String) -> ArticleGeoFeature? {
        features.first { $0.code == code }
    }

    private static func loadBundled() -> ArticleGeoRepository {
        guard let url = Bundle.module.url(
            forResource: "world-countries-110m",
            withExtension: "json"
        ), let data = try? Data(contentsOf: url),
           let resource = try? JSONDecoder().decode(ArticleGeoResource.self, from: data)
        else {
            return ArticleGeoRepository(features: [])
        }
        return ArticleGeoRepository(features: resource.countries)
    }
}

public struct ArticleGeoViewport: Equatable, Sendable {
    public let minLongitude: Double
    public let maxLongitude: Double
    public let minLatitude: Double
    public let maxLatitude: Double

    public static func forScope(_ scope: ArticleGeoScope) -> ArticleGeoViewport {
        switch scope {
        case .world: return .init(minLongitude: -180, maxLongitude: 180, minLatitude: -60, maxLatitude: 85)
        case .africa: return .init(minLongitude: -20, maxLongitude: 55, minLatitude: -36, maxLatitude: 38)
        case .asia: return .init(minLongitude: 25, maxLongitude: 180, minLatitude: -12, maxLatitude: 82)
        case .europe: return .init(minLongitude: -25, maxLongitude: 50, minLatitude: 34, maxLatitude: 72)
        case .northAmerica: return .init(minLongitude: -170, maxLongitude: -50, minLatitude: 5, maxLatitude: 85)
        case .southAmerica: return .init(minLongitude: -92, maxLongitude: -30, minLatitude: -58, maxLatitude: 16)
        case .oceania: return .init(minLongitude: 110, maxLongitude: 180, minLatitude: -50, maxLatitude: 10)
        }
    }

    public func project(_ coordinate: ArticleGeoCoordinate, into size: CGSize) -> CGPoint {
        let width = max(maxLongitude - minLongitude, .leastNonzeroMagnitude)
        let height = max(maxLatitude - minLatitude, .leastNonzeroMagnitude)
        let x = (coordinate.longitude - minLongitude) / width
        let y = (maxLatitude - coordinate.latitude) / height
        return CGPoint(x: x * size.width, y: y * size.height)
    }

    public func fittedRect(in size: CGSize) -> CGRect {
        guard size.width > 0, size.height > 0 else { return .zero }
        let geographicWidth = maxLongitude - minLongitude
        let geographicHeight = maxLatitude - minLatitude
        guard geographicWidth > 0, geographicHeight > 0 else { return .zero }
        let geographicAspect = geographicWidth / geographicHeight
        let canvasAspect = size.width / size.height
        if canvasAspect > geographicAspect {
            let width = size.height * geographicAspect
            return CGRect(x: (size.width - width) / 2, y: 0, width: width, height: size.height)
        }
        let height = size.width / geographicAspect
        return CGRect(x: 0, y: (size.height - height) / 2, width: size.width, height: height)
    }
}

public struct ArticleColorComponents: Equatable, Sendable {
    public let red: Double
    public let green: Double
    public let blue: Double

    public init(red: Double, green: Double, blue: Double) {
        self.red = red
        self.green = green
        self.blue = blue
    }
}

public enum ArticleColorScaleSupport {
    private static let palettes: [String: [ArticleColorComponents]] = [
        "ocean": [
            .init(red: 0.91, green: 0.95, blue: 0.94),
            .init(red: 0.62, green: 0.82, blue: 0.80),
            .init(red: 0.31, green: 0.62, blue: 0.66),
            .init(red: 0.16, green: 0.45, blue: 0.53),
            .init(red: 0.10, green: 0.31, blue: 0.38),
        ],
        "forest": [
            .init(red: 0.94, green: 0.94, blue: 0.86),
            .init(red: 0.70, green: 0.82, blue: 0.59),
            .init(red: 0.43, green: 0.65, blue: 0.39),
            .init(red: 0.25, green: 0.50, blue: 0.29),
            .init(red: 0.16, green: 0.38, blue: 0.24),
        ],
        "ember": [
            .init(red: 0.97, green: 0.91, blue: 0.82),
            .init(red: 0.91, green: 0.70, blue: 0.45),
            .init(red: 0.82, green: 0.49, blue: 0.28),
            .init(red: 0.69, green: 0.31, blue: 0.18),
            .init(red: 0.56, green: 0.19, blue: 0.12),
        ],
        "violet": [
            .init(red: 0.94, green: 0.92, blue: 0.96),
            .init(red: 0.76, green: 0.68, blue: 0.85),
            .init(red: 0.58, green: 0.48, blue: 0.72),
            .init(red: 0.44, green: 0.34, blue: 0.62),
            .init(red: 0.33, green: 0.24, blue: 0.52),
        ],
        "diverging": [
            .init(red: 0.12, green: 0.36, blue: 0.53),
            .init(red: 0.48, green: 0.68, blue: 0.73),
            .init(red: 0.94, green: 0.92, blue: 0.86),
            .init(red: 0.87, green: 0.61, blue: 0.40),
            .init(red: 0.75, green: 0.31, blue: 0.16),
        ],
    ]

    public static func normalized(
        value: Double,
        values: [Double],
        scale: ArticleColorScale
    ) -> Double {
        let finite = values.filter(\.isFinite)
        guard value.isFinite, !finite.isEmpty else { return 0.5 }
        let result: Double
        if scale.scheme == "diverging" {
            let magnitude = finite.map(abs).max() ?? 0
            result = magnitude > 0 ? 0.5 + value / (magnitude * 2) : 0.5
        } else if let minimum = finite.min(), let maximum = finite.max(), maximum > minimum {
            result = (value - minimum) / (maximum - minimum)
        } else {
            result = 0.5
        }
        let clamped = min(max(result, 0), 1)
        return scale.mode == "quantized" ? (clamped * 4).rounded() / 4 : clamped
    }

    public static func components(
        value: Double,
        values: [Double],
        scale: ArticleColorScale
    ) -> ArticleColorComponents {
        let palette = palettes[scale.scheme] ?? palettes["ocean"]!
        let normalizedValue = normalized(value: value, values: values, scale: scale)
        let position = normalizedValue * Double(palette.count - 1)
        let lower = min(Int(position.rounded(.down)), palette.count - 1)
        let upper = min(lower + 1, palette.count - 1)
        let fraction = position - Double(lower)
        return interpolate(palette[lower], palette[upper], fraction: fraction)
    }

    public static func stops(scale: ArticleColorScale) -> [ArticleColorComponents] {
        palettes[scale.scheme] ?? palettes["ocean"]!
    }

    private static func interpolate(
        _ start: ArticleColorComponents,
        _ end: ArticleColorComponents,
        fraction: Double
    ) -> ArticleColorComponents {
        .init(
            red: start.red + (end.red - start.red) * fraction,
            green: start.green + (end.green - start.green) * fraction,
            blue: start.blue + (end.blue - start.blue) * fraction
        )
    }
}
