import Foundation

private extension String {
    var isLowercaseSHA256: Bool {
        count == 64 && unicodeScalars.allSatisfy {
            ("0"..."9").contains(Character(String($0)))
                || ("a"..."f").contains(Character(String($0)))
        }
    }
}

public enum ClientPlatform: String, Codable, Sendable {
    case ios
    case ipados
}

public enum ChallengePurpose: String, Codable, Sendable {
    case attestation
    case assertion
}

public struct DeviceInfo: Codable, Equatable, Sendable {
    public let installationID: UUID
    public let vendorID: UUID?
    public let platform: ClientPlatform
    public let model: String
    public let osVersion: String
    public let appVersion: String
    public let buildNumber: String
    public let locale: String

    public init(
        installationID: UUID,
        vendorID: UUID?,
        platform: ClientPlatform,
        model: String,
        osVersion: String,
        appVersion: String,
        buildNumber: String,
        locale: String
    ) {
        self.installationID = installationID
        self.vendorID = vendorID
        self.platform = platform
        self.model = model
        self.osVersion = osVersion
        self.appVersion = appVersion
        self.buildNumber = buildNumber
        self.locale = locale
    }

    enum CodingKeys: String, CodingKey {
        case installationID = "installation_id"
        case vendorID = "vendor_id"
        case platform
        case model
        case osVersion = "os_version"
        case appVersion = "app_version"
        case buildNumber = "build_number"
        case locale
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        installationID = try UUID(
            uuidString: container.decode(String.self, forKey: .installationID)
        ).unwrap(or: DecodingError.dataCorruptedError(
            forKey: .installationID,
            in: container,
            debugDescription: "Invalid installation UUID"
        ))
        if let rawVendorID = try container.decodeIfPresent(String.self, forKey: .vendorID) {
            vendorID = try UUID(uuidString: rawVendorID).unwrap(or:
                DecodingError.dataCorruptedError(
                    forKey: .vendorID,
                    in: container,
                    debugDescription: "Invalid vendor UUID"
                )
            )
        } else {
            vendorID = nil
        }
        platform = try container.decode(ClientPlatform.self, forKey: .platform)
        model = try container.decode(String.self, forKey: .model)
        osVersion = try container.decode(String.self, forKey: .osVersion)
        appVersion = try container.decode(String.self, forKey: .appVersion)
        buildNumber = try container.decode(String.self, forKey: .buildNumber)
        locale = try container.decode(String.self, forKey: .locale)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(installationID.uuidString.lowercased(), forKey: .installationID)
        if let vendorID {
            try container.encode(vendorID.uuidString.lowercased(), forKey: .vendorID)
        } else {
            try container.encodeNil(forKey: .vendorID)
        }
        try container.encode(platform, forKey: .platform)
        try container.encode(model, forKey: .model)
        try container.encode(osVersion, forKey: .osVersion)
        try container.encode(appVersion, forKey: .appVersion)
        try container.encode(buildNumber, forKey: .buildNumber)
        try container.encode(locale, forKey: .locale)
    }
}

public struct ChallengeRequest: Codable, Equatable, Sendable {
    public let purpose: ChallengePurpose
    public let keyID: String
    public let device: DeviceInfo

    public init(purpose: ChallengePurpose, keyID: String, device: DeviceInfo) {
        self.purpose = purpose
        self.keyID = keyID
        self.device = device
    }

    enum CodingKeys: String, CodingKey {
        case purpose
        case keyID = "key_id"
        case device
    }
}

public struct ChallengeResponse: Codable, Equatable, Sendable {
    public let challengeID: UUID
    public let challenge: String
    public let expiresAt: Date

    enum CodingKeys: String, CodingKey {
        case challengeID = "challenge_id"
        case challenge
        case expiresAt = "expires_at"
    }
}

public struct RegisterRequest: Codable, Equatable, Sendable {
    public let challengeID: UUID
    public let keyID: String
    public let attestationObject: String
    public let device: DeviceInfo

    public init(
        challengeID: UUID,
        keyID: String,
        attestationObject: String,
        device: DeviceInfo
    ) {
        self.challengeID = challengeID
        self.keyID = keyID
        self.attestationObject = attestationObject
        self.device = device
    }

    enum CodingKeys: String, CodingKey {
        case challengeID = "challenge_id"
        case keyID = "key_id"
        case attestationObject = "attestation_object"
        case device
    }
}

public struct RegisterResponse: Codable, Equatable, Sendable {
    public let registered: Bool
    public let keyID: String

    enum CodingKeys: String, CodingKey {
        case registered
        case keyID = "key_id"
    }
}

public enum FeedMode: String, Codable, Equatable, Sendable {
    case recent
    case random
    case latest
}

public struct FeedPayload: Codable, Equatable, Sendable {
    public let device: DeviceInfo
    public let limit: Int
    public let cursor: String?
    public let mode: FeedMode
    public let excludeArticleIDs: [String]

    public init(
        device: DeviceInfo,
        limit: Int = 5,
        cursor: String? = nil,
        mode: FeedMode = .recent,
        excludeArticleIDs: [String] = []
    ) {
        self.device = device
        self.limit = limit
        self.cursor = cursor
        self.mode = mode
        var seen = Set<String>()
        var normalizedArticleIDs: [String] = []
        for articleID in excludeArticleIDs where seen.insert(articleID).inserted {
            normalizedArticleIDs.append(articleID)
            if normalizedArticleIDs.count == 32 { break }
        }
        self.excludeArticleIDs = normalizedArticleIDs
    }

    enum CodingKeys: String, CodingKey {
        case device
        case limit
        case cursor
        case mode
        case excludeArticleIDs = "exclude_article_ids"
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(device, forKey: .device)
        try container.encode(limit, forKey: .limit)
        if let cursor {
            try container.encode(cursor, forKey: .cursor)
        } else {
            try container.encodeNil(forKey: .cursor)
        }
        try container.encode(mode, forKey: .mode)
        try container.encode(excludeArticleIDs, forKey: .excludeArticleIDs)
    }
}

