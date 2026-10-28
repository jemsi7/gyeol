import ArticleFeedKit
import SwiftUI

enum PrototypeScreen: String, CaseIterable, Identifiable, Codable, Sendable {
    case onboarding = "SCR-01"
    case home = "SCR-10"
    case explore = "SCR-11"
    case reader = "SCR-02"
    case boundary = "SCR-03"
    case library = "SCR-04"
    case settings = "SCR-05"
    case loading = "SCR-06"
    case empty = "SCR-07"
    case offline = "SCR-08"
    case attestError = "SCR-09"

    var id: String { rawValue }

    var primaryTab: PrimaryAppTab? {
        switch self {
        case .home: return .home
        case .explore: return .reader
        case .library: return .library
        case .settings: return .settings
        case .onboarding, .reader, .boundary, .loading, .empty, .offline, .attestError:
            return nil
        }
    }

    var isProductionActive: Bool {
        switch self {
        case .home, .explore, .reader, .boundary, .library, .settings, .attestError:
            return true
        case .onboarding, .loading, .empty, .offline:
            return false
        }
    }

    var dockLabel: String {
        switch self {
        case .onboarding: return "01 첫 실행"
        case .home: return "10 읽기 홈"
        case .explore: return "11 둘러보기"
        case .reader: return "02 Reader"
        case .boundary: return "03 기사 경계"
        case .library: return "04 보관함"
        case .settings: return "05 읽기 설정"
        case .loading: return "06 로딩"
        case .empty: return "07 빈 Feed"
        case .offline: return "08 오프라인"
        case .attestError: return "09 앱 확인 오류"
        }
    }
}

enum PrimaryAppTab: String, CaseIterable, Identifiable, Sendable {
    case home
    case reader
    case library
    case settings

    var id: String { rawValue }

    var label: String {
        switch self {
        case .home: return "홈"
        case .reader: return "둘러보기"
        case .library: return "보관함"
        case .settings: return "설정"
        }
    }

    var symbol: String {
        switch self {
        case .home: return "house"
        case .reader: return "magnifyingglass"
        case .library: return "books.vertical"
        case .settings: return "gearshape"
        }
    }
}

enum HomeFeedStatus: Equatable, Sendable {
    case idle
    case loading
    case offlineCached(Int)
    case downloaded(Int)
    case cooldown
    case empty
    case failed
}

enum HomePaginationState: Equatable, Sendable {
    case idle
    case loading
    case end
    case failed
}

enum LayoutMode: String {
    case compact
    case split
    case wide

    init(width: CGFloat) {
        if width < 600 { self = .compact }
        else if width < 900 { self = .split }
        else { self = .wide }
    }
}

enum ArtworkKind: String, Codable, Sendable {
    case tidal
    case canopy
    case nightGrid
}

struct PrototypeScene: Identifiable, Equatable, Sendable {
    let id: Int
    let label: String
    let title: String
    let summary: String
    let body: String
    let secondary: String
    let artwork: ArtworkKind?
    let artworkLabel: String
    let caption: String
    let quote: String
    /// The then-versus-now line for a figure in this scene, or "" (C-13205).
    var stalenessNote: String = ""
    var remoteImageURL: URL? = nil
    var remoteImageID: String? = nil
    var visualization: ArticleVisualization? = nil
    var role: ArticleSceneRole? = nil
}

struct PrototypeArticle: Identifiable, Equatable, Sendable {
    let id: String
    let source: String
    let domain: String
    let readTime: String
    let date: String
    let scenes: [PrototypeScene]
    let keywords: [String]
    var sourceURL: URL? = nil
    var status: ArticleDisplayStatus = .ok
    var publishedAt: Date? = nil
    var headline: String = ""
    var documentType: String = ""
    var sourcePublisher: String = ""
    /// One factual line about who issued this article, or "" (DR-13203).
    /// Empty is the common case and renders nothing at all.
    var publisherContext: String = ""
    /// What happened to this report's figures afterwards, or "" (C-13210).
    var revisionNote: String = ""
    /// What has been added to this incident since it was saved, or "".
    var chainProgressNote: String = ""

    /// The single line shown before entering the article. Chain progress
    /// already resolves to the correction when there is one, so it wins.
    var entryNote: String {
        chainProgressNote.isEmpty ? revisionNote : chainProgressNote
    }

    var documentTypeLabel: String {
        switch documentType {
        case "governance_proposal": "거버넌스 제안"
        case "post_mortem": "사후 보고서"
        case "audit_report": "감사 보고서"
        case "regulatory_filing": "규제 파일링"
        case "foundation_notice": "공지"
        default: ""
        }
    }

    var homeMetadataText: String {
        guard !documentTypeLabel.isEmpty else { return "\(source) · \(readTime)" }
        let publisher = sourcePublisher.isEmpty ? source : sourcePublisher
        return "\(documentTypeLabel) · \(publisher) · \(readTime)"
    }

