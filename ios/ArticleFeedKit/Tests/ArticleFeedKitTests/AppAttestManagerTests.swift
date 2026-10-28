import CryptoKit
import Foundation
import XCTest
@testable import ArticleFeedKit

#if canImport(DeviceCheck)
import DeviceCheck
#endif

private final class FakeAttestService: AppAttestServicing, @unchecked Sendable {
    var isSupported = true
    var generatedKeys = 0
    var attestedKeys = 0
    var assertions = 0
    var lastAssertionHash: Data?
    var attestationFailures: [ArticleFeedError] = []
    var attestationKeyIDs: [String] = []
    var attestationHashes: [Data] = []

    func generateKey() async throws -> String {
        generatedKeys += 1
        return "apple-app-attest-key-id"
    }

    func attestKey(_ keyID: String, clientDataHash: Data) async throws -> Data {
        attestedKeys += 1
        attestationKeyIDs.append(keyID)
        attestationHashes.append(clientDataHash)
        if !attestationFailures.isEmpty {
            throw attestationFailures.removeFirst()
        }
        return Data("attestation-object".utf8)
    }

    func generateAssertion(_ keyID: String, clientDataHash: Data) async throws -> Data {
        assertions += 1
        lastAssertionHash = clientDataHash
        return Data("assertion-object".utf8)
    }
}

private actor SleepRecorder {
    private(set) var durations: [UInt64] = []

    func record(_ duration: UInt64) {
        durations.append(duration)
    }
}

private final class FakeKeyStore: AppAttestKeyStoring, @unchecked Sendable {
    var keyID: String?
    var registered = false
    func loadKeyID() -> String? { keyID }
    func isRegistered() -> Bool { registered }
    func saveKeyID(_ keyID: String) { self.keyID = keyID }
    func markRegistered() { registered = true }
    func clear() { keyID = nil; registered = false }
}

private final class FakeAttestTransport: AppAttestTransport, @unchecked Sendable {
    let challengeID = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
    let challengeValue = Base64URL.encode(Data(repeating: 0, count: 32))
    var purposes: [ChallengePurpose] = []
    var registrations = 0
    var registrationBearerTokens: [String?] = []

    func challenge(_ request: ChallengeRequest) async throws -> ChallengeResponse {
        purposes.append(request.purpose)
        let json = """
        {"challenge_id":"\(challengeID.uuidString.lowercased())","challenge":"\(challengeValue)","expires_at":"2026-07-10T10:02:00Z"}
        """
        return try APIJSON.makeDecoder().decode(ChallengeResponse.self, from: Data(json.utf8))
    }

    func register(
        _ request: RegisterRequest,
        bearerToken: String?
    ) async throws -> RegisterResponse {
        registrations += 1
        registrationBearerTokens.append(bearerToken)
        let json = "{\"registered\":true,\"key_id\":\"\(request.keyID)\"}"
        return try APIJSON.makeDecoder().decode(RegisterResponse.self, from: Data(json.utf8))
    }
}

final class AppAttestManagerTests: XCTestCase {
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

    #if canImport(DeviceCheck)
    func testDeviceCheckServerUnavailableClassifierRejectsOtherErrors() {
        let transient = NSError(
            domain: DCError.errorDomain,
            code: DCError.Code.serverUnavailable.rawValue
        )
        let invalidKey = NSError(
            domain: DCError.errorDomain,
            code: DCError.Code.invalidKey.rawValue
        )
        let foreign = NSError(
            domain: "example.error",
            code: DCError.Code.serverUnavailable.rawValue
        )

        XCTAssertTrue(SystemAppAttestService.isServerUnavailable(transient))
        XCTAssertFalse(SystemAppAttestService.isServerUnavailable(invalidKey))
        XCTAssertFalse(SystemAppAttestService.isServerUnavailable(foreign))
    }
    #endif