public struct AssertionEnvelope: Codable, Equatable, Sendable {
    public let challengeID: UUID
    public let keyID: String
    public let assertion: String
    public let issuedAt: Int

    public init(challengeID: UUID, keyID: String, assertion: String, issuedAt: Int) {
        self.challengeID = challengeID
        self.keyID = keyID
        self.assertion = assertion
        self.issuedAt = issuedAt
    }

    enum CodingKeys: String, CodingKey {
        case challengeID = "challenge_id"
        case keyID = "key_id"
        case assertion
        case issuedAt = "issued_at"
    }
}

public struct AuthenticatedFeedRequest: Codable, Equatable, Sendable {
    public let payload: FeedPayload
    public let auth: AssertionEnvelope

    public init(payload: FeedPayload, auth: AssertionEnvelope) {
        self.payload = payload
        self.auth = auth
    }
}

public struct PrivacyConsentPayload: Codable, Equatable, Sendable {
    public let policyVersion: String
    public let device: DeviceInfo

    public init(policyVersion: String, device: DeviceInfo) {
        self.policyVersion = policyVersion
        self.device = device
    }

    enum CodingKeys: String, CodingKey {
        case policyVersion = "policy_version"
        case device
    }
}

public struct AuthenticatedPrivacyConsentRequest: Codable, Equatable, Sendable {
    public let payload: PrivacyConsentPayload
    public let auth: AssertionEnvelope

    public init(payload: PrivacyConsentPayload, auth: AssertionEnvelope) {
        self.payload = payload
        self.auth = auth
    }
}

public struct PrivacyConsentReceipt: Codable, Equatable, Sendable {
    public let consentID: UUID
    public let policyVersion: String
    public let policyURL: URL
    public let acceptedAt: Date

    public init(
        consentID: UUID,
        policyVersion: String,
        policyURL: URL,
        acceptedAt: Date
    ) {
        self.consentID = consentID
        self.policyVersion = policyVersion
        self.policyURL = policyURL
        self.acceptedAt = acceptedAt
    }

    enum CodingKeys: String, CodingKey {
        case consentID = "consent_id"
        case policyVersion = "policy_version"
        case policyURL = "policy_url"
        case acceptedAt = "accepted_at"
    }
}

public struct ArticleViewEvent: Codable, Equatable, Sendable, Identifiable {
    public let eventID: UUID
    public let articleID: String

    public var id: UUID { eventID }
    public var isValid: Bool { articleID.isLowercaseSHA256 }

    public init(eventID: UUID = UUID(), articleID: String) {
        self.eventID = eventID
        self.articleID = articleID
    }

    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case articleID = "article_id"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawEventID = try container.decode(String.self, forKey: .eventID)
        eventID = try UUID(uuidString: rawEventID).unwrap(
            or: DecodingError.dataCorruptedError(
                forKey: .eventID,
                in: container,
                debugDescription: "Invalid article-view event UUID"
            )
        )
        articleID = try container.decode(String.self, forKey: .articleID)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(
            eventID.uuidString.lowercased(),
            forKey: .eventID
        )
        try container.encode(articleID, forKey: .articleID)
    }
}

public struct ArticleViewPayload: Codable, Equatable, Sendable {
    public let eventID: UUID
    public let articleID: String
    public let device: DeviceInfo

    public var isValid: Bool { articleID.isLowercaseSHA256 }

    public init(eventID: UUID, articleID: String, device: DeviceInfo) {
        self.eventID = eventID
        self.articleID = articleID
        self.device = device
    }

    public init(event: ArticleViewEvent, device: DeviceInfo) {
        self.init(
            eventID: event.eventID,
            articleID: event.articleID,
            device: device
        )
    }

    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case articleID = "article_id"
        case device
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(
            eventID.uuidString.lowercased(),
            forKey: .eventID
        )
        try container.encode(articleID, forKey: .articleID)
        try container.encode(device, forKey: .device)
    }
}

public struct AuthenticatedArticleViewRequest: Codable, Equatable, Sendable {
    public let payload: ArticleViewPayload
    public let auth: AssertionEnvelope

    public init(payload: ArticleViewPayload, auth: AssertionEnvelope) {
        self.payload = payload
        self.auth = auth
    }
}

public struct ArticleViewResponse: Codable, Equatable, Sendable {
    public let articleID: String
    public let counted: Bool
    public let viewCount: Int

    public init(articleID: String, counted: Bool, viewCount: Int) {
        self.articleID = articleID
        self.counted = counted
        self.viewCount = viewCount
    }

    enum CodingKeys: String, CodingKey {
        case articleID = "article_id"
        case counted
        case viewCount = "view_count"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        articleID = try container.decode(String.self, forKey: .articleID)
        counted = try container.decode(Bool.self, forKey: .counted)
        viewCount = try container.decode(Int.self, forKey: .viewCount)
        guard articleID.isLowercaseSHA256 else {
            throw DecodingError.dataCorruptedError(
                forKey: .articleID,
                in: container,
                debugDescription: "Invalid article-view response article ID"
            )
        }
        guard viewCount >= 0 else {
            throw DecodingError.dataCorruptedError(
                forKey: .viewCount,
                in: container,
                debugDescription: "Article-view count must not be negative"
            )
        }
    }
}

public enum APNsEnvironment: String, Codable, Sendable {
    case development
    case production
}

public struct PushNotificationPreferences: Codable, Equatable, Sendable {
    public var editorial: Bool
    public var weeklyInsight: Bool
    public var timelineRefreshes: Bool
    public var timelineAdditions: Bool

    public var timelineUpdates: Bool {
        get { timelineRefreshes || timelineAdditions }
        set {
            timelineRefreshes = newValue
            timelineAdditions = newValue
        }
    }

