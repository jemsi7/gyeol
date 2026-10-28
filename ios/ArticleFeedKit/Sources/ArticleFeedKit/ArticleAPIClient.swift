import Foundation

public protocol ArticleAPIClientProtocol: Sendable {
    func loadFeed(payload: FeedPayload, auth: AssertionEnvelope) async throws -> FeedResponse
}

public protocol DiscoveryAPIClientProtocol: Sendable {
    func loadDiscovery(
        payload: DiscoveryPayload,
        auth: AssertionEnvelope
    ) async throws -> DiscoveryResponse
}

public protocol PushRegistrationAPIClientProtocol: Sendable {
    func registerPush(
        payload: PushRegistrationPayload,
        auth: AssertionEnvelope,
        sessionToken: String
    ) async throws -> PushRegistrationResponse
    func updatePushPreferences(
        installationID: UUID,
        preferences: PushNotificationPreferences,
        sessionToken: String
    ) async throws -> PushRegistrationResponse
    func unregisterPush(installationID: UUID, sessionToken: String) async throws
}

public protocol PrivacyConsentAPIClientProtocol: Sendable {
    func recordPrivacyConsent(
        payload: PrivacyConsentPayload,
        auth: AssertionEnvelope
    ) async throws -> PrivacyConsentReceipt
}

public protocol ArticleViewAPIClientProtocol: Sendable {
    func recordArticleView(
        payload: ArticleViewPayload,
        auth: AssertionEnvelope
    ) async throws -> ArticleViewResponse
}

public final class ArticleAPIClient: AppAttestTransport, ArticleAPIClientProtocol, DiscoveryAPIClientProtocol, PushRegistrationAPIClientProtocol, PrivacyConsentAPIClientProtocol, ArticleViewAPIClientProtocol, @unchecked Sendable {
    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) throws {
        guard
            ["http", "https"].contains(baseURL.scheme?.lowercased() ?? ""),
            baseURL.host != nil,
            baseURL.user == nil,
            baseURL.password == nil
        else { throw ArticleFeedError.invalidResponse }
        self.baseURL = baseURL
        self.session = session
    }

    public func challenge(_ request: ChallengeRequest) async throws -> ChallengeResponse {
        try await post(path: "/v1/app/attest/challenge", body: request)
    }

    public func register(
        _ request: RegisterRequest,
        bearerToken: String?
    ) async throws -> RegisterResponse {
        try await post(
            path: "/v1/app/attest/register",
            body: request,
            bearerToken: bearerToken
        )
    }

    public func loadFeed(
        payload: FeedPayload,
        auth: AssertionEnvelope
    ) async throws -> FeedResponse {
        try await post(
            path: "/v1/app/feed",
            body: AuthenticatedFeedRequest(payload: payload, auth: auth)
        )
    }

    public func loadDiscovery(
        payload: DiscoveryPayload,
        auth: AssertionEnvelope
    ) async throws -> DiscoveryResponse {
        try await post(
            path: "/v1/app/discovery",
            body: AuthenticatedDiscoveryRequest(payload: payload, auth: auth)
        )
    }

    public func recordPrivacyConsent(
        payload: PrivacyConsentPayload,
        auth: AssertionEnvelope
    ) async throws -> PrivacyConsentReceipt {
        try await post(
            path: "/v1/app/privacy/consents",
            body: AuthenticatedPrivacyConsentRequest(payload: payload, auth: auth)
        )
    }

    public func recordArticleView(
        payload: ArticleViewPayload,
        auth: AssertionEnvelope
    ) async throws -> ArticleViewResponse {
        try await post(
            path: "/v1/app/article-views",
            body: AuthenticatedArticleViewRequest(payload: payload, auth: auth)
        )
    }

    public func registerPush(
        payload: PushRegistrationPayload,
        auth: AssertionEnvelope,
        sessionToken: String
    ) async throws -> PushRegistrationResponse {
        try await post(
            path: "/v1/app/push/registrations",
            body: AuthenticatedPushRegistrationRequest(payload: payload, auth: auth),
            bearerToken: sessionToken
        )
    }

    public func unregisterPush(installationID: UUID, sessionToken: String) async throws {
        guard let url = URL(
            string: "/v1/app/push/registrations/\(installationID.uuidString.lowercased())",
            relativeTo: baseURL
        )?.absoluteURL else {
            throw ArticleFeedError.invalidResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
        _ = try await perform(request)
    }

    public func updatePushPreferences(
        installationID: UUID,
        preferences: PushNotificationPreferences,
        sessionToken: String
    ) async throws -> PushRegistrationResponse {
        guard let url = URL(
            string: "/v1/app/push/registrations/\(installationID.uuidString.lowercased())",
            relativeTo: baseURL
        )?.absoluteURL else {
            throw ArticleFeedError.invalidResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = try CanonicalJSON.encode(preferences)
        let (data, http) = try await perform(request)
        if let contentType = http.value(forHTTPHeaderField: "Content-Type"),
           !contentType.lowercased().contains("application/json") {
            throw ArticleFeedError.invalidResponse
        }
        do {
            return try APIJSON.makeDecoder().decode(PushRegistrationResponse.self, from: data)
        } catch {
            throw ArticleFeedError.decoding
        }
    }

    private func post<RequestBody: Encodable, ResponseBody: Decodable>(
        path: String,
        body: RequestBody,
        bearerToken: String? = nil
    ) async throws -> ResponseBody {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw ArticleFeedError.invalidResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bearerToken {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = try CanonicalJSON.encode(body)

        let (data, http) = try await perform(request)
        if let contentType = http.value(forHTTPHeaderField: "Content-Type"),
           !contentType.lowercased().contains("application/json") {
            throw ArticleFeedError.invalidResponse
        }
        do {
            return try APIJSON.makeDecoder().decode(ResponseBody.self, from: data)
        } catch {
            throw ArticleFeedError.decoding
        }
    }

    private func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw ArticleFeedError.invalidResponse
        }
        guard let http = response as? HTTPURLResponse else {
            throw ArticleFeedError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let envelope = try? APIJSON.makeDecoder().decode(APIErrorEnvelope.self, from: data)
            let headerRetry = http.value(forHTTPHeaderField: "Retry-After").flatMap(Double.init)
            throw ArticleFeedError.server(
                code: envelope?.error.code ?? "http_\(http.statusCode)",
                retryAfter: envelope?.error.retryAfterSeconds ?? headerRetry
            )
        }
        return (data, http)
    }
}
