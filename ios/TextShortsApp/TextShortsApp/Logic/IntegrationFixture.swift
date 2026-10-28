#if DEBUG
import ArticleFeedKit
import Foundation
import UIKit

enum IntegrationFixture {
    @MainActor
    static func environment(
        boundaryWidthRegression: Bool = false,
        homeMediaLayoutRegression: Bool = false,
        homeWidthLayoutRegression: Bool = false,
        homeHub: Bool = false,
        cryptoMarketing: Int = 0,
        accountService: any AccountServicing = MemoryAccountService(),
        editorialPushService: (any EditorialPushServicing)? = nil
    ) -> AppEnvironment {
        do {
            let items = try makeItems(
                boundaryWidthRegression: boundaryWidthRegression,
                homeHub: homeHub,
                cryptoMarketing: cryptoMarketing
            )
            let discoveryCatalog = try makeDiscoveryResponse(from: items)
            let discoverySearch = DiscoveryResponse(
                generatedAt: discoveryCatalog.generatedAt,
                expiresAt: discoveryCatalog.expiresAt,
                representativeRouteID: discoveryCatalog.representativeRouteID,
                routes: discoveryCatalog.routes,
                searchSuggestions: discoveryCatalog.searchSuggestions,
                trendingSearchSuggestions: discoveryCatalog.trendingSearchSuggestions,
                query: "도시 변화",
                results: [
                    DiscoverySearchResult(
                        depth: .quick,
                        article: discoveryCatalog.routes[0].items[0].article
                    ),
                    DiscoverySearchResult(
                        depth: .deep,
                        article: discoveryCatalog.routes[1].items[0].article
                    ),
                    DiscoverySearchResult(
                        depth: .quick,
                        retrieval: .rag,
                        article: discoveryCatalog.routes[0].items[1].article
                    ),
                ],
                cooldownSeconds: 1,
                timelineResults: Array(discoveryCatalog.featuredTimelines.prefix(1))
            )
            let discoveryLoader = StubDiscoveryLoader(
                response: discoveryCatalog,
                state: .loaded
            )
            discoveryLoader.catalogResult = (discoveryCatalog, .loaded)
            discoveryLoader.searchResult = (discoverySearch, .loaded)
            discoveryLoader.editorialResult = (
                DiscoveryResponse(
                    generatedAt: discoverySearch.generatedAt,
                    expiresAt: discoverySearch.expiresAt,
                    representativeRouteID: discoverySearch.representativeRouteID,
                    routes: discoverySearch.routes,
                    searchSuggestions: discoverySearch.searchSuggestions,
                    trendingSearchSuggestions: discoverySearch.trendingSearchSuggestions,
                    query: nil,
                    results: discoverySearch.results,
                    cooldownSeconds: 1,
                    collection: .editorial,
                    nextCursor: nil
                ),
                .loaded
            )
            discoveryLoader.weeklyInsightResult = (
                DiscoveryResponse(
                    generatedAt: discoverySearch.generatedAt,
                    expiresAt: discoverySearch.expiresAt,
                    representativeRouteID: discoverySearch.representativeRouteID,
                    routes: discoverySearch.routes,
                    searchSuggestions: discoverySearch.searchSuggestions,
                    trendingSearchSuggestions: discoverySearch.trendingSearchSuggestions,
                    query: nil,
                    results: [
                        DiscoverySearchResult(
                            depth: .quick,
                            article: discoveryCatalog.routes[0].items[1].article
                        ),
                        DiscoverySearchResult(
                            depth: .deep,
                            article: discoveryCatalog.routes[1].items[1].article
                        ),
                    ],
                    cooldownSeconds: 1,
                    collection: .weeklyInsight,
                    nextCursor: nil
                ),
                .loaded
            )
            let previewURL = URL(string: "https://images.example/integration-2.jpg")!
            let homeURL = URL(string: "https://images.example/integration-1.jpg")!
            let previewData = makeWidePreviewImageData()
            var cachedImages = [previewURL: previewData]
            let discoveryThumbnailAssets = [
                (1, "HomeCoast"),
                (5, "HomeForest"),
            ]
            for (articleNumber, asset) in discoveryThumbnailAssets {
                let url = URL(string: "https://images.example/integration-\(articleNumber).jpg")!
                if let data = UIImage(named: asset)?.pngData() {
                    cachedImages[url] = data
                }
            }
            if homeMediaLayoutRegression {
                cachedImages[homeURL] = makeHomeMediaImageData()
            }
            if homeWidthLayoutRegression {
                cachedImages[homeURL] = makeHomeWideMediaImageData()
            }
            if cryptoMarketing > 0 {
                // App Store 캡처에 QA 합성 이미지(파란 사각형)가 들어가면 안 된다.
                let assets = ["HomeCity", "HomeCoast", "HomeForest"]
                for (index, asset) in assets.enumerated() {
                    let url = URL(string: "https://images.example/integration-\(index + 1).jpg")!
                    if let data = UIImage(named: asset)?.pngData() {
                        cachedImages[url] = data
                    }
                }
            }
            if homeHub {
                let assets = ["HomeCoast", "HomeForest", "HomeCity"]
                for (index, asset) in assets.enumerated() {
                    let url = URL(string: "https://images.example/integration-\(index + 1).jpg")!
                    if let data = UIImage(named: asset)?.pngData() {
                        cachedImages[url] = data
                    }
                }
            }
            var snapshot = PersistedAppSnapshot.empty
            snapshot.hasCompletedOnboarding = true
            snapshot.hasSeenReaderGuide = true
            let bundleVersion = (Bundle.main.object(
                forInfoDictionaryKey: "CFBundleShortVersionString"
            ) as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            snapshot.lastPresentedAppVersion = bundleVersion?.isEmpty == false
                ? bundleVersion
                : "1.0"
            snapshot.acceptedPrivacyPolicyVersion = PrivacyPolicy.currentVersion
            snapshot.privacyConsentReceiptID = UUID(
                uuidString: "33333333-3333-3333-3333-333333333333"
            )
            snapshot.cachedDiscovery = discoveryCatalog
            if homeHub || boundaryWidthRegression {
                snapshot.cachedFeed = Array(items.prefix(5))
                if homeHub {
                    snapshot.sceneProgress = ["integration-2": 0]
                    snapshot.lastArticleID = "integration-2"
                } else {
                    snapshot.lastArticleID = "integration-1"
                }
            }
            let latestResults: [StubFeedPage] = homeHub
                ? [
                    StubFeedPage(
                        items: [items[4], items[3]],
                        state: .loaded,
                        hasMorePages: false
                    )
                ]
                : []
            return AppEnvironment(
                loader: StubArticleFeedLoader(
                    items: items,
                    state: .loaded,
                    hasMorePages: homeHub,
                    latestResults: latestResults
                ),
                discoveryLoader: discoveryLoader,
                persistence: MemoryAppPersistence(snapshot: snapshot),
                mediaCache: MemoryArticleMediaCache(values: cachedImages),
                connectivity: StubConnectivityMonitor(states: [.online]),
                accountService: accountService,
                editorialPushService: editorialPushService
            )
        } catch {
            return AppEnvironment(
                loader: UnavailableArticleFeedLoader(code: "integration_fixture_invalid"),
                persistence: MemoryAppPersistence(),
                mediaCache: MemoryArticleMediaCache(),
                connectivity: NoopConnectivityMonitor(),
                accountService: accountService,
                editorialPushService: editorialPushService
            )
        }
    }

    @MainActor
    private static func makeWidePreviewImageData() -> Data {
        let size = CGSize(width: 1_200, height: 630)
        let format = UIGraphicsImageRendererFormat()
        format.opaque = true
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).pngData { context in
            let bounds = CGRect(origin: .zero, size: size)
            context.cgContext.setFillColor(UIColor(red: 0.03, green: 0.24, blue: 0.91, alpha: 1).cgColor)
            context.cgContext.fill(bounds)
            context.cgContext.setFillColor(UIColor(red: 0.95, green: 0.35, blue: 0.16, alpha: 1).cgColor)
            context.cgContext.fill(CGRect(x: 0, y: 0, width: 36, height: size.height))
            context.cgContext.fill(CGRect(x: size.width - 36, y: 0, width: 36, height: size.height))
        }
    }