    public init(
        editorial: Bool = false,
        weeklyInsight: Bool = false,
        timelineUpdates: Bool = false,
        timelineRefreshes: Bool? = nil,
        timelineAdditions: Bool? = nil
    ) {
        self.editorial = editorial
        self.weeklyInsight = weeklyInsight
        self.timelineRefreshes = timelineRefreshes ?? timelineUpdates
        self.timelineAdditions = timelineAdditions ?? timelineUpdates
    }

    public static let allEnabled = PushNotificationPreferences(
        editorial: true,
        weeklyInsight: true
    )
    public static let editorialOnly = PushNotificationPreferences(editorial: true)
    public static let none = PushNotificationPreferences()
    public var anyEnabled: Bool {
        editorial || weeklyInsight || timelineRefreshes || timelineAdditions
    }

    enum CodingKeys: String, CodingKey {
        case editorial
        case weeklyInsight = "weekly_insight"
        case timelineUpdates = "timeline_updates"
        case timelineRefreshes = "timeline_refreshes"
        case timelineAdditions = "timeline_additions"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        editorial = try container.decodeIfPresent(Bool.self, forKey: .editorial) ?? false
        weeklyInsight = try container.decodeIfPresent(Bool.self, forKey: .weeklyInsight) ?? false
        let legacyTimelineUpdates = try container.decodeIfPresent(
            Bool.self,
            forKey: .timelineUpdates
        ) ?? false
        timelineRefreshes = try container.decodeIfPresent(
            Bool.self,
            forKey: .timelineRefreshes
        ) ?? legacyTimelineUpdates
        timelineAdditions = try container.decodeIfPresent(
            Bool.self,
            forKey: .timelineAdditions
        ) ?? legacyTimelineUpdates
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(editorial, forKey: .editorial)
        try container.encode(weeklyInsight, forKey: .weeklyInsight)
        try container.encode(timelineUpdates, forKey: .timelineUpdates)
        try container.encode(timelineRefreshes, forKey: .timelineRefreshes)
        try container.encode(timelineAdditions, forKey: .timelineAdditions)
    }
}

public struct PushRegistrationPayload: Codable, Equatable, Sendable {
    public let installationID: UUID
    public let deviceToken: String
    public let environment: APNsEnvironment
    public let preferences: PushNotificationPreferences
    public let device: DeviceInfo

    public init(
        installationID: UUID,
        deviceToken: String,
        environment: APNsEnvironment,
        preferences: PushNotificationPreferences = .editorialOnly,
        device: DeviceInfo
    ) {
        self.installationID = installationID
        self.deviceToken = deviceToken
        self.environment = environment
        self.preferences = preferences
        self.device = device
    }

    enum CodingKeys: String, CodingKey {
        case installationID = "installation_id"
        case deviceToken = "device_token"
        case environment
        case preferences
        case device
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(installationID.uuidString.lowercased(), forKey: .installationID)
        try container.encode(deviceToken, forKey: .deviceToken)
        try container.encode(environment, forKey: .environment)
        try container.encode(preferences, forKey: .preferences)
        try container.encode(device, forKey: .device)
    }
}

public struct AuthenticatedPushRegistrationRequest: Codable, Equatable, Sendable {
    public let payload: PushRegistrationPayload
    public let auth: AssertionEnvelope

    public init(payload: PushRegistrationPayload, auth: AssertionEnvelope) {
        self.payload = payload
        self.auth = auth
    }
}

public struct PushRegistrationResponse: Codable, Equatable, Sendable {
    public let active: Bool
    public let installationID: UUID
    public let environment: APNsEnvironment
    public let preferences: PushNotificationPreferences

    enum CodingKeys: String, CodingKey {
        case active
        case installationID = "installation_id"
        case environment
        case preferences
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        active = try container.decode(Bool.self, forKey: .active)
        installationID = try container.decode(UUID.self, forKey: .installationID)
        environment = try container.decode(APNsEnvironment.self, forKey: .environment)
        preferences = try container.decodeIfPresent(
            PushNotificationPreferences.self,
            forKey: .preferences
        ) ?? .editorialOnly
    }
}

public enum ArticleMapStatus: String, Codable, Sendable {
    case ok
    case degraded
}

public struct ArticleImage: Codable, Equatable, Sendable {
    public let ref: Int
    public let imageID: String
    public let url: URL
    public let sourceURL: URL?
    public let role: String?
    public let caption: String?

    enum CodingKeys: String, CodingKey {
        case ref
        case imageID = "image_id"
        case url
        case sourceURL = "source_url"
        case role
        case caption
    }
}

public enum ArticleVisualizationKind: Equatable, Sendable, Codable {
    case bar
    case line
    case area
    case pie
    case mixed
    case worldMap
    case regionHeatmap
    case matrixHeatmap
    case unsupported(String)

