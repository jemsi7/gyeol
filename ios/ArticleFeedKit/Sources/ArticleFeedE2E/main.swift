import ArticleFeedKit
import Foundation

private final class E2EAppAttestService: AppAttestServicing, @unchecked Sendable {
    let isSupported = true
    func generateKey() async throws -> String { "e2e-app-attest-key-id" }
    func attestKey(_ keyID: String, clientDataHash: Data) async throws -> Data {
        Data("e2e-attestation-object".utf8)
    }
    func generateAssertion(_ keyID: String, clientDataHash: Data) async throws -> Data {
        Data("e2e-assertion-object".utf8)
    }
}

private final class E2EKeyStore: AppAttestKeyStoring, @unchecked Sendable {
    private var keyID: String?
    private var registered = false
    func loadKeyID() -> String? { keyID }
    func isRegistered() -> Bool { registered }
    func saveKeyID(_ keyID: String) { self.keyID = keyID }
    func markRegistered() { registered = true }
    func clear() { keyID = nil; registered = false }
}

@main
struct ArticleFeedE2E {
    static func main() async throws {
        guard
            let rawBaseURL = ProcessInfo.processInfo.environment["APP_E2E_BASE_URL"],
            let baseURL = URL(string: rawBaseURL)
        else {
            throw ArticleFeedError.invalidResponse
        }
        let client = try ArticleAPIClient(baseURL: baseURL)
        let manager = AppAttestManager(
            service: E2EAppAttestService(),
            keyStore: E2EKeyStore(),
            transport: client
        )
        let device = DeviceInfo(
            installationID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
            vendorID: nil,
            platform: .ipados,
            model: "iPad14,5",
            osVersion: "26.0",
            appVersion: "1.0",
            buildNumber: "1",
            locale: "ko-KR"
        )
        let payload = FeedPayload(device: device, limit: 5, cursor: nil)
        let assertion = try await manager.makeAssertion(payload: payload)
        let response = try await client.loadFeed(payload: payload, auth: assertion)
        guard response.items.count == 1 else { throw ArticleFeedError.decoding }
        let item = response.items[0]
        let blocks = ArticleMapParser().parse(item.map)
        let environment = ProcessInfo.processInfo.environment
        if let expectedArticleID = environment["APP_E2E_EXPECTED_ARTICLE_ID"] {
            guard
                let expectedHeadline = environment["APP_E2E_EXPECTED_HEADLINE"],
                let rawSceneCount = environment["APP_E2E_EXPECTED_SCENE_COUNT"],
                let expectedSceneCount = Int(rawSceneCount),
                (3...9).contains(expectedSceneCount)
            else { throw ArticleFeedError.invalidResponse }
            let expectedSceneIDs = (1...expectedSceneCount).map {
                String(format: "S-%02d", $0)
            }
            guard
                item.articleID == expectedArticleID,
                item.map.formatVersion == 3,
                item.map.headline == expectedHeadline,
                item.map.scenes.count == expectedSceneCount,
                item.map.scenes.map(\.id) == expectedSceneIDs,
                item.map.scenes.allSatisfy({ !$0.title.isEmpty && !$0.label.isEmpty }),
                item.map.selectedClaimRefs.count == Set(item.map.selectedClaimRefs).count,
                Set(item.map.requiredClaimRefs).isSubset(of: Set(item.map.selectedClaimRefs)),
                item.map.editorial?.criticVerdict == "pass",
                !blocks.isEmpty
            else { throw ArticleFeedError.decoding }
        } else {
            guard
                item.map.formatVersion == 3,
                item.map.scenes.map(\.id) == ["S-01", "S-02", "S-03"],
                item.map.scenes.map(\.title) == [
                    "가장 뜨거운 길부터",
                    "보행자의 이동을 기록하다",
                    "머무는 방식을 바꾼 그늘",
                ],
                item.map.requiredClaimRefs == ["CL-001", "CL-002"],
                item.map.editorial?.criticVerdict == "pass",
                blocks.count == 5
            else { throw ArticleFeedError.decoding }
            guard case .markdown(let first) = blocks[0], first.contains("첫 문단") else {
                throw ArticleFeedError.decoding
            }
            guard case .image(let firstImage) = blocks[1], firstImage.ref == 0 else {
                throw ArticleFeedError.decoding
            }
            guard case .markdown(let middle) = blocks[2], middle.contains("둘째 문단") else {
                throw ArticleFeedError.decoding
            }
            guard case .image(let secondImage) = blocks[3], secondImage.ref == 1 else {
                throw ArticleFeedError.decoding
            }
            guard case .markdown(let last) = blocks[4], last.contains("마지막 문단") else {
                throw ArticleFeedError.decoding
            }
        }
        for image in item.map.images {
            guard image.url.scheme == "http", image.url.host == "127.0.0.1" else {
                throw ArticleFeedError.invalidResponse
            }
            let (data, response) = try await URLSession.shared.data(from: image.url)
            guard
                !data.isEmpty,
                (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type") == "image/png"
            else { throw ArticleFeedError.invalidResponse }
        }

        let secondAssertion = try await manager.makeAssertion(payload: payload)
        do {
            _ = try await client.loadFeed(payload: payload, auth: secondAssertion)
            throw ArticleFeedError.invalidResponse
        } catch let ArticleFeedError.server(code, retryAfter) {
            guard code == "feed_cooldown", retryAfter != nil else { throw ArticleFeedError.invalidResponse }
        }
        print(
            "E2E_OK article=\(item.articleID) map=v3 scenes=\(item.map.scenes.count) "
                + "blocks=\(blocks.count) images=\(item.map.images.count) cooldown=429"
        )
    }
}