    @MainActor
    private static func makeHomeMediaImageData() -> Data {
        let size = CGSize(width: 1_200, height: 800)
        let format = UIGraphicsImageRendererFormat()
        format.opaque = true
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).pngData { context in
            let bounds = CGRect(origin: .zero, size: size)
            context.cgContext.setFillColor(UIColor(red: 0.03, green: 0.24, blue: 0.91, alpha: 1).cgColor)
            context.cgContext.fill(bounds)
            context.cgContext.setFillColor(UIColor(red: 0.95, green: 0.35, blue: 0.16, alpha: 1).cgColor)
            context.cgContext.fill(CGRect(x: 0, y: 0, width: 36, height: size.height))
            context.cgContext.fill(CGRect(x: size.width - 36, y: 0, width: 36, height: size.height))
        }
    }

    @MainActor
    private static func makeHomeWideMediaImageData() -> Data {
        let size = CGSize(width: 1_200, height: 480)
        let format = UIGraphicsImageRendererFormat()
        format.opaque = true
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).pngData { context in
            let bounds = CGRect(origin: .zero, size: size)
            context.cgContext.setFillColor(UIColor(red: 0.03, green: 0.24, blue: 0.91, alpha: 1).cgColor)
            context.cgContext.fill(bounds)
            context.cgContext.setFillColor(UIColor(red: 0.95, green: 0.35, blue: 0.16, alpha: 1).cgColor)
            context.cgContext.fill(CGRect(x: 0, y: 0, width: 120, height: size.height))
            context.cgContext.fill(CGRect(x: size.width - 120, y: 0, width: 120, height: size.height))
        }
    }

    static func makeItems(
        boundaryWidthRegression: Bool = false,
        homeHub: Bool = false,
        cryptoMarketing: Int = 0
    ) throws -> [ArticleFeedItem] {
        let itemJSON = #"""
        {
          "article_id": "integration-1",
          "source_url": "https://news.example/integration-1",
          "domain": "news.example",
          "published_at": "2026-07-11T08:40:00Z",
          "source_language": "Korean",
          "source_country": "South Korea",
          "status": "ok",
          "map": {
            "format_version": 3,
            "language": "ko",
            "headline": "도시의 그늘이 만드는 느린 변화",
            "subheadline": "작은 설계가 여름의 이동을 바꾼다",
            "summary": "보행자가 잠시 머물 수 있는 그늘이 도시의 여름 풍경을 바꾸고 있다.",
            "keywords": ["도시", "그늘", "공공디자인"],
            "body_markdown": "첫 문단입니다. 가장 뜨거운 길부터 작은 그늘이 놓였습니다.\n\n<img:0>\n\n둘째 문단입니다. 사람들은 전보다 천천히 걷고 더 오래 머물렀습니다.\n\n> 좋은 변화는 머물 자리를 만드는 일에서 시작됩니다.\n\n마지막 문단입니다. 다음 계절을 위한 기록이 이어집니다.",
            "body_resolved": null,
            "images": [{
              "ref": 0,
              "image_id": "integration-image-1",
              "url": "https://images.example/integration-1.jpg",
              "source_url": "https://news.example/images/1",
              "role": "content",
              "caption": "거리 위에 놓인 이동식 그늘"
            }],
            "used_refs": [0],
            "visualizations": [],
            "used_chart_refs": []
          }
        }
        """#
        let baseData = Data(itemJSON.utf8)
        let base = try JSONSerialization.jsonObject(with: baseData) as! [String: Any]
        let configurations: [(String, [String])] = [
            ("bar", ["bar"]),
            ("line", ["line"]),
            ("area", ["area"]),
            ("pie", ["sector"]),
            ("mixed", ["bar", "line"]),
            ("world_map", ["region"]),
            ("region_heatmap", ["region"]),
            ("matrix_heatmap", ["cell"]),
        ]
        let items = configurations.enumerated().map { offset, configuration -> [String: Any] in
            let index = offset + 1
            var item = base
            item["article_id"] = "integration-\(index)"
            item["source_url"] = "https://news.example/integration-\(index)"
            item["domain"] = index == 2 ? "fieldnote.example" : "news.example"
            item["published_at"] = String(format: "2026-07-11T%02d:10:00Z", 8 + index)
            if index == 2 {
                item["document_type"] = "audit_report"
                item["source_publisher"] = "Protocol Foundation"
            }

            var map = item["map"] as! [String: Any]
            map["headline"] = "\(configuration.0.uppercased()) 그래프로 읽는 도시의 변화"
            map["summary"] = "기사에 제시된 실제 수치의 비교를 \(configuration.0) 그래프로 확인한다."
            map["keywords"] = [configuration.0, "실제 수치", "도시"]
            let numericParagraph = configuration.1.count == 1
                ? "시범 구간의 값은 이전 8분, 이후 13분으로 기록됐습니다."
                : "계열 1의 값은 이전 8분, 이후 13분입니다. 계열 2의 값은 이전 11분, 이후 16분입니다."
            map["body_markdown"] = "첫 문단입니다. 도시의 변화를 기록했습니다.\n\n<img:0>\n\n\(numericParagraph)\n\n<chart:0>\n\n마지막 문단입니다. 기록은 다음 계획에 반영됩니다."
            map["claim_ids"] = ["CL-001", "CL-002", "CL-003"]
            map["required_claim_refs"] = ["CL-001", "CL-002"]
            map["selected_claim_refs"] = ["CL-001", "CL-002", "CL-003"]
            map["scenes"] = [
                [
                    "id": "S-01", "role": "lead", "label": "변화",
                    "title": "\(configuration.0.uppercased()) 그래프로 읽는 도시의 변화",
                    "summary": "기사에 제시된 실제 수치의 비교를 \(configuration.0) 그래프로 확인한다.",
                    "body": "첫 문단입니다. 도시의 변화를 기록했습니다.",
                    "secondary": "", "quote": "", "image_ref": 0,
                    "chart_ref": NSNull(), "claim_refs": ["CL-001"],
                ],
                [
                    "id": "S-02", "role": "evidence", "label": "수치",
                    "title": "시범 구간 값 비교 \(index)", "summary": "", "body": "",
                    "secondary": "", "quote": "", "image_ref": NSNull(),
                    "chart_ref": 0, "claim_refs": ["CL-002"],
                ],
                [
                    "id": "S-03", "role": "close", "label": "다음",
                    "title": "기록을 다음 계획으로", "summary": "",
                    "body": "마지막 문단입니다. 기록은 다음 계획에 반영됩니다.",
                    "secondary": "", "quote": "", "image_ref": NSNull(),
                    "chart_ref": NSNull(), "claim_refs": ["CL-003"],
                ],
            ]
            map["editorial"] = [
                "mode": "editorial_completeness",
                "pipeline_version": "integration-fixture",
                "source_hash": String(repeating: "a", count: 64),
                "critic_verdict": "pass",
            ]
            map["visualizations"] = [makeVisualization(
                kind: configuration.0,
                marks: configuration.1,
                articleIndex: index
            )]
            map["used_chart_refs"] = [0]
            var images = map["images"] as! [[String: Any]]
            images[0]["image_id"] = "integration-image-\(index)"
            images[0]["url"] = "https://images.example/integration-\(index).jpg"
            images[0]["caption"] = "도시 변화를 기록하는 시범 구간"
            map["images"] = images
            if cryptoMarketing > 0, index <= 3 {
                // F-2·F-3은 Home 첫 카드에서만 보이므로 셋이 같은 자리를 놓고
                // 겹친다. 그래서 강조할 기능을 첫 카드로 회전시킨다.
                let slot = ((index - 1 + (cryptoMarketing - 1)) % 3) + 1
                // App Store 크립토 스크린샷용 fixture. 세 항목이 F-1·F-2·F-3을
                // 하나씩 맡는다. 문자열 형식은 서버가 실제로 만드는 것과 같다 --
                // `staleness.py`의 `기사 시점 X · 지금 Y · Z% 감소`와
                // `publisher_context.py`의 고정 템플릿을 그대로 따른다. 발행처
                // 이름은 실재하지 않는 가상 매체다.
                item["document_type"] = ""
                item["source_publisher"] = ""
                var scenes = map["scenes"] as! [[String: Any]]
                switch slot {
                case 1:
                    // F-1 · 시효 대조. 임계치를 넘긴 수치 하나에만 주석이 붙는다.
                    item["domain"] = "온체인 브리프"
                    item["published_at"] = "2026-08-14T09:20:00Z"
                    map["headline"] = "재스테이킹에 묶인 담보가 다시 늘고 있다"
                    map["subheadline"] = "예치 경쟁이 만든 3주간의 되돌림"
                    map["summary"] = "재스테이킹 프로토콜에 예치된 담보 규모가 여름 내내 줄다가 다시 방향을 바꿨다."
                    map["keywords"] = ["재스테이킹", "담보", "이더리움"]
                    scenes[0]["label"] = "흐름"
                    scenes[0]["title"] = "재스테이킹에 묶인 담보가 다시 늘고 있다"
                    scenes[0]["summary"] = "예치 경쟁이 만든 3주간의 되돌림"
                    scenes[0]["body"] = "여름 내내 줄어들던 예치 담보가 8월 들어 방향을 바꿨습니다."
                    scenes[1]["label"] = "수치"
                    scenes[1]["title"] = "예치 담보의 규모"
                    scenes[1]["body"] = """
                    기사 작성 시점 재스테이킹 프로토콜의 예치 담보는 480억 달러였습니다. \
                    예치 유인이 가장 컸던 3주 사이에 담보가 몰렸고, 그 뒤로는 인출이 \
                    유입을 앞선 날이 더 많았습니다.
                    """
                    scenes[1]["secondary"] = "본문의 숫자는 그대로 두고, 지금 값을 그 아래에 덧붙입니다."
                    scenes[1]["chart_ref"] = NSNull()
                    scenes[1]["staleness_note"] = "기사 시점 480억 달러 · 지금 391억 달러 · 18.5% 감소"
                    scenes[2]["title"] = "다음 분기의 조건"
                    scenes[2]["body"] = "예치 유인이 유지되는지가 다음 분기의 방향을 정합니다."
                case 2:
                    // F-2 · 발행 맥락. `publisher_context.py`의 가능성 표현 템플릿.
                    item["domain"] = "디지털자산 리서치"
                    item["published_at"] = "2026-08-16T02:10:00Z"
                    map["headline"] = "레이어2 수수료 인하가 바꾼 거래 경로"
                    map["subheadline"] = "체인 사이를 오가는 비용의 재계산"
                    map["summary"] = "수수료 구조가 바뀌면서 거래가 지나가는 경로도 함께 움직였다."
                    map["keywords"] = ["레이어2", "수수료", "거래 경로"]
                    map["publisher_context"] =
                        "디지털자산 리서치이(가) 발행했고, 공개된 자료 기준 자사에 ETH이(가) 상장된 것으로 보입니다."
                    scenes[0]["label"] = "구조"
                    scenes[0]["title"] = "레이어2 수수료 인하가 바꾼 거래 경로"
                    scenes[0]["summary"] = "체인 사이를 오가는 비용의 재계산"
                    scenes[0]["body"] = "수수료가 내려가자 거래가 지나가는 경로부터 달라졌습니다."
                    scenes[1]["title"] = "경로별 비용 비교"
                    scenes[2]["title"] = "다음에 확인할 것"
                    scenes[2]["body"] = "인하가 유지되는지, 경로가 되돌아가는지를 함께 봅니다."
                default:
                    // F-3 · 1차 문서. 기사 대신 원문 문서 자체를 읽기 루트에 둔다.
                    item["domain"] = "블록 감사 컨소시엄"
                    item["published_at"] = "2026-08-15T11:00:00Z"
                    item["document_type"] = "audit_report"
                    item["source_publisher"] = "블록 감사 컨소시엄"
                    map["headline"] = "브리지 계약 감사 보고서가 남긴 세 가지 지적"
                    map["subheadline"] = "원문 82쪽에서 실제로 문제라고 적힌 부분"
                    map["summary"] = "감사 보고서 원문의 지적 사항을 순서대로 따라 읽는다."
                    map["keywords"] = ["감사 보고서", "브리지", "계약"]
                    scenes[0]["label"] = "원문"
                    scenes[0]["title"] = "브리지 계약 감사 보고서가 남긴 세 가지 지적"
                    scenes[0]["summary"] = "원문 82쪽에서 실제로 문제라고 적힌 부분"
                    scenes[0]["body"] = "보고서는 세 가지를 순서대로 지적합니다."
                    scenes[1]["title"] = "지적 사항의 무게"
                    scenes[2]["title"] = "보고서가 권고한 조치"
                    scenes[2]["body"] = "권고는 계약 교체가 아니라 검증 단계 추가입니다."
                }
                map["scenes"] = scenes
                map["body_markdown"] = String(
                    repeating: "원문의 흐름을 따라 한 논점씩 이어 읽습니다. ",
                    count: 40 + slot * 5
                )
            } else if homeHub, index <= 3 {
                let fixture: (source: String, headline: String, summary: String, keyword: String)
                switch index {
                case 1:
                    fixture = (
                        "The Guardian",
                        "조용한 바다가 먼저 보낸 신호",
                        "해수면 아래 변화가 해안 도시의 내일을 어떻게 바꾸는지 살펴봅니다.",
                        "해안"
                    )
                case 2:
                    fixture = (
                        "Nature",
                        "숲은 도시보다 먼저 비의 방향을 기억한다",
                        "기후가 바꾼 숲의 리듬을 위성 자료와 현장 기록으로 읽습니다.",
                        "숲"
                    )
                default:
                    fixture = (
                        "Reuters",
                        "막대그래프로 읽는 도시의 변화",
                        "숫자 뒤에 숨은 인구 이동과 생활 반경의 변화를 정리했습니다.",
                        "도시"
                    )
                }
                item["domain"] = fixture.source
                map["headline"] = fixture.headline
                map["summary"] = fixture.summary
                map["keywords"] = [fixture.keyword, "기록", "변화"]
                map["body_markdown"] = String(
                    repeating: "관측과 기록을 통해 변화의 방향을 차분히 살펴봅니다. ",
                    count: 42 + index * 6
                )
                var scenes = map["scenes"] as! [[String: Any]]
                scenes[0]["label"] = "오늘의 선택"
                scenes[0]["title"] = fixture.headline
                scenes[0]["summary"] = fixture.summary
                scenes[0]["body"] = fixture.summary
                scenes[1]["title"] = "기록에서 드러난 변화"
                scenes[2]["title"] = "다음 계절을 준비하는 법"
                map["scenes"] = scenes
            } else if boundaryWidthRegression, index == 1 {
                map["headline"] = "누수규모"
                map["keywords"] = ["누수", "물 부족", "호스파이프 금지"]
                var scenes = map["scenes"] as! [[String: Any]]
                scenes[0]["title"] = "누수규모"
                scenes[2]["role"] = "impact"
                scenes[2]["label"] = "영향"
                scenes.append([
                    "id": "S-04", "role": "close", "label": "결론",
                    "title": "물 사용 제한을 앞둔 런던", "summary": "",
                    "body": "건조한 날씨가 이어지며 물 사용 제한 가능성이 커졌습니다.",
                    "secondary": "", "quote": "", "image_ref": NSNull(),
                    "chart_ref": NSNull(), "claim_refs": ["CL-003"],
                ])
                map["scenes"] = scenes
            } else if boundaryWidthRegression, index == 2 {
                item["domain"] = "aol.co.uk"
                map["headline"] = "런던 건조 기간"
                var scenes = map["scenes"] as! [[String: Any]]
                scenes[0]["title"] = "런던 건조 기간"
                map["scenes"] = scenes
            }
            item["map"] = map
            return item
        }

        let response: [String: Any] = [
            "format_version": 1,
            "items": items,
            "next_cursor": NSNull(),
            "cooldown_seconds": 1.0,
        ]
        let data = try JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])
        return try APIJSON.makeDecoder().decode(FeedResponse.self, from: data).items
    }

    private static func makeDiscoveryResponse(
        from sourceItems: [ArticleFeedItem]
    ) throws -> DiscoveryResponse {
        var items = sourceItems
        for index in 0..<3 {
            let source = sourceItems[index]
            let encoded = try APIJSON.makeEncoder().encode(source)
            var object = try JSONSerialization.jsonObject(with: encoded) as! [String: Any]
            let number = sourceItems.count + index + 1
            object["article_id"] = "integration-discovery-\(number)"
            object["source_url"] = "https://research.example/discovery-\(number)"
            object["domain"] = index == 0 ? "research.example" : "civic.example"
            var map = object["map"] as! [String: Any]
            map["headline"] = [
                "데이터센터 전력망의 긴 시간표",
                "도시 그늘이 이동권에 미치는 영향",
                "기후 적응 예산의 지역별 격차",
            ][index]
            map["summary"] = "단발성 발표보다 오래 남는 구조와 근거를 차분히 설명합니다."
            map["keywords"] = ["구조", "근거", "변화"]
            object["map"] = map
            let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
            items.append(try APIJSON.makeDecoder().decode(ArticleFeedItem.self, from: data))
        }

        let roles: [DiscoveryRouteRole] = [.start, .context, .evidence, .counterpoint]
        let routeCopy = [
            (
                "AI 인프라를 네 번에 이해하기",
                "AI 인프라",
                "수요의 출발점부터 전력과 공급망, 지역사회의 반론까지 이어 읽습니다."
            ),
            (
                "도시 기후 적응을 읽는 네 관점",
                "도시 기후 적응",
                "원인과 설계, 검증된 대응 수단과 형평성 논쟁을 차례로 살펴봅니다."
            ),
        ]
        let routes = routeCopy.enumerated().map { routeIndex, copy in
            DiscoveryRoute(
                id: UUID(uuidString: String(
                    format: "00000000-0000-0000-0000-%012d",
                    routeIndex + 1
                ))!,
                rank: routeIndex + 1,
                slug: routeIndex == 0 ? "ai-infrastructure" : "climate-adaptation",
                title: copy.0,
                summary: copy.2,
                topic: copy.1,
                isRepresentative: routeIndex == 0,
                items: (0..<4).map { itemIndex in
                    DiscoveryRouteItem(
                        position: itemIndex + 1,
                        role: roles[itemIndex],
                        reason: "앞뒤 기사와 연결되는 구조와 근거가 충분한 선정 이유입니다.",
                        lastingValue: "며칠 뒤에도 현상을 이해하는 설명과 근거로 남는 기사입니다.",
                        article: items[routeIndex * 4 + itemIndex]
                    )
                }
            )
        }
        let timelineCopy = [
            (
                "AI 반도체 공급망의 새 질서",
                "AI 반도체",
                "수출 기준 개정부터 패키징 투자까지, 공급망 재편의 핵심 사건을 시간순으로 묶었습니다."
            ),
            (
                "데이터센터 전력 확보 경쟁",
                "전력과 데이터센터",
                "전력 구매 계약과 계통 접속 지연이 AI 인프라 확장에 미친 흐름을 정리했습니다."
            ),
            (
                "도시 기후 적응의 실행 단계",
                "도시 기후 적응",
                "폭염 대응 기준이 예산과 공간 설계로 이어지는 과정을 주요 사건으로 골랐습니다."
            ),
            (
                "위성 인터넷의 상용화 경쟁",
                "위성 인터넷",
                "주파수 승인과 단말 제휴가 실제 서비스 확장으로 이어진 사건을 모았습니다."
            ),
            (
                "전기차 배터리 공급망 재편",
                "배터리 공급망",
                "원재료 계약과 재활용 기준, 생산 조정이 맞물린 변화를 한 흐름으로 읽습니다."
            ),
            (
                "의료 AI 검증 기준의 형성",
                "의료 AI 규제",
                "임상 검증과 데이터 공개 기준이 제품 승인에 반영된 사건을 연결했습니다."
            ),
        ]
        let timelineBase = Date(timeIntervalSince1970: 1_783_180_800)
        let featuredTimelines = timelineCopy.enumerated().map { timelineIndex, copy in
            let number = timelineIndex + 1
            let nodeIDs = (1...5).map { nodeIndex in
                UUID(uuidString: String(
                    format: "20000000-0000-0000-0000-%012d",
                    number * 100 + nodeIndex
                ))!
            }
            let start = timelineBase.addingTimeInterval(Double(timelineIndex) * -86_400)
            let lanes: [TimelineLane] = [.main, .branch, .main, .branch, .main]
            let updates: [TimelineUpdateKind] = [
                .existing, .inserted, .existing, .inserted, .occurred,
            ]
            let nodes = (0..<5).map { nodeIndex in
                FeaturedTimelineNode(
                    id: nodeIDs[nodeIndex],
                    lane: lanes[nodeIndex],
                    anchorNodeID: lanes[nodeIndex] == .branch
                        ? nodeIDs[nodeIndex - 1]
                        : nil,
                    updateKind: updates[nodeIndex],
                    publishedAt: start.addingTimeInterval(Double(nodeIndex) * 3 * 86_400),
                    dateSource: .provider,
                    article: items[(timelineIndex + nodeIndex) % items.count]
                )
            }
            return FeaturedTimeline(
                id: UUID(uuidString: String(
                    format: "11000000-0000-0000-0000-%012d",
                    number
                ))!,
                rank: number,
                stableKey: String(
                    format: "10000000-0000-0000-0000-%012d",
                    number
                ),
                title: copy.0,
                summary: copy.2,
                topic: copy.1,
                startedAt: nodes.first!.publishedAt,
                endedAt: nodes.last!.publishedAt,
                nodes: Array(nodes.reversed())
            )
        }
        return DiscoveryResponse(
            generatedAt: Date(timeIntervalSince1970: 1_783_747_200),
            expiresAt: Date(timeIntervalSince1970: 2_000_000_000),
            representativeRouteID: routes[0].id,
            routes: routes,
            searchSuggestions: [
                "주간 인사이트",
                "도시 변화",
                "AI 인프라",
                "전력과 데이터센터",
                "기후 적응",
            ],
            trendingSearchSuggestions: [
                "도시 재생",
                "전력망 전환",
                "기후 적응",
            ],
            query: nil,
            results: [],
            cooldownSeconds: 1,
            timelineStatus: .fresh,
            timelinesGeneratedAt: Date(timeIntervalSince1970: 1_783_747_200),
            timelinesRefreshedAt: Date(timeIntervalSince1970: 1_783_747_200),
            featuredTimelines: featuredTimelines
        )
    }

    private static func makeVisualization(
        kind: String,
        marks: [String],
        articleIndex: Int
    ) -> [String: Any] {
        let series = marks.enumerated().map { seriesIndex, mark -> [String: Any] in
            let before = 8 + seriesIndex * 3
            let after = 13 + seriesIndex * 3
            let evidence = marks.count == 1
                ? "시범 구간의 값은 이전 \(before)분, 이후 \(after)분으로 기록됐습니다."
                : "계열 \(seriesIndex + 1)의 값은 이전 \(before)분, 이후 \(after)분입니다."
            var points: [[String: Any]] = [
                [
                    "label": "이전",
                    "value": before,
                    "raw_value": "\(before)분",
                    "evidence": evidence,
                ],
                [
                    "label": "이후",
                    "value": after,
                    "raw_value": "\(after)분",
                    "evidence": evidence,
                ],
            ]
            if mark == "region" {
                points[0]["label"] = "대한민국"
                points[0]["region_code"] = "KR"
                points[1]["label"] = "일본"
                points[1]["region_code"] = "JP"
            } else if mark == "cell" {
                points[0]["label"] = "아시아 · 이전"
                points[0]["row"] = "아시아"
                points[0]["column"] = "이전"
                points[1]["label"] = "유럽 · 이후"
                points[1]["row"] = "유럽"
                points[1]["column"] = "이후"
            }
            return [
                "name": marks.count == 1 ? "평균 체류 시간" : "계열 \(seriesIndex + 1)",
                "mark": mark,
                "points": points,
            ]
        }
        let isGeo = kind == "world_map" || kind == "region_heatmap"
        let isHeatmap = isGeo || kind == "matrix_heatmap"
        return [
            "ref": 0,
            "kind": kind,
            "title": "시범 구간 값 비교 \(articleIndex)",
            "unit": "분",
            "alt_text": "시범 구간의 이전 값과 이후 값을 \(kind) 그래프로 비교한다.",
            "geo_scope": kind == "world_map" ? "world" : (kind == "region_heatmap" ? "asia" : ""),
            "color_scale": isHeatmap
                ? ["scheme": kind == "matrix_heatmap" ? "violet" : "ocean", "mode": "quantized"]
                : NSNull(),
            "series": series,
        ]
    }
}
#endif