    public var rawValue: String {
        switch self {
        case .bar: return "bar"
        case .line: return "line"
        case .area: return "area"
        case .pie: return "pie"
        case .mixed: return "mixed"
        case .worldMap: return "world_map"
        case .regionHeatmap: return "region_heatmap"
        case .matrixHeatmap: return "matrix_heatmap"
        case .unsupported(let value): return value
        }
    }

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        switch raw {
        case "bar": self = .bar
        case "line": self = .line
        case "area": self = .area
        case "pie": self = .pie
        case "mixed": self = .mixed
        case "world_map": self = .worldMap
        case "region_heatmap": self = .regionHeatmap
        case "matrix_heatmap": self = .matrixHeatmap
        default: self = .unsupported(raw)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public enum ArticleChartMark: Equatable, Sendable, Codable {
    case bar
    case line
    case area
    case sector
    case region
    case cell
    case unsupported(String)

    public var rawValue: String {
        switch self {
        case .bar: return "bar"
        case .line: return "line"
        case .area: return "area"
        case .sector: return "sector"
        case .region: return "region"
        case .cell: return "cell"
        case .unsupported(let value): return value
        }
    }

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        switch raw {
        case "bar": self = .bar
        case "line": self = .line
        case "area": self = .area
        case "sector": self = .sector
        case "region": self = .region
        case "cell": self = .cell
        default: self = .unsupported(raw)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public struct ArticleChartPoint: Codable, Equatable, Sendable {
    public let label: String
    public let value: Double
    public let rawValue: String
    public let evidence: String
    public let regionCode: String?
    public let row: String?
    public let column: String?

    enum CodingKeys: String, CodingKey {
        case label
        case value
        case rawValue = "raw_value"
        case evidence
        case regionCode = "region_code"
        case row
        case column
    }
}

public struct ArticleColorScale: Codable, Equatable, Sendable {
    public let scheme: String
    public let mode: String

    public init(scheme: String, mode: String) {
        self.scheme = scheme
        self.mode = mode
    }
}

public struct ArticleChartSeries: Codable, Equatable, Sendable {
    public let name: String
    public let mark: ArticleChartMark
    public let points: [ArticleChartPoint]
}

public struct ArticleVisualization: Codable, Equatable, Sendable {
    public let ref: Int
    public let kind: ArticleVisualizationKind
    public let title: String
    public let unit: String
    public let altText: String
    public let geoScope: String?
    public let colorScale: ArticleColorScale?
    public let series: [ArticleChartSeries]

    enum CodingKeys: String, CodingKey {
        case ref
        case kind
        case title
        case unit
        case altText = "alt_text"
        case geoScope = "geo_scope"
        case colorScale = "color_scale"
        case series
    }
}

public enum ArticleSceneRole: Equatable, Sendable, Codable {
    case lead
    case context
    case evidence
    case impact
    case close
    case unsupported(String)

    public var rawValue: String {
        switch self {
        case .lead: return "lead"
        case .context: return "context"
        case .evidence: return "evidence"
        case .impact: return "impact"
        case .close: return "close"
        case .unsupported(let value): return value
        }
    }

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        switch raw {
        case "lead": self = .lead
        case "context": self = .context
        case "evidence": self = .evidence
        case "impact": self = .impact
        case "close": self = .close
        default: self = .unsupported(raw)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public struct ArticleScene: Codable, Equatable, Sendable {
    public let id: String
    public let role: ArticleSceneRole
    public let label: String
    public let title: String
    public let summary: String
    public let body: String
    public let secondary: String
    public let quote: String
    /// The then-versus-now line for a figure in this scene, or "" (C-13205).
    /// Computed per request rather than stored, because it states what a
    /// number is *now*.
    public let stalenessNote: String
    public let imageRef: Int?
    public let chartRef: Int?
    public let claimRefs: [String]

    enum CodingKeys: String, CodingKey {
        case id
        case role
        case label
        case title
        case summary
        case body
        case secondary
        case quote
        case stalenessNote = "staleness_note"
        case imageRef = "image_ref"
        case chartRef = "chart_ref"
        case claimRefs = "claim_refs"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id) ?? ""
        role = try container.decodeIfPresent(ArticleSceneRole.self, forKey: .role)
            ?? .unsupported("")
        label = try container.decodeIfPresent(String.self, forKey: .label) ?? ""
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? ""
        summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? ""
        body = try container.decodeIfPresent(String.self, forKey: .body) ?? ""
        secondary = try container.decodeIfPresent(String.self, forKey: .secondary) ?? ""
        quote = try container.decodeIfPresent(String.self, forKey: .quote) ?? ""
        // Absent on every Map written before C-13205, and on every scene with
        // nothing worth saying.
        stalenessNote = try container.decodeIfPresent(
            String.self,
            forKey: .stalenessNote
        ) ?? ""
        imageRef = try container.decodeIfPresent(Int.self, forKey: .imageRef)
        chartRef = try container.decodeIfPresent(Int.self, forKey: .chartRef)
        claimRefs = try container.decodeIfPresent([String].self, forKey: .claimRefs) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(role, forKey: .role)
        try container.encode(label, forKey: .label)
        try container.encode(title, forKey: .title)
        try container.encode(summary, forKey: .summary)
        try container.encode(body, forKey: .body)
        try container.encode(secondary, forKey: .secondary)
        try container.encode(quote, forKey: .quote)
        try container.encode(stalenessNote, forKey: .stalenessNote)
        try container.encodeIfPresent(imageRef, forKey: .imageRef)
        try container.encodeIfPresent(chartRef, forKey: .chartRef)
        try container.encode(claimRefs, forKey: .claimRefs)
    }
}

public struct ArticleEditorialMetadata: Codable, Equatable, Sendable {
    public let mode: String
    public let pipelineVersion: String
    public let sourceHash: String
    public let criticVerdict: String

    enum CodingKeys: String, CodingKey {
        case mode
        case pipelineVersion = "pipeline_version"
        case sourceHash = "source_hash"
        case criticVerdict = "critic_verdict"
    }
}

public struct ArticleMap: Codable, Equatable, Sendable {
    public let formatVersion: Int
    public let language: String
    public let headline: String
    public let subheadline: String?
    public let summary: String
    public let keywords: [String]
    public let bodyMarkdown: String
    public let bodyResolved: String?
    public let images: [ArticleImage]
    public let coverImageRef: Int?
    public let usedRefs: [Int]
    public let visualizations: [ArticleVisualization]
    public let usedChartRefs: [Int]
    public let claimIDs: [String]
    public let requiredClaimRefs: [String]
    public let selectedClaimRefs: [String]
    public let scenes: [ArticleScene]
    /// One factual line about who issued this article, or "" when nothing can
    /// be stated (DR-13203). Never a rating and never a warning.
    public let publisherContext: String
    public let editorial: ArticleEditorialMetadata?

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case language
        case headline
        case subheadline
        case summary
        case keywords
        case bodyMarkdown = "body_markdown"
        case bodyResolved = "body_resolved"
        case images
        case coverImageRef = "cover_image_ref"
        case usedRefs = "used_refs"
        case visualizations
        case usedChartRefs = "used_chart_refs"
        case claimIDs = "claim_ids"
        case requiredClaimRefs = "required_claim_refs"
        case selectedClaimRefs = "selected_claim_refs"
        case scenes
        case publisherContext = "publisher_context"
        case editorial
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        formatVersion = try container.decode(Int.self, forKey: .formatVersion)
        language = try container.decode(String.self, forKey: .language)
        headline = try container.decode(String.self, forKey: .headline)
        subheadline = try container.decodeIfPresent(String.self, forKey: .subheadline)
        summary = try container.decode(String.self, forKey: .summary)
        keywords = try container.decode([String].self, forKey: .keywords)
        bodyMarkdown = try container.decode(String.self, forKey: .bodyMarkdown)
        bodyResolved = try container.decodeIfPresent(String.self, forKey: .bodyResolved)
        images = try container.decode([ArticleImage].self, forKey: .images)
        coverImageRef = try container.decodeIfPresent(Int.self, forKey: .coverImageRef)
        usedRefs = try container.decodeIfPresent([Int].self, forKey: .usedRefs) ?? []
        visualizations = try container.decodeIfPresent(
            [ArticleVisualization].self,
            forKey: .visualizations
        ) ?? []
        usedChartRefs = try container.decodeIfPresent([Int].self, forKey: .usedChartRefs) ?? []
        claimIDs = try container.decodeIfPresent([String].self, forKey: .claimIDs) ?? []
        requiredClaimRefs = try container.decodeIfPresent(
            [String].self,
            forKey: .requiredClaimRefs
        ) ?? []
        selectedClaimRefs = try container.decodeIfPresent(
            [String].self,
            forKey: .selectedClaimRefs
        ) ?? []
        scenes = try container.decodeIfPresent([ArticleScene].self, forKey: .scenes) ?? []
        // Absent on Maps written before DR-13203, and on every core-domain Map
        // that has nothing factual to say.
        publisherContext = try container.decodeIfPresent(
            String.self,
            forKey: .publisherContext
        ) ?? ""
        editorial = try container.decodeIfPresent(
            ArticleEditorialMetadata.self,
            forKey: .editorial
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(formatVersion, forKey: .formatVersion)
        try container.encode(language, forKey: .language)
        try container.encode(headline, forKey: .headline)
        try container.encodeIfPresent(subheadline, forKey: .subheadline)
        try container.encode(summary, forKey: .summary)
        try container.encode(keywords, forKey: .keywords)
        try container.encode(bodyMarkdown, forKey: .bodyMarkdown)
        try container.encodeIfPresent(bodyResolved, forKey: .bodyResolved)
        try container.encode(images, forKey: .images)
        try container.encodeIfPresent(coverImageRef, forKey: .coverImageRef)
        try container.encode(usedRefs, forKey: .usedRefs)
        try container.encode(visualizations, forKey: .visualizations)
        try container.encode(usedChartRefs, forKey: .usedChartRefs)
        try container.encode(claimIDs, forKey: .claimIDs)
        try container.encode(requiredClaimRefs, forKey: .requiredClaimRefs)
        try container.encode(selectedClaimRefs, forKey: .selectedClaimRefs)
        try container.encode(scenes, forKey: .scenes)
        try container.encode(publisherContext, forKey: .publisherContext)
        try container.encodeIfPresent(editorial, forKey: .editorial)
    }
}

public struct ArticleFeedItem: Codable, Equatable, Sendable, Identifiable {
    public let articleID: String
    public let sourceURL: URL?
    public let domain: String?
    public let publishedAt: Date?
    public let sourceLanguage: String?
    public let sourceCountry: String?
    public let documentType: String
    public let sourcePublisher: String
    public let status: ArticleMapStatus
    /// What happened to this report's figures afterwards, or "" (C-13210).
    /// Never a judgement about which report was right.
    public let revisionNote: String
    /// The later article the note points at, or "".
    public let revisionTargetArticleID: String
    /// What has been added to this incident since it was saved, or "".
    public let chainProgressNote: String
    public let map: ArticleMap

    public var id: String { articleID }

    enum CodingKeys: String, CodingKey {
        case articleID = "article_id"
        case sourceURL = "source_url"
        case domain
        case publishedAt = "published_at"
        case sourceLanguage = "source_language"
        case sourceCountry = "source_country"
        case documentType = "document_type"
        case sourcePublisher = "source_publisher"
        case status
        case revisionNote = "revision_note"
        case revisionTargetArticleID = "revision_target_article_id"
        case chainProgressNote = "chain_progress_note"
        case map
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        articleID = try container.decode(String.self, forKey: .articleID)
        sourceURL = try container.decodeIfPresent(URL.self, forKey: .sourceURL)
        domain = try container.decodeIfPresent(String.self, forKey: .domain)
        publishedAt = try container.decodeIfPresent(Date.self, forKey: .publishedAt)
        sourceLanguage = try container.decodeIfPresent(String.self, forKey: .sourceLanguage)
        sourceCountry = try container.decodeIfPresent(String.self, forKey: .sourceCountry)
        documentType = try container.decodeIfPresent(String.self, forKey: .documentType) ?? ""
        sourcePublisher = try container.decodeIfPresent(String.self, forKey: .sourcePublisher) ?? ""
        status = try container.decode(ArticleMapStatus.self, forKey: .status)
        // Absent on every payload written before C-13210/C-13211, and on every
        // article whose incident has not moved.
        revisionNote = try container.decodeIfPresent(String.self, forKey: .revisionNote) ?? ""
        revisionTargetArticleID = try container.decodeIfPresent(
            String.self,
            forKey: .revisionTargetArticleID
        ) ?? ""
        chainProgressNote = try container.decodeIfPresent(
            String.self,
            forKey: .chainProgressNote
        ) ?? ""
        map = try container.decode(ArticleMap.self, forKey: .map)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(articleID, forKey: .articleID)
        try container.encodeIfPresent(sourceURL, forKey: .sourceURL)
        try container.encodeIfPresent(domain, forKey: .domain)
        try container.encodeIfPresent(publishedAt, forKey: .publishedAt)
        try container.encodeIfPresent(sourceLanguage, forKey: .sourceLanguage)
        try container.encodeIfPresent(sourceCountry, forKey: .sourceCountry)
        try container.encode(documentType, forKey: .documentType)
        try container.encode(sourcePublisher, forKey: .sourcePublisher)
        try container.encode(status, forKey: .status)
        try container.encode(revisionNote, forKey: .revisionNote)
        try container.encode(revisionTargetArticleID, forKey: .revisionTargetArticleID)
        try container.encode(chainProgressNote, forKey: .chainProgressNote)
        try container.encode(map, forKey: .map)
    }
}

public struct FeedResponse: Codable, Equatable, Sendable {
    public let formatVersion: Int
    public let items: [ArticleFeedItem]
    public let nextCursor: String?
    public let cooldownSeconds: Double

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case items
        case nextCursor = "next_cursor"
        case cooldownSeconds = "cooldown_seconds"
    }
}

public enum DiscoveryCollection: String, Codable, Equatable, Sendable {
    case editorial
    case weeklyInsight = "weekly_insight"
}

public struct DiscoveryPayload: Codable, Equatable, Sendable {
    public let device: DeviceInfo
    public let query: String?
    public let limit: Int
    public let collection: DiscoveryCollection?
    public let cursor: String?

    public init(
        device: DeviceInfo,
        query: String? = nil,
        limit: Int = 12,
        collection: DiscoveryCollection? = nil,
        cursor: String? = nil
    ) {
        self.device = device
        self.query = query
        self.limit = limit
        self.collection = collection
        self.cursor = cursor
    }

    enum CodingKeys: String, CodingKey {
        case device
        case query
        case limit
        case collection
        case cursor
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(device, forKey: .device)
        if let query {
            try container.encode(query, forKey: .query)
        } else {
            try container.encodeNil(forKey: .query)
        }
        try container.encode(limit, forKey: .limit)
        if let collection {
            try container.encode(collection, forKey: .collection)
        } else {
            try container.encodeNil(forKey: .collection)
        }
        if let cursor {
            try container.encode(cursor, forKey: .cursor)
        } else {
            try container.encodeNil(forKey: .cursor)
        }
    }
}

public struct AuthenticatedDiscoveryRequest: Codable, Equatable, Sendable {
    public let payload: DiscoveryPayload
    public let auth: AssertionEnvelope

    public init(payload: DiscoveryPayload, auth: AssertionEnvelope) {
        self.payload = payload
        self.auth = auth
    }
}

public enum DiscoveryRouteRole: String, Codable, Equatable, Sendable {
    case start
    case context
    case evidence
    case counterpoint

    public var displayLabel: String {
        switch self {
        case .start: return "먼저"
        case .context: return "이어서"
        case .evidence: return "근거로"
        case .counterpoint: return "반대편"
        }
    }
}

public struct DiscoveryRouteItem: Codable, Equatable, Sendable, Identifiable {
    public let position: Int
    public let role: DiscoveryRouteRole
    public let reason: String
    public let lastingValue: String
    public let article: ArticleFeedItem

    public var id: String { article.articleID }

    public init(
        position: Int,
        role: DiscoveryRouteRole,
        reason: String,
        lastingValue: String,
        article: ArticleFeedItem
    ) {
        self.position = position
        self.role = role
        self.reason = reason
        self.lastingValue = lastingValue
        self.article = article
    }

    enum CodingKeys: String, CodingKey {
        case position
        case role
        case reason
        case lastingValue = "lasting_value"
        case article
    }
}

public struct DiscoveryRoute: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let rank: Int
    public let slug: String
    public let title: String
    public let summary: String
    public let topic: String
    public let isRepresentative: Bool
    public let items: [DiscoveryRouteItem]

    public init(
        id: UUID,
        rank: Int,
        slug: String,
        title: String,
        summary: String,
        topic: String,
        isRepresentative: Bool,
        items: [DiscoveryRouteItem]
    ) {
        self.id = id
        self.rank = rank
        self.slug = slug
        self.title = title
        self.summary = summary
        self.topic = topic
        self.isRepresentative = isRepresentative
        self.items = items
    }

    enum CodingKeys: String, CodingKey {
        case id
        case rank
        case slug
        case title
        case summary
        case topic
        case isRepresentative = "is_representative"
        case items
    }
}

public enum DiscoveryResultDepth: String, Codable, Equatable, Sendable {
    case quick
    case deep
}

public enum DiscoveryResultRetrieval: String, Codable, Equatable, Sendable {
    case keyword
    case rag
}

public struct DiscoverySearchResult: Codable, Equatable, Sendable, Identifiable {
    public let depth: DiscoveryResultDepth
    public let retrieval: DiscoveryResultRetrieval
    public let article: ArticleFeedItem

    public var id: String { article.articleID }

    public init(
        depth: DiscoveryResultDepth,
        retrieval: DiscoveryResultRetrieval = .keyword,
        article: ArticleFeedItem
    ) {
        self.depth = depth
        self.retrieval = retrieval
        self.article = article
    }

    enum CodingKeys: String, CodingKey {
        case depth
        case retrieval
        case article
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        depth = try container.decode(DiscoveryResultDepth.self, forKey: .depth)
        retrieval = try container.decodeIfPresent(
            DiscoveryResultRetrieval.self,
            forKey: .retrieval
        ) ?? .keyword
        article = try container.decode(ArticleFeedItem.self, forKey: .article)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(depth, forKey: .depth)
        try container.encode(retrieval, forKey: .retrieval)
        try container.encode(article, forKey: .article)
    }
}

public enum TimelineStatus: String, Codable, Equatable, Sendable {
    case fresh
    case stale
    case unavailable
}

public enum TimelineLane: String, Codable, Equatable, Sendable {
    case main
    case branch
}

public enum TimelineUpdateKind: String, Codable, Equatable, Sendable {
    case existing
    case inserted
    case occurred
}

public enum TimelineDateSource: String, Codable, Equatable, Sendable {
    case provider
    case firstSeen = "first_seen"
}

private enum TimelineDateOrder {
    static let timeZone = TimeZone(identifier: "Asia/Seoul")
        ?? TimeZone(secondsFromGMT: 9 * 60 * 60)!

    static let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        return calendar
    }()

    static func day(_ date: Date) -> Date {
        calendar.startOfDay(for: date)
    }
}

public struct FeaturedTimelineNode: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let lane: TimelineLane
    public let anchorNodeID: UUID?
    public let updateKind: TimelineUpdateKind
    public let publishedAt: Date
    public let dateSource: TimelineDateSource
    public let article: ArticleFeedItem

    public init(
        id: UUID,
        lane: TimelineLane,
        anchorNodeID: UUID?,
        updateKind: TimelineUpdateKind,
        publishedAt: Date,
        dateSource: TimelineDateSource,
        article: ArticleFeedItem
    ) {
        self.id = id
        self.lane = lane
        self.anchorNodeID = anchorNodeID
        self.updateKind = updateKind
        self.publishedAt = publishedAt
        self.dateSource = dateSource
        self.article = article
    }

    enum CodingKeys: String, CodingKey {
        case id
        case lane
        case anchorNodeID = "anchor_node_id"
        case updateKind = "update_kind"
        case publishedAt = "published_at"
        case dateSource = "date_source"
        case article
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        lane = try container.decode(TimelineLane.self, forKey: .lane)
        anchorNodeID = try container.decodeIfPresent(UUID.self, forKey: .anchorNodeID)
        updateKind = try container.decode(TimelineUpdateKind.self, forKey: .updateKind)
        publishedAt = try container.decode(Date.self, forKey: .publishedAt)
        dateSource = try container.decode(TimelineDateSource.self, forKey: .dateSource)
        article = try container.decode(ArticleFeedItem.self, forKey: .article)
        guard (lane == .main) == (anchorNodeID == nil) else {
            throw DecodingError.dataCorruptedError(
                forKey: .anchorNodeID,
                in: container,
                debugDescription: "Timeline branch anchor does not match its lane"
            )
        }
    }
}

public struct FeaturedTimeline: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let rank: Int
    public let stableKey: String
    public let title: String
    public let summary: String
    public let topic: String
    public let startedAt: Date
    public let endedAt: Date
    public let nodes: [FeaturedTimelineNode]