    var publishedContextText: String {
        guard let publishedAt else { return "발행 시각 미상" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.dateFormat = "yyyy. M. d. HH:mm"
        return formatter.string(from: publishedAt)
    }

    var homeHeadline: String {
        if !headline.isEmpty { return headline }
        return scenes.first?.title ?? source
    }

    var homeSummary: String {
        if let summary = scenes.lazy.map(\.summary).first(where: { !$0.isEmpty }) {
            return summary
        }
        if let body = scenes.lazy.map(\.body).first(where: { !$0.isEmpty }) {
            return body
        }
        return "기사의 핵심 흐름을 장면별로 읽어보세요."
    }
}

enum ArticleDisplayStatus: String, Codable, Sendable {
    case ok
    case degraded
}

enum ReaderTypeSize: String, CaseIterable, Identifiable, Codable, Sendable {
    case small = "작게"
    case standard = "기본"
    case large = "크게"
    var id: String { rawValue }
    var scale: CGFloat { self == .small ? 0.91 : self == .large ? 1.12 : 1 }
}

enum ReaderLeading: String, CaseIterable, Identifiable, Codable, Sendable {
    case compact = "촘촘"
    case comfortable = "편안"
    var id: String { rawValue }
    var spacing: CGFloat { self == .compact ? 4 : 7 }
}

enum ReaderTheme: String, CaseIterable, Identifiable, Codable, Sendable {
    case system = "시스템"
    case light = "밝게"
    case dark = "어둡게"
    var id: String { rawValue }
    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}

struct ReaderSettings: Codable, Equatable, Sendable {
    var typeSize: ReaderTypeSize = .standard
    var leading: ReaderLeading = .comfortable
    var theme: ReaderTheme = .light
    var motionEnabled = true
    var mediaSaver = false
    var offlineCache = true

    private enum CodingKeys: String, CodingKey {
        case typeSize, leading, theme, motionEnabled, mediaSaver, offlineCache
    }

    init(
        typeSize: ReaderTypeSize = .standard,
        leading: ReaderLeading = .comfortable,
        theme: ReaderTheme = .light,
        motionEnabled: Bool = true,
        mediaSaver: Bool = false,
        offlineCache: Bool = true
    ) {
        self.typeSize = typeSize
        self.leading = leading
        self.theme = theme
        self.motionEnabled = motionEnabled
        self.mediaSaver = mediaSaver
        self.offlineCache = offlineCache
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        typeSize = try container.decodeIfPresent(ReaderTypeSize.self, forKey: .typeSize) ?? .standard
        leading = try container.decodeIfPresent(ReaderLeading.self, forKey: .leading) ?? .comfortable
        theme = try container.decodeIfPresent(ReaderTheme.self, forKey: .theme) ?? .light
        motionEnabled = try container.decodeIfPresent(Bool.self, forKey: .motionEnabled) ?? true
        mediaSaver = try container.decodeIfPresent(Bool.self, forKey: .mediaSaver) ?? false
        offlineCache = try container.decodeIfPresent(Bool.self, forKey: .offlineCache) ?? true
    }
}

struct CacheStats: Equatable, Sendable {
    var articleCount = 0
    var imageCount = 0
    var byteCount = 0

