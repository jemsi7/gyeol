import Foundation
import XCTest
@testable import ArticleFeedKit

private final class MockURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: ArticleFeedError.invalidResponse)
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}

private func requestBody(_ request: URLRequest) throws -> Data {
    if let body = request.httpBody { return body }
    guard let stream = request.httpBodyStream else {
        throw ArticleFeedError.invalidResponse
    }
    stream.open()
    defer { stream.close() }
    var body = Data()
    var buffer = [UInt8](repeating: 0, count: 1_024)
    while stream.hasBytesAvailable {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count < 0 { throw stream.streamError ?? ArticleFeedError.invalidResponse }
        if count == 0 { break }
        body.append(contentsOf: buffer.prefix(count))
    }
    return body
}

final class ArticleAPIClientTests: XCTestCase {
    private func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func payload() -> FeedPayload {
        FeedPayload(
            device: DeviceInfo(
                installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
                vendorID: nil,
                platform: .ipados,
                model: "iPad14,5",
                osVersion: "26.0",
                appVersion: "1.0",
                buildNumber: "1",
                locale: "ko-KR"
            )
        )
    }

    private func auth() -> AssertionEnvelope {
        AssertionEnvelope(
            challengeID: UUID(),
            keyID: "apple-app-attest-key-id",
            assertion: "YXNzZXJ0aW9uLW9iamVjdA",
            issuedAt: 1_700_000_000
        )
    }