    public init(
        id: UUID,
        rank: Int,
        stableKey: String,
        title: String,
        summary: String,
        topic: String,
        startedAt: Date,
        endedAt: Date,
        nodes: [FeaturedTimelineNode]
    ) {
        self.id = id
        self.rank = rank
        self.stableKey = stableKey
        self.title = title
        self.summary = summary
        self.topic = topic
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.nodes = nodes
    }

    enum CodingKeys: String, CodingKey {
        case id
        case rank
        case stableKey = "stable_key"
        case title
        case summary
        case topic
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case nodes
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        rank = try container.decode(Int.self, forKey: .rank)
        stableKey = try container.decode(String.self, forKey: .stableKey)
        title = try container.decode(String.self, forKey: .title)
        summary = try container.decode(String.self, forKey: .summary)
        topic = try container.decode(String.self, forKey: .topic)
        startedAt = try container.decode(Date.self, forKey: .startedAt)
        endedAt = try container.decode(Date.self, forKey: .endedAt)
        nodes = try container.decode([FeaturedTimelineNode].self, forKey: .nodes)

        let nodeIDs = nodes.map(\.id)
        let mainIDs = Set(nodes.lazy.filter { $0.lane == .main }.map(\.id))
        guard (1...8).contains(rank),
              (1...128).contains(stableKey.count),
              (4...100).contains(title.count),
              (20...240).contains(summary.count),
              (1...100).contains(topic.count),
              startedAt <= endedAt,
              (4...8).contains(nodes.count),
              Set(nodeIDs).count == nodeIDs.count,
              mainIDs.count >= 3,
              nodes.allSatisfy({ node in
                  node.lane == .main || node.anchorNodeID.map(mainIDs.contains) == true
              })
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .nodes,
                in: container,
                debugDescription: "Invalid featured timeline tree"
            )
        }
    }

    public var chronologicalNodes: [FeaturedTimelineNode] {
        nodes.sorted {
            let leftDay = TimelineDateOrder.day($0.publishedAt)
            let rightDay = TimelineDateOrder.day($1.publishedAt)
            if leftDay != rightDay {
                return leftDay < rightDay
            }
            return $0.article.articleID < $1.article.articleID
        }
    }
}

