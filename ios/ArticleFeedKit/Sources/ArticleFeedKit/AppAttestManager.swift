import CryptoKit
import Foundation

#if canImport(DeviceCheck)
import DeviceCheck
#endif

public protocol AppAttestServicing: Sendable {
    var isSupported: Bool { get }
    func generateKey() async throws -> String
    func attestKey(_ keyID: String, clientDataHash: Data) async throws -> Data
    func generateAssertion(_ keyID: String, clientDataHash: Data) async throws -> Data
}

public protocol AppAttestKeyStoring: Sendable {
    func loadKeyID() -> String?
    func isRegistered() -> Bool
    func saveKeyID(_ keyID: String)
    func markRegistered()
    func clear()
}

public protocol AppAttestTransport: Sendable {
    func challenge(_ request: ChallengeRequest) async throws -> ChallengeResponse
    func register(
        _ request: RegisterRequest,
        bearerToken: String?
    ) async throws -> RegisterResponse
}

public enum ArticleFeedError: Error, Equatable, Sendable {
    case appAttestUnsupported
    case invalidChallenge
    case attestationUnavailable
    case attestationServerUnavailable
    case registrationRejected
    case invalidResponse
    case decoding
    case server(code: String, retryAfter: Double?)
}

public final class UserDefaultsAppAttestKeyStore: AppAttestKeyStoring, @unchecked Sendable {
    private let defaults: UserDefaults
    private let keyIDKey: String
    private let registeredKey: String

    public init(defaults: UserDefaults = .standard, namespace: String = "ArticleFeedKit") {
        self.defaults = defaults
        keyIDKey = "\(namespace).appAttestKeyID"
        registeredKey = "\(namespace).appAttestRegistered"
    }

    public func loadKeyID() -> String? { defaults.string(forKey: keyIDKey) }
    public func isRegistered() -> Bool { defaults.bool(forKey: registeredKey) }
    public func saveKeyID(_ keyID: String) { defaults.set(keyID, forKey: keyIDKey) }
    public func markRegistered() { defaults.set(true, forKey: registeredKey) }
    public func clear() {
        defaults.removeObject(forKey: keyIDKey)
        defaults.removeObject(forKey: registeredKey)
    }
}

public struct SystemAppAttestService: AppAttestServicing {
    public init() {}

    public var isSupported: Bool {
        #if canImport(DeviceCheck) && os(iOS)
        return DCAppAttestService.shared.isSupported
        #else
        return false
        #endif
    }

    public func generateKey() async throws -> String {
        #if canImport(DeviceCheck) && os(iOS)
        return try await withCheckedThrowingContinuation { continuation in
            DCAppAttestService.shared.generateKey { keyID, error in
                if let keyID { continuation.resume(returning: keyID) }
                else { continuation.resume(throwing: error ?? ArticleFeedError.attestationUnavailable) }
            }
        }
        #else
        throw ArticleFeedError.appAttestUnsupported
        #endif
    }

    public func attestKey(_ keyID: String, clientDataHash: Data) async throws -> Data {
        #if canImport(DeviceCheck) && os(iOS)
        return try await withCheckedThrowingContinuation { continuation in
            DCAppAttestService.shared.attestKey(keyID, clientDataHash: clientDataHash) {
                attestation, error in
                if let attestation { continuation.resume(returning: attestation) }
                else if let error, Self.isServerUnavailable(error) {
                    continuation.resume(throwing: ArticleFeedError.attestationServerUnavailable)
                } else {
                    continuation.resume(
                        throwing: error ?? ArticleFeedError.attestationUnavailable
                    )
                }
            }
        }
        #else
        throw ArticleFeedError.appAttestUnsupported
        #endif
    }

    public func generateAssertion(_ keyID: String, clientDataHash: Data) async throws -> Data {
        #if canImport(DeviceCheck) && os(iOS)
        return try await withCheckedThrowingContinuation { continuation in
            DCAppAttestService.shared.generateAssertion(keyID, clientDataHash: clientDataHash) {
                assertion, error in
                if let assertion { continuation.resume(returning: assertion) }
                else { continuation.resume(throwing: error ?? ArticleFeedError.attestationUnavailable) }
            }
        }
        #else
        throw ArticleFeedError.appAttestUnsupported
        #endif
    }