    func testEnsureRegisteredGeneratesAndPersistsKeyOnce() async throws {
        let service = FakeAttestService()
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport
        )
        let first = try await manager.ensureRegistered(device: device)
        let second = try await manager.ensureRegistered(device: device)
        XCTAssertEqual(first, second)
        XCTAssertEqual(service.generatedKeys, 1)
        XCTAssertEqual(service.attestedKeys, 1)
        XCTAssertEqual(transport.registrations, 1)
        XCTAssertNil(transport.registrationBearerTokens[0])
        XCTAssertTrue(store.registered)
    }

    func testAssertionUsesServerChallengeAndCanonicalPayload() async throws {
        let service = FakeAttestService()
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let fixedDate = Date(timeIntervalSince1970: 1_700_000_000)
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            clock: { fixedDate }
        )
        let payload = FeedPayload(device: device, limit: 5, cursor: nil)
        let envelope = try await manager.makeAssertion(payload: payload)
        let clientData = try AppAttestClientData.make(
            challengeID: transport.challengeID,
            challenge: transport.challengeValue,
            payload: payload,
            issuedAt: 1_700_000_000
        )
        XCTAssertEqual(
            String(decoding: clientData, as: UTF8.self),
            #"{"body_sha256":"9234a406596b181890fa2bcfa54fd14d3849b8621336afc362fe16ba3dc58827","challenge":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","challenge_id":"00000000-0000-0000-0000-000000000001","issued_at":1700000000,"method":"POST","path":"/v1/app/feed","version":1}"#
        )
        XCTAssertEqual(service.lastAssertionHash, Data(SHA256.hash(data: clientData)))
        XCTAssertEqual(envelope.issuedAt, 1_700_000_000)
        XCTAssertEqual(transport.purposes, [.attestation, .assertion])

        let payloadJSON = String(decoding: try CanonicalJSON.encode(payload), as: UTF8.self)
        XCTAssertTrue(payloadJSON.contains(#""cursor":null"#))
        XCTAssertTrue(payloadJSON.contains(#""mode":"recent""#))
        XCTAssertTrue(payloadJSON.contains(#""exclude_article_ids":[]"#))
        XCTAssertTrue(payloadJSON.contains(#""vendor_id":null"#))
        XCTAssertTrue(payloadJSON.contains(INSTALLATION_ID.lowercased()))
    }

    func testDiscoveryAssertionBindsCanonicalPayloadToDiscoveryPath() async throws {
        let service = FakeAttestService()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: FakeKeyStore(),
            transport: transport,
            clock: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        let payload = DiscoveryPayload(device: device, query: nil, limit: 12)

        let envelope = try await manager.makeAssertion(payload: payload)
        let clientData = try AppAttestClientData.make(
            challengeID: transport.challengeID,
            challenge: transport.challengeValue,
            payload: payload,
            issuedAt: 1_700_000_000
        )

        XCTAssertEqual(
            String(decoding: clientData, as: UTF8.self),
            #"{"body_sha256":"40c16a40a591ece587f94b7d3f0116a249bde034c6443d016a08f6cee63bde3f","challenge":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","challenge_id":"00000000-0000-0000-0000-000000000001","issued_at":1700000000,"method":"POST","path":"/v1/app/discovery","version":1}"#
        )
        XCTAssertEqual(service.lastAssertionHash, Data(SHA256.hash(data: clientData)))
        XCTAssertEqual(envelope.issuedAt, 1_700_000_000)
    }

    func testPushAssertionBindsTokenAndInstallationToRegistrationPath() async throws {
        let service = FakeAttestService()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: FakeKeyStore(),
            transport: transport,
            clock: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        let payload = PushRegistrationPayload(
            installationID: device.installationID,
            deviceToken: "AQIDBA",
            environment: .development,
            device: device
        )

        _ = try await manager.makeAssertion(
            payload: payload,
            recoverySessionToken: "recovery-session-token"
        )
        let clientData = try AppAttestClientData.make(
            challengeID: transport.challengeID,
            challenge: transport.challengeValue,
            payload: payload,
            issuedAt: 1_700_000_000
        )
        let value = String(decoding: clientData, as: UTF8.self)

        XCTAssertTrue(value.contains(#""path":"/v1/app/push/registrations""#))
        XCTAssertTrue(value.contains(#""method":"POST""#))
        XCTAssertEqual(service.lastAssertionHash, Data(SHA256.hash(data: clientData)))
        XCTAssertEqual(
            transport.registrationBearerTokens,
            ["recovery-session-token"]
        )
    }

    func testPrivacyConsentAssertionBindsPolicyAndDeviceToConsentPath() async throws {
        let service = FakeAttestService()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: FakeKeyStore(),
            transport: transport,
            clock: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        let payload = PrivacyConsentPayload(policyVersion: "1.2.0", device: device)

        _ = try await manager.makeAssertion(payload: payload)
        let clientData = try AppAttestClientData.make(
            challengeID: transport.challengeID,
            challenge: transport.challengeValue,
            payload: payload,
            issuedAt: 1_700_000_000
        )
        let value = String(decoding: clientData, as: UTF8.self)

        XCTAssertTrue(value.contains(#""path":"/v1/app/privacy/consents""#))
        XCTAssertTrue(value.contains(#""method":"POST""#))
        XCTAssertEqual(service.lastAssertionHash, Data(SHA256.hash(data: clientData)))
        let payloadJSON = String(decoding: try CanonicalJSON.encode(payload), as: UTF8.self)
        XCTAssertTrue(payloadJSON.contains(#""policy_version":"1.2.0""#))
        XCTAssertTrue(payloadJSON.contains(INSTALLATION_ID.lowercased()))
    }

    func testArticleViewAssertionUsesDedicatedPath() async throws {
        let service = FakeAttestService()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: FakeKeyStore(),
            transport: transport,
            clock: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        let payload = ArticleViewPayload(
            eventID: UUID(
                uuidString: "33333333-3333-3333-3333-333333333333"
            )!,
            articleID: String(repeating: "a", count: 64),
            device: device
        )

        _ = try await manager.makeAssertion(payload: payload)
        let clientData = try AppAttestClientData.make(
            challengeID: transport.challengeID,
            challenge: transport.challengeValue,
            payload: payload,
            issuedAt: 1_700_000_000
        )
        let value = String(decoding: clientData, as: UTF8.self)

        XCTAssertTrue(value.contains(#""path":"/v1/app/article-views""#))
        XCTAssertTrue(value.contains(#""method":"POST""#))
        XCTAssertEqual(
            service.lastAssertionHash,
            Data(SHA256.hash(data: clientData))
        )
        XCTAssertThrowsError(
            try AppAttestClientData.make(
                challengeID: transport.challengeID,
                challenge: transport.challengeValue,
                payload: payload,
                issuedAt: 1_700_000_000,
                path: "/v1/app/article-views/other"
            )
        )
    }

    func testUnsupportedHasNoSharedSecretFallback() async {
        let service = FakeAttestService()
        service.isSupported = false
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: FakeKeyStore(),
            transport: transport
        )
        do {
            _ = try await manager.ensureRegistered(device: device)
            XCTFail("Expected unsupported error")
        } catch {
            XCTAssertEqual(error as? ArticleFeedError, .appAttestUnsupported)
        }
        XCTAssertTrue(transport.purposes.isEmpty)
    }

    func testResetRegistrationClearsStoredStateAndNextExplicitCallRegistersFreshKey() async throws {
        let service = FakeAttestService()
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport
        )

        _ = try await manager.ensureRegistered(device: device)
        XCTAssertEqual(service.generatedKeys, 1)
        XCTAssertTrue(store.registered)

        await manager.resetRegistration()
        XCTAssertNil(store.keyID)
        XCTAssertFalse(store.registered)
        XCTAssertEqual(service.generatedKeys, 1, "Reset must not retry automatically")

        _ = try await manager.ensureRegistered(device: device)
        XCTAssertEqual(service.generatedKeys, 2)
        XCTAssertEqual(service.attestedKeys, 2)
        XCTAssertEqual(transport.registrations, 2)
        XCTAssertTrue(store.registered)
    }

    func testServerUnavailableRetriesWithSameKeyAndHashThenRegisters() async throws {
        let service = FakeAttestService()
        service.attestationFailures = [.attestationServerUnavailable]
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let sleeps = SleepRecorder()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            sleep: { duration in await sleeps.record(duration) }
        )

        _ = try await manager.ensureRegistered(device: device)
        let recordedSleeps = await sleeps.durations

        XCTAssertEqual(service.attestedKeys, 2)
        XCTAssertEqual(Set(service.attestationKeyIDs), ["apple-app-attest-key-id"])
        XCTAssertEqual(Set(service.attestationHashes).count, 1)
        XCTAssertEqual(recordedSleeps, [1_000_000_000])
        XCTAssertEqual(transport.registrations, 1)
        XCTAssertTrue(store.registered)
    }

    func testServerUnavailableCanRecoverOnThirdAttemptWithBoundedBackoff() async throws {
        let service = FakeAttestService()
        service.attestationFailures = [
            .attestationServerUnavailable,
            .attestationServerUnavailable,
        ]
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let sleeps = SleepRecorder()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            sleep: { duration in await sleeps.record(duration) }
        )

        _ = try await manager.ensureRegistered(device: device)
        let recordedSleeps = await sleeps.durations

        XCTAssertEqual(service.attestedKeys, 3)
        XCTAssertEqual(Set(service.attestationKeyIDs), ["apple-app-attest-key-id"])
        XCTAssertEqual(Set(service.attestationHashes).count, 1)
        XCTAssertEqual(recordedSleeps, [1_000_000_000, 3_000_000_000])
        XCTAssertEqual(transport.registrations, 1)
        XCTAssertTrue(store.registered)
    }

    func testServerUnavailableRetriesAtMostThreeTimesAndPreservesKey() async {
        let service = FakeAttestService()
        service.attestationFailures = Array(
            repeating: .attestationServerUnavailable,
            count: 3
        )
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let sleeps = SleepRecorder()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            sleep: { duration in await sleeps.record(duration) }
        )

        do {
            _ = try await manager.ensureRegistered(device: device)
            XCTFail("Expected bounded attestation failure")
        } catch {
            XCTAssertEqual(error as? ArticleFeedError, .attestationUnavailable)
        }
        let recordedSleeps = await sleeps.durations

        XCTAssertEqual(service.attestedKeys, 3)
        XCTAssertEqual(Set(service.attestationKeyIDs), ["apple-app-attest-key-id"])
        XCTAssertEqual(Set(service.attestationHashes).count, 1)
        XCTAssertEqual(recordedSleeps, [1_000_000_000, 3_000_000_000])
        XCTAssertEqual(transport.registrations, 0)
        XCTAssertEqual(store.keyID, "apple-app-attest-key-id")
        XCTAssertFalse(store.registered)
    }

    func testFatalAttestationErrorClearsKeyWithoutRetry() async {
        let service = FakeAttestService()
        service.attestationFailures = [.invalidResponse]
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let sleeps = SleepRecorder()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            sleep: { duration in await sleeps.record(duration) }
        )

        do {
            _ = try await manager.ensureRegistered(device: device)
            XCTFail("Expected fatal attestation failure")
        } catch {
            XCTAssertEqual(error as? ArticleFeedError, .attestationUnavailable)
        }
        let recordedSleeps = await sleeps.durations

        XCTAssertEqual(service.attestedKeys, 1)
        XCTAssertEqual(recordedSleeps, [])
        XCTAssertEqual(transport.registrations, 0)
        XCTAssertNil(store.keyID)
        XCTAssertFalse(store.registered)
    }

    func testRetrySleepCancellationStopsWithoutClearingKey() async {
        let service = FakeAttestService()
        service.attestationFailures = [.attestationServerUnavailable]
        let store = FakeKeyStore()
        let transport = FakeAttestTransport()
        let manager = AppAttestManager(
            service: service,
            keyStore: store,
            transport: transport,
            sleep: { _ in throw CancellationError() }
        )

        do {
            _ = try await manager.ensureRegistered(device: device)
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }

        XCTAssertEqual(service.attestedKeys, 1)
        XCTAssertEqual(transport.registrations, 0)
        XCTAssertEqual(store.keyID, "apple-app-attest-key-id")
        XCTAssertFalse(store.registered)
    }
}

private let INSTALLATION_ID = "11111111-1111-1111-1111-111111111111"