public struct DiscoveryResponse: Codable, Equatable, Sendable {
    public let formatVersion: Int
    public let generatedAt: Date?
    public let expiresAt: Date?
    public let representativeRouteID: UUID?
    public let routes: [DiscoveryRoute]
    public let searchSuggestions: [String]
    public let trendingSearchSuggestions: [String]
    public let query: String?
    public let results: [DiscoverySearchResult]
    public let cooldownSeconds: Double
    public let collection: DiscoveryCollection?
    public let timelineStatus: TimelineStatus
    public let timelinesGeneratedAt: Date?
    public let timelinesRefreshedAt: Date?
    public let featuredTimelines: [FeaturedTimeline]
    public let timelineResults: [FeaturedTimeline]
    public let nextCursor: String?

    public var representativeRoute: DiscoveryRoute? {
        if let representativeRouteID,
           let match = routes.first(where: { $0.id == representativeRouteID }) {
            return match
        }
        return routes.first(where: \.isRepresentative) ?? routes.first
    }

    public init(
        formatVersion: Int = 1,
        generatedAt: Date?,
        expiresAt: Date?,
        representativeRouteID: UUID?,
        routes: [DiscoveryRoute],
        searchSuggestions: [String],
        trendingSearchSuggestions: [String] = [],
        query: String?,
        results: [DiscoverySearchResult],
        cooldownSeconds: Double,
        collection: DiscoveryCollection? = nil,
        timelineStatus: TimelineStatus = .unavailable,
        timelinesGeneratedAt: Date? = nil,
        timelinesRefreshedAt: Date? = nil,
        featuredTimelines: [FeaturedTimeline] = [],
        timelineResults: [FeaturedTimeline] = [],
        nextCursor: String? = nil
    ) {
        self.formatVersion = formatVersion
        self.generatedAt = generatedAt
        self.expiresAt = expiresAt
        self.representativeRouteID = representativeRouteID
        self.routes = routes
        self.searchSuggestions = searchSuggestions
        self.trendingSearchSuggestions = trendingSearchSuggestions
        self.query = query
        self.results = results
        self.cooldownSeconds = cooldownSeconds
        self.collection = collection
        self.timelineStatus = timelineStatus
        self.timelinesGeneratedAt = timelinesGeneratedAt
        self.timelinesRefreshedAt = timelinesRefreshedAt
        self.featuredTimelines = featuredTimelines
        self.timelineResults = timelineResults
        self.nextCursor = nextCursor
    }

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case generatedAt = "generated_at"
        case expiresAt = "expires_at"
        case representativeRouteID = "representative_route_id"
        case routes
        case searchSuggestions = "search_suggestions"
        case trendingSearchSuggestions = "trending_search_suggestions"
        case query
        case results
        case cooldownSeconds = "cooldown_seconds"
        case collection
        case timelineStatus = "timeline_status"
        case timelinesGeneratedAt = "timelines_generated_at"
        case timelinesRefreshedAt = "timelines_refreshed_at"
        case featuredTimelines = "featured_timelines"
        case timelineResults = "timeline_results"
        case nextCursor = "next_cursor"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        formatVersion = try container.decode(Int.self, forKey: .formatVersion)
        generatedAt = try container.decodeIfPresent(Date.self, forKey: .generatedAt)
        expiresAt = try container.decodeIfPresent(Date.self, forKey: .expiresAt)
        representativeRouteID = try container.decodeIfPresent(
            UUID.self,
            forKey: .representativeRouteID
        )
        routes = try container.decode([DiscoveryRoute].self, forKey: .routes)
        searchSuggestions = try container.decode([String].self, forKey: .searchSuggestions)