    #if canImport(DeviceCheck)
    static func isServerUnavailable(_ error: Error) -> Bool {
        let error = error as NSError
        return error.domain == DCError.errorDomain
            && error.code == DCError.Code.serverUnavailable.rawValue
    }
    #endif
}

public actor AppAttestManager {
    private let service: any AppAttestServicing
    private let keyStore: any AppAttestKeyStoring
    private let transport: any AppAttestTransport
    private let clock: @Sendable () -> Date
    private let sleep: @Sendable (UInt64) async throws -> Void

    private static let attestationRetryDelays: [UInt64] = [
        1_000_000_000,
        3_000_000_000,
    ]

    public init(
        service: any AppAttestServicing,
        keyStore: any AppAttestKeyStoring,
        transport: any AppAttestTransport,
        clock: @escaping @Sendable () -> Date = Date.init,
        sleep: @escaping @Sendable (UInt64) async throws -> Void = {
            try await Task.sleep(nanoseconds: $0)
        }
    ) {
        self.service = service
        self.keyStore = keyStore
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
    }

    public func ensureRegistered(device: DeviceInfo) async throws -> String {
        try await ensureRegistered(device: device, recoverySessionToken: nil)
    }

    public func ensureRegistered(
        device: DeviceInfo,
        recoverySessionToken: String?
    ) async throws -> String {
        guard service.isSupported else { throw ArticleFeedError.appAttestUnsupported }
        let keyID: String
        if let stored = keyStore.loadKeyID() {
            keyID = stored
        } else {
            do {
                keyID = try await service.generateKey()
                keyStore.saveKeyID(keyID)
            } catch {
                throw ArticleFeedError.attestationUnavailable
            }
        }
        if keyStore.isRegistered() { return keyID }

        let challenge = try await transport.challenge(
            ChallengeRequest(purpose: .attestation, keyID: keyID, device: device)
        )
        guard let challengeData = Base64URL.decode(challenge.challenge), challengeData.count == 32 else {
            throw ArticleFeedError.invalidChallenge
        }
        let digest = Data(SHA256.hash(data: challengeData))
        let attestation: Data
        do {
            attestation = try await attestWithRetry(
                keyID: keyID,
                clientDataHash: digest
            )
        } catch is CancellationError {
            throw CancellationError()
        } catch ArticleFeedError.attestationServerUnavailable {
            throw ArticleFeedError.attestationUnavailable
        } catch {
            keyStore.clear()
            throw ArticleFeedError.attestationUnavailable
        }
        #if DEBUG
        #endif
        let response = try await transport.register(
            RegisterRequest(
                challengeID: challenge.challengeID,
                keyID: keyID,
                attestationObject: Base64URL.encode(attestation),
                device: device
            ),
            bearerToken: recoverySessionToken
        )
        guard response.registered, response.keyID == keyID else {
            throw ArticleFeedError.registrationRejected
        }
        keyStore.markRegistered()
        return keyID
    }

    private func attestWithRetry(
        keyID: String,
        clientDataHash: Data
    ) async throws -> Data {
        for attempt in 0...Self.attestationRetryDelays.count {
            do {
                return try await service.attestKey(
                    keyID,
                    clientDataHash: clientDataHash
                )
            } catch ArticleFeedError.attestationServerUnavailable {
                guard attempt < Self.attestationRetryDelays.count else {
                    throw ArticleFeedError.attestationServerUnavailable
                }
                try await sleep(Self.attestationRetryDelays[attempt])
            }
        }
        throw ArticleFeedError.attestationServerUnavailable
    }

    public func makeAssertion(payload: FeedPayload) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            device: payload.device,
            path: "/v1/app/feed"
        )
    }

    public func makeAssertion(payload: DiscoveryPayload) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            device: payload.device,
            path: "/v1/app/discovery"
        )
    }

    public func makeAssertion(
        payload: PushRegistrationPayload,
        recoverySessionToken: String? = nil
    ) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            device: payload.device,
            path: "/v1/app/push/registrations",
            recoverySessionToken: recoverySessionToken
        )
    }

    public func makeAssertion(payload: PrivacyConsentPayload) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            device: payload.device,
            path: "/v1/app/privacy/consents"
        )
    }

    public func makeAssertion(payload: ArticleViewPayload) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            device: payload.device,
            path: "/v1/app/article-views"
        )
    }

    private func makeAssertion<Payload: Encodable>(
        payload: Payload,
        device: DeviceInfo,
        path: String,
        recoverySessionToken: String? = nil
    ) async throws -> AssertionEnvelope {
        let keyID = try await ensureRegistered(
            device: device,
            recoverySessionToken: recoverySessionToken
        )
        let challenge = try await transport.challenge(
            ChallengeRequest(purpose: .assertion, keyID: keyID, device: device)
        )
        guard let challengeData = Base64URL.decode(challenge.challenge), challengeData.count == 32 else {
            throw ArticleFeedError.invalidChallenge
        }
        let issuedAt = Int(clock().timeIntervalSince1970)
        let clientData = try AppAttestClientData.make(
            challengeID: challenge.challengeID,
            challenge: challenge.challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: path
        )
        let digest = Data(SHA256.hash(data: clientData))
        let assertion: Data
        do {
            assertion = try await service.generateAssertion(keyID, clientDataHash: digest)
        } catch {
            throw ArticleFeedError.attestationUnavailable
        }
        #if DEBUG
        #endif
        return AssertionEnvelope(
            challengeID: challenge.challengeID,
            keyID: keyID,
            assertion: Base64URL.encode(assertion),
            issuedAt: issuedAt
        )
    }

    public func resetRegistration() async {
        keyStore.clear()
    }
}