    func testClientDecodesFeedResponse() async throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "feed_response", withExtension: "json")
        )
        let fixture = try Data(contentsOf: fixtureURL)
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/app/feed")
            XCTAssertEqual(request.httpMethod, "POST")
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!,
                fixture
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let response = try await client.loadFeed(payload: payload(), auth: auth())
        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.items[0].map.images.count, 2)
    }

    func testAttestRegistrationSendsBearerOnlyWhenProvided() async throws {
        var authorizationHeaders: [String?] = []
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/app/attest/register")
            authorizationHeaders.append(
                request.value(forHTTPHeaderField: "Authorization")
            )
            let decoded = try APIJSON.makeDecoder().decode(
                RegisterRequest.self,
                from: requestBody(request)
            )
            let response = "{\"registered\":true,\"key_id\":\"\(decoded.keyID)\"}"
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!,
                Data(response.utf8)
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let request = RegisterRequest(
            challengeID: UUID(),
            keyID: "apple-app-attest-key-id",
            attestationObject: "YXR0ZXN0YXRpb24tb2JqZWN0",
            device: payload().device
        )

        _ = try await client.register(
            request,
            bearerToken: "recovery-session-token"
        )
        _ = try await client.register(request, bearerToken: nil)

        XCTAssertEqual(authorizationHeaders.count, 2)
        XCTAssertEqual(authorizationHeaders[0], "Bearer recovery-session-token")
        XCTAssertNil(authorizationHeaders[1])
    }

    func testClientPostsAuthenticatedDiscoveryAndDecodesCatalog() async throws {
        let fixture = try Data(contentsOf: DiscoveryTestFixture.url)
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/app/discovery")
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try requestBody(request)
            let decoded = try APIJSON.makeDecoder().decode(
                AuthenticatedDiscoveryRequest.self,
                from: body
            )
            XCTAssertNil(decoded.payload.query)
            XCTAssertEqual(decoded.payload.limit, 12)
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!,
                fixture
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let discoveryPayload = DiscoveryPayload(
            device: payload().device,
            query: nil,
            limit: 12
        )

        let response = try await client.loadDiscovery(
            payload: discoveryPayload,
            auth: auth()
        )

        XCTAssertEqual(response.routes.count, 2)
        XCTAssertEqual(response.representativeRoute?.rank, 1)
    }

    func testClientRegistersAndUnregistersPushWithAccountBearer() async throws {
        let installationID = payload().device.installationID
        let token = "AQIDBA"
        var requestedPaths: [String] = []
        MockURLProtocol.handler = { request in
            requestedPaths.append(request.url?.path ?? "")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer opaque-session")
            if request.httpMethod == "POST" {
                let decoded = try APIJSON.makeDecoder().decode(
                    AuthenticatedPushRegistrationRequest.self,
                    from: requestBody(request)
                )
                XCTAssertEqual(decoded.payload.installationID, installationID)
                XCTAssertEqual(decoded.payload.deviceToken, token)
                XCTAssertEqual(decoded.payload.environment, .development)
                XCTAssertEqual(decoded.payload.preferences, .editorialOnly)
                let response = #"{"active":true,"installation_id":"\#(installationID.uuidString.lowercased())","environment":"development","preferences":{"editorial":true,"weekly_insight":false}}"#
                return (
                    HTTPURLResponse(
                        url: request.url!,
                        statusCode: 200,
                        httpVersion: nil,
                        headerFields: ["Content-Type": "application/json"]
                    )!,
                    Data(response.utf8)
                )
            }
            if request.httpMethod == "PATCH" {
                let preferences = try APIJSON.makeDecoder().decode(
                    PushNotificationPreferences.self,
                    from: requestBody(request)
                )
                XCTAssertEqual(
                    preferences,
                    PushNotificationPreferences(editorial: true, weeklyInsight: true)
                )
                let response = #"{"active":true,"installation_id":"\#(installationID.uuidString.lowercased())","environment":"development","preferences":{"editorial":true,"weekly_insight":true}}"#
                return (
                    HTTPURLResponse(
                        url: request.url!,
                        statusCode: 200,
                        httpVersion: nil,
                        headerFields: ["Content-Type": "application/json"]
                    )!,
                    Data(response.utf8)
                )
            }
            XCTAssertEqual(request.httpMethod, "DELETE")
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 204,
                    httpVersion: nil,
                    headerFields: nil
                )!,
                Data()
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let pushPayload = PushRegistrationPayload(
            installationID: installationID,
            deviceToken: token,
            environment: .development,
            device: payload().device
        )

        let registered = try await client.registerPush(
            payload: pushPayload,
            auth: auth(),
            sessionToken: "opaque-session"
        )
        let updated = try await client.updatePushPreferences(
            installationID: installationID,
            preferences: PushNotificationPreferences(editorial: true, weeklyInsight: true),
            sessionToken: "opaque-session"
        )
        try await client.unregisterPush(
            installationID: installationID,
            sessionToken: "opaque-session"
        )

        XCTAssertTrue(registered.active)
        XCTAssertEqual(registered.preferences, .editorialOnly)
        XCTAssertEqual(
            updated.preferences,
            PushNotificationPreferences(editorial: true, weeklyInsight: true)
        )
        XCTAssertEqual(
            requestedPaths,
            [
                "/v1/app/push/registrations",
                "/v1/app/push/registrations/\(installationID.uuidString.lowercased())",
                "/v1/app/push/registrations/\(installationID.uuidString.lowercased())",
            ]
        )
    }

    func testClientPostsAttestedPrivacyConsentAndDecodesReceipt() async throws {
        let consentID = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/app/privacy/consents")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            let decoded = try APIJSON.makeDecoder().decode(
                AuthenticatedPrivacyConsentRequest.self,
                from: requestBody(request)
            )
            XCTAssertEqual(decoded.payload.policyVersion, "1.2.0")
            XCTAssertEqual(decoded.payload.device, self.payload().device)
            let response = #"{"consent_id":"\#(consentID.uuidString.lowercased())","policy_version":"1.2.0","policy_url":"https://example.invalid/privacy","accepted_at":"2026-07-22T12:00:00Z"}"#
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!,
                Data(response.utf8)
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let consentPayload = PrivacyConsentPayload(
            policyVersion: "1.2.0",
            device: payload().device
        )

        let receipt = try await client.recordPrivacyConsent(
            payload: consentPayload,
            auth: auth()
        )

        XCTAssertEqual(receipt.consentID, consentID)
        XCTAssertEqual(receipt.policyVersion, "1.2.0")
        XCTAssertEqual(
            receipt.policyURL.absoluteString,
            "https://example.invalid/privacy"
        )
        XCTAssertEqual(receipt.acceptedAt.timeIntervalSince1970, 1_784_721_600)
    }

    func testClientPostsAuthenticatedArticleViewWithoutBearerAndDecodesCounter() async throws {
        let eventID = UUID(
            uuidString: "33333333-3333-3333-3333-333333333333"
        )!
        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/app/article-views")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            let decoded = try APIJSON.makeDecoder().decode(
                AuthenticatedArticleViewRequest.self,
                from: requestBody(request)
            )
            XCTAssertEqual(decoded.payload.eventID, eventID)
            XCTAssertEqual(decoded.payload.articleID, String(repeating: "a", count: 64))
            XCTAssertEqual(decoded.payload.device, self.payload().device)
            let response = """
            {"article_id":"\(String(repeating: "a", count: 64))","counted":true,"view_count":42}
            """
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!,
                Data(response.utf8)
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        let articleViewPayload = ArticleViewPayload(
            eventID: eventID,
            articleID: String(repeating: "a", count: 64),
            device: payload().device
        )

        let response = try await client.recordArticleView(
            payload: articleViewPayload,
            auth: auth()
        )

        XCTAssertEqual(response.articleID, String(repeating: "a", count: 64))
        XCTAssertTrue(response.counted)
        XCTAssertEqual(response.viewCount, 42)
    }

    func testArticleViewWireUsesSnakeCaseAndValidatesResponse() throws {
        let eventID = UUID(
            uuidString: "33333333-3333-3333-3333-333333333333"
        )!
        let event = ArticleViewEvent(
            eventID: eventID,
            articleID: String(repeating: "a", count: 64)
        )
        let encoded = String(
            decoding: try CanonicalJSON.encode(event),
            as: UTF8.self
        )

        XCTAssertTrue(event.isValid)
        XCTAssertEqual(
            encoded,
            #"{"article_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","event_id":"33333333-3333-3333-3333-333333333333"}"#
        )
        XCTAssertFalse(
            ArticleViewEvent(
                eventID: eventID,
                articleID: String(repeating: "A", count: 64)
            ).isValid
        )
        XCTAssertThrowsError(
            try APIJSON.makeDecoder().decode(
                ArticleViewResponse.self,
                from: Data(
                    #"{"article_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","counted":true,"view_count":-1}"#.utf8
                )
            )
        )
    }

    func testClientDecodesCooldownErrorAndRetryHeader() async throws {
        MockURLProtocol.handler = { request in
            let data = Data(
                #"{"error":{"code":"feed_cooldown","message":"feed cooldown","retry_after_seconds":1.25}}"#.utf8
            )
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 429,
                    httpVersion: nil,
                    headerFields: [
                        "Content-Type": "application/json",
                        "Retry-After": "1.250",
                    ]
                )!,
                data
            )
        }
        let client = try ArticleAPIClient(
            baseURL: URL(string: "https://api.example")!,
            session: session()
        )
        do {
            _ = try await client.loadFeed(payload: payload(), auth: auth())
            XCTFail("Expected cooldown")
        } catch let ArticleFeedError.server(code, retryAfter) {
            XCTAssertEqual(code, "feed_cooldown")
            XCTAssertEqual(retryAfter, 1.25)
        }
    }
}