        let trending = try container.decodeIfPresent(
            [String].self,
            forKey: .trendingSearchSuggestions
        ) ?? []
        guard trending.count <= 3 else {
            throw DecodingError.dataCorruptedError(
                forKey: .trendingSearchSuggestions,
                in: container,
                debugDescription: "Trending search suggestions must contain at most three values"
            )
        }
        var seenTrending = Set<String>()
        for value in trending {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard value == trimmed,
                  (2...60).contains(value.count),
                  value.unicodeScalars.allSatisfy({
                      !CharacterSet.controlCharacters.contains($0)
                  }),
                  seenTrending.insert(value).inserted
            else {
                throw DecodingError.dataCorruptedError(
                    forKey: .trendingSearchSuggestions,
                    in: container,
                    debugDescription: "Trending search suggestions are invalid or duplicated"
                )
            }
        }
        trendingSearchSuggestions = trending

        query = try container.decodeIfPresent(String.self, forKey: .query)
        results = try container.decode([DiscoverySearchResult].self, forKey: .results)
        cooldownSeconds = try container.decode(Double.self, forKey: .cooldownSeconds)
        collection = try container.decodeIfPresent(DiscoveryCollection.self, forKey: .collection)
        timelineStatus = try container.decodeIfPresent(
            TimelineStatus.self,
            forKey: .timelineStatus
        ) ?? .unavailable
        timelinesGeneratedAt = try container.decodeIfPresent(
            Date.self,
            forKey: .timelinesGeneratedAt
        )
        timelinesRefreshedAt = try container.decodeIfPresent(
            Date.self,
            forKey: .timelinesRefreshedAt
        )
        featuredTimelines = try container.decodeIfPresent(
            [FeaturedTimeline].self,
            forKey: .featuredTimelines
        ) ?? []
        guard featuredTimelines.count <= 8,
              Set(featuredTimelines.map(\.id)).count == featuredTimelines.count,
              Set(featuredTimelines.map(\.stableKey)).count == featuredTimelines.count
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .featuredTimelines,
                in: container,
                debugDescription: "Featured timelines must be unique and bounded"
            )
        }
        timelineResults = try container.decodeIfPresent(
            [FeaturedTimeline].self,
            forKey: .timelineResults
        ) ?? []
        guard timelineResults.count <= 8,
              Set(timelineResults.map(\.id)).count == timelineResults.count,
              Set(timelineResults.map(\.stableKey)).count == timelineResults.count
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .timelineResults,
                in: container,
                debugDescription: "Timeline search results must be unique and bounded"
            )
        }
        nextCursor = try container.decodeIfPresent(String.self, forKey: .nextCursor)
    }
}

public struct APIErrorDetail: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
    public let retryAfterSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case code
        case message
        case retryAfterSeconds = "retry_after_seconds"
    }
}

public struct APIErrorEnvelope: Codable, Equatable, Sendable {
    public let error: APIErrorDetail
}

public enum APIJSON {
    public static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(formatRFC3339(date))
        }
        return encoder
    }

    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            guard let date = parseRFC3339(value) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Invalid RFC 3339 timestamp"
                )
            }
            return date
        }
        return decoder
    }

    private static func parseRFC3339(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        let standard = ISO8601DateFormatter()
        standard.formatOptions = [.withInternetDateTime]
        return standard.date(from: value)
    }

    private static func formatRFC3339(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: date)
    }
}

public enum CanonicalJSON {
    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        try APIJSON.makeEncoder().encode(value)
    }
}

private extension Optional {
    func unwrap(or error: @autoclosure () -> Error) throws -> Wrapped {
        guard let value = self else { throw error() }
        return value
    }
}
