import ArticleFeedKit
import Foundation

enum PrivacyPolicy {
    static let currentVersion = "1.5.0"
    static let url = URL(
        string: "https://example.invalid/privacy"
    )!
}

enum PrivacyConsentState: Equatable, Sendable {
    case idle
    case saving
    case accepted
    case failed(String)
}

@MainActor
protocol PrivacyConsentServicing: AnyObject {
    func record(policyVersion: String) async throws -> PrivacyConsentReceipt
}

protocol PrivacyConsentAppAttestManaging: Sendable {
    func makeAssertion(payload: PrivacyConsentPayload) async throws -> AssertionEnvelope
    func resetRegistration() async
}

extension AppAttestManager: PrivacyConsentAppAttestManaging {}

@MainActor
final class LivePrivacyConsentService: PrivacyConsentServicing {
    private let client: any PrivacyConsentAPIClientProtocol
    private let appAttest: any PrivacyConsentAppAttestManaging
    private let device: DeviceInfo

    init(
        client: any PrivacyConsentAPIClientProtocol,
        appAttest: any PrivacyConsentAppAttestManaging,
        device: DeviceInfo
    ) {
        self.client = client
        self.appAttest = appAttest
        self.device = device
    }

    func record(policyVersion: String) async throws -> PrivacyConsentReceipt {
        let payload = PrivacyConsentPayload(
            policyVersion: policyVersion,
            device: device
        )

        do {
            return try await submit(payload)
        } catch let ArticleFeedError.server(code, _)
            where code.lowercased() == "invalid_or_expired_challenge"
        {
            return try await submit(payload)
        } catch let ArticleFeedError.server(code, _)
            where code.lowercased() == "attested_key_not_active"
        {
            await appAttest.resetRegistration()
            return try await submit(payload)
        }
    }

    private func submit(
        _ payload: PrivacyConsentPayload
    ) async throws -> PrivacyConsentReceipt {
        let assertion = try await appAttest.makeAssertion(payload: payload)
        return try await client.recordPrivacyConsent(payload: payload, auth: assertion)
    }
}

@MainActor
final class MemoryPrivacyConsentService: PrivacyConsentServicing {
    var result: Result<PrivacyConsentReceipt, Error>
    private(set) var requestedVersions: [String] = []

    init(result: Result<PrivacyConsentReceipt, Error>? = nil) {
        self.result = result ?? .success(
            PrivacyConsentReceipt(
                consentID: UUID(uuidString: "33333333-3333-3333-3333-333333333333")!,
                policyVersion: PrivacyPolicy.currentVersion,
                policyURL: PrivacyPolicy.url,
                acceptedAt: Date(timeIntervalSince1970: 1_784_721_600)
            )
        )
    }

    func record(policyVersion: String) async throws -> PrivacyConsentReceipt {
        requestedVersions.append(policyVersion)
        return try result.get()
    }
}

@MainActor
final class UnavailablePrivacyConsentService: PrivacyConsentServicing {
    func record(policyVersion: String) async throws -> PrivacyConsentReceipt {
        throw ArticleFeedError.server(
            code: "privacy_consent_unavailable",
            retryAfter: nil
        )
    }
}
