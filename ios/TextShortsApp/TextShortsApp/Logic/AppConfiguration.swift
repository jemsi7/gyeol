import ArticleFeedKit
import Foundation

enum BackendEnvironment: String, Sendable {
    case staging
    case production
}

enum AppConfigurationError: Error, Equatable {
    case missingBackendEnvironment
    case invalidBackendEnvironment
    case missingAPIBaseURL
    case invalidAPIBaseURL
    case missingAPNsEnvironment
    case invalidAPNsEnvironment
    case environmentIdentityMismatch
}

struct AppConfiguration: Equatable, Sendable {
    private static let productionBundleIdentifier = "com.example.gyeol"
    private static let stagingBundleIdentifier = "com.example.gyeol.staging"
    private static let productionHost = "server-production-bf47.up.railway.app"
    private static let stagingHost = "server-staging-production-0fdf.up.railway.app"

    let backendEnvironment: BackendEnvironment
    let apiBaseURL: URL
    let apnsEnvironment: APNsEnvironment

    init(info: [String: Any], bundleIdentifier: String?) throws {
        guard let rawBackendEnvironment = info["BACKEND_ENVIRONMENT"] as? String else {
            throw AppConfigurationError.missingBackendEnvironment
        }
        guard let backendEnvironment = BackendEnvironment(
            rawValue: rawBackendEnvironment.trimmingCharacters(in: .whitespacesAndNewlines)
        ) else {
            throw AppConfigurationError.invalidBackendEnvironment
        }

        guard let rawValue = info["ARTICLE_API_BASE_URL"] as? String else {
            throw AppConfigurationError.missingAPIBaseURL
        }
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty,
              !value.contains("$("),
              let components = URLComponents(string: value),
              components.scheme?.lowercased() == "https",
              let host = components.host?.lowercased(),
              !host.isEmpty,
              components.user == nil,
              components.password == nil,
              components.port == nil,
              components.query == nil,
              components.fragment == nil,
              components.path.isEmpty || components.path == "/",
              let url = components.url else {
            throw AppConfigurationError.invalidAPIBaseURL
        }

        guard let rawAPNsEnvironment = info["APNS_REGISTRATION_ENVIRONMENT"] as? String else {
            throw AppConfigurationError.missingAPNsEnvironment
        }
        switch rawAPNsEnvironment.trimmingCharacters(in: .whitespacesAndNewlines) {
        case APNsEnvironment.development.rawValue:
            apnsEnvironment = .development
        case APNsEnvironment.production.rawValue:
            apnsEnvironment = .production
        default:
            throw AppConfigurationError.invalidAPNsEnvironment
        }

        guard let rawExpectedHost = info["BACKEND_EXPECTED_HOST"] as? String else {
            throw AppConfigurationError.environmentIdentityMismatch
        }
        let expectedHost = rawExpectedHost
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        guard !expectedHost.isEmpty,
              !expectedHost.contains("$("),
              host == expectedHost else {
            throw AppConfigurationError.environmentIdentityMismatch
        }

        switch backendEnvironment {
        case .staging:
            guard bundleIdentifier == Self.stagingBundleIdentifier,
                  expectedHost != Self.productionHost,
                  expectedHost == Self.stagingHost else {
                throw AppConfigurationError.environmentIdentityMismatch
            }
        case .production:
            guard bundleIdentifier == Self.productionBundleIdentifier,
                  expectedHost == Self.productionHost else {
                throw AppConfigurationError.environmentIdentityMismatch
            }
        }

        self.backendEnvironment = backendEnvironment
        apiBaseURL = url
    }

    static func live(bundle: Bundle = .main) throws -> AppConfiguration {
        try AppConfiguration(
            info: bundle.infoDictionary ?? [:],
            bundleIdentifier: bundle.bundleIdentifier
        )
    }
}