enum AppAttestClientData {
    private struct Value: Encodable {
        let bodySHA256: String
        let challenge: String
        let challengeID: String
        let issuedAt: Int
        let method: String
        let path: String
        let version: Int

        enum CodingKeys: String, CodingKey {
            case bodySHA256 = "body_sha256"
            case challenge
            case challengeID = "challenge_id"
            case issuedAt = "issued_at"
            case method
            case path
            case version
        }
    }

    static func make(
        challengeID: UUID,
        challenge: String,
        payload: FeedPayload,
        issuedAt: Int
    ) throws -> Data {
        try make(
            challengeID: challengeID,
            challenge: challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: "/v1/app/feed"
        )
    }

    static func make(
        challengeID: UUID,
        challenge: String,
        payload: DiscoveryPayload,
        issuedAt: Int
    ) throws -> Data {
        try make(
            challengeID: challengeID,
            challenge: challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: "/v1/app/discovery"
        )
    }

    static func make(
        challengeID: UUID,
        challenge: String,
        payload: PushRegistrationPayload,
        issuedAt: Int
    ) throws -> Data {
        try make(
            challengeID: challengeID,
            challenge: challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: "/v1/app/push/registrations"
        )
    }

    static func make(
        challengeID: UUID,
        challenge: String,
        payload: PrivacyConsentPayload,
        issuedAt: Int
    ) throws -> Data {
        try make(
            challengeID: challengeID,
            challenge: challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: "/v1/app/privacy/consents"
        )
    }

    static func make(
        challengeID: UUID,
        challenge: String,
        payload: ArticleViewPayload,
        issuedAt: Int
    ) throws -> Data {
        try make(
            challengeID: challengeID,
            challenge: challenge,
            payload: payload,
            issuedAt: issuedAt,
            path: "/v1/app/article-views"
        )
    }

    static func make<Payload: Encodable>(
        challengeID: UUID,
        challenge: String,
        payload: Payload,
        issuedAt: Int,
        path: String
    ) throws -> Data {
        guard path == "/v1/app/feed"
                || path == "/v1/app/discovery"
                || path == "/v1/app/push/registrations"
                || path == "/v1/app/privacy/consents"
                || path == "/v1/app/article-views" else {
            throw ArticleFeedError.invalidResponse
        }
        let payloadData = try CanonicalJSON.encode(payload)
        let payloadHash = SHA256.hash(data: payloadData).map { String(format: "%02x", $0) }.joined()
        return try CanonicalJSON.encode(
            Value(
                bodySHA256: payloadHash,
                challenge: challenge,
                challengeID: challengeID.uuidString.lowercased(),
                issuedAt: issuedAt,
                method: "POST",
                path: path,
                version: 1
            )
        )
    }
}

enum Base64URL {
    static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func decode(_ value: String) -> Data? {
        var normalized = value
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        normalized += String(repeating: "=", count: (4 - normalized.count % 4) % 4)
        return Data(base64Encoded: normalized)
    }
}