    var formatted: String {
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        return "기사 \(articleCount)개 · 사진 \(imageCount)장 · 약 \(formatter.string(fromByteCount: Int64(byteCount)))"
    }
}

struct SharePayload: Identifiable, Equatable {
    let id = UUID()
    let headline: String
    let url: URL
}

enum PrototypeData {
    static let articles: [PrototypeArticle] = [
        PrototypeArticle(
            id: "prototype-1",
            source: "기후저널",
            domain: "climate-journal.kr",
            readTime: "6분 읽기",
            date: "2026. 7. 10.",
            scenes: [
                PrototypeScene(id: 0, label: "변화의 신호", title: "조용한 바다가 먼저 보낸 신호", summary: "연안 관측망이 기록한 작은 온도 변화는 올해 해양 생태계가 맞을 더 큰 변화를 예고한다.", body: "지난달 남해의 표층 수온은 평년보다 1.4도 높았다. 하루 단위로는 작은 차이지만, 같은 방향의 변화가 여러 지점에서 이어졌다는 점이 연구자들의 눈을 붙잡았다.", secondary: "관측팀은 하나의 숫자보다 변화가 이어지는 시간을 본다. 바다는 갑자기 달라지는 대신 먼저 길고 조용한 신호를 보낸다.", artwork: .tidal, artworkLabel: "연안의 수온과 염분을 기록하는 관측 부이", caption: "남해 연안 관측 부이. 센서는 10분 간격으로 수온과 염분을 기록한다.", quote: ""),
                PrototypeScene(id: 1, label: "바다의 기록", title: "하루의 온도보다 긴 시간을 본다", summary: "", body: "같은 날에도 수온은 해류와 바람에 따라 크게 오르내린다. 연구팀은 순간의 최고값보다 밤사이 얼마나 식었는지, 높은 온도가 며칠 동안 이어졌는지를 함께 살핀다.", secondary: "회복할 틈이 사라지면 작은 생물부터 이동 경로를 바꾼다. 그 변화는 어업과 해안 마을의 계절에도 천천히 도착한다.", artwork: .tidal, artworkLabel: "바다 위 연안 관측 부이", caption: "여러 관측점의 기록은 시간축 위에서 하나의 흐름으로 비교된다.", quote: ""),
                PrototypeScene(id: 2, label: "숫자 너머", title: "숫자는 생물이 쉴 수 있었는지 말한다", summary: "", body: "데이터는 예측을 위한 재료이면서 지역의 대응 시간을 확보하는 신호다. 양식장은 취수 깊이를 조절하고, 지자체는 고수온 경보보다 먼저 현장 관측 간격을 좁힐 수 있다.", secondary: "관측 정보가 충분히 빨리 공유될 때 같은 수치는 피해가 아니라 준비의 언어가 된다.", artwork: nil, artworkLabel: "", caption: "", quote: "“중요한 것은 최고 수온보다, 생물이 회복할 틈이 있었는지입니다.”"),
                PrototypeScene(id: 3, label: "다음 관측", title: "다음 계절을 위한 기록은 이미 시작됐다", summary: "", body: "올해 추가된 관측점은 섬과 만의 안쪽처럼 변화가 늦게 포착되던 곳을 연결한다. 연구팀은 이 기록을 지역 어민의 조업 일지와 함께 비교할 계획이다.", secondary: "바다의 변화는 멀리 있는 미래가 아니다. 오늘의 촘촘한 기록이 다음 계절의 선택지를 늘린다.", artwork: .nightGrid, artworkLabel: "밤에도 자료를 보내는 연안 관측망", caption: "관측값은 밤에도 육상 기지로 전달돼 다음 날 예측에 반영된다.", quote: "")
            ],
            keywords: ["해양 관측", "기후 적응", "연안 생태"]
        ),
        PrototypeArticle(
            id: "prototype-2",
            source: "필드노트",
            domain: "fieldnote.press",
            readTime: "8분 읽기",
            date: "2026. 7. 10.",
            scenes: [
                PrototypeScene(id: 0, label: "숲의 기억", title: "숲은 도시보다 먼저 비의 방향을 기억한다", summary: "나뭇잎과 토양에 남은 미세한 흔적은 한 계절의 비가 어디에서 와 어디로 흘렀는지 보여준다.", body: "해안 숲의 연구자들은 강수량만 보지 않는다. 잎에 머문 물의 시간과 토양으로 스민 깊이를 함께 읽어 숲이 가뭄을 버티는 방식을 기록한다.", secondary: "도시에서 하나의 비로 보이는 사건도 숲에서는 수십 개의 느린 경로로 나뉜다.", artwork: .canopy, artworkLabel: "안개 낀 해안 숲의 수관", caption: "해안 숲 상공에서 본 수관. 안개와 비는 서로 다른 높이에서 잎에 닿는다.", quote: ""),
                PrototypeScene(id: 1, label: "느린 경로", title: "빗물은 가장 짧은 길로 흐르지 않는다", summary: "", body: "거친 나무껍질과 두꺼운 낙엽층은 물의 속도를 늦춘다. 그 짧은 지연이 토양이 물을 받아들일 시간을 만들고 갑작스러운 유출을 줄인다.", secondary: "연구팀은 이 지연 시간을 도시의 녹지 설계에 옮길 방법을 찾고 있다.", artwork: .canopy, artworkLabel: "다양한 높이의 나무가 이어진 해안 숲", caption: "수관의 높이 차이는 물이 땅에 닿는 시간을 바꾼다.", quote: ""),
                PrototypeScene(id: 2, label: "도시의 번역", title: "숲의 시간을 도시에 번역한다", summary: "", body: "도시가 숲과 같아질 수는 없다. 대신 빗물이 한꺼번에 배수구로 몰리지 않도록 머무는 면적과 시간을 설계할 수 있다.", secondary: "작은 정원과 가로수 아래의 토양도 연결되면 하나의 느린 물길이 된다.", artwork: nil, artworkLabel: "", caption: "", quote: "“좋은 적응은 물을 빨리 버리는 일이 아니라, 잠시 머물 자리를 만드는 일입니다.”"),
                PrototypeScene(id: 3, label: "다음 비", title: "다음 비가 오기 전에 남겨야 할 것", summary: "", body: "연구팀은 올가을 공개 지도를 통해 숲과 도시의 물길을 함께 보여줄 예정이다. 숫자만으로는 보이지 않던 시간의 차이를 주민과 나누기 위해서다.", secondary: "한 계절의 기록은 다음 계절의 배수 계획과 나무를 심을 위치를 바꿀 수 있다.", artwork: .nightGrid, artworkLabel: "밤의 도시 기반 시설과 전력망", caption: "도시의 관측망은 비와 전력, 교통의 변화를 함께 기록한다.", quote: "")
            ],
            keywords: ["해안 숲", "물길", "도시 적응"]
        )
    ]
}
