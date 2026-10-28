import AuthenticationServices
import ArticleFeedKit
import CryptoKit
import Foundation
import Security

struct AccountProfile: Codable, Equatable, Sendable, Identifiable {
    let id: UUID
    let displayName: String?
    let email: String?
}

struct AccountSession: Codable, Equatable, Sendable {
    let sessionToken: String
    let expiresAt: Date
    let appleUserID: String
    let account: AccountProfile
}

struct AccountDeletionResult: Codable, Equatable, Sendable {
    let deleted: Bool
    let appleTokenRevoked: Bool
}

struct TimelineSubscriptionMutation: Codable, Equatable, Sendable {
    let stableKey: String
    let subscribed: Bool
    let subscribedAt: Date?
}

struct TimelineSubscriptionDetail: Codable, Equatable, Sendable, Identifiable {
    let stableKey: String
    let title: String?
    let summary: String?
    let startedAt: Date?
    let endedAt: Date?
    let subscribedAt: Date

    var id: String { stableKey }
}

enum TimelineSubscriptionEventType: String, Codable, Sendable {
    case subscribe
    case unsubscribe
}

struct TimelineSubscriptionHistoryEvent: Codable, Equatable, Sendable, Identifiable {
    let id: UUID
    let stableKey: String
    let eventType: TimelineSubscriptionEventType
    let occurredAt: Date
    let isCurrentlySubscribed: Bool
    let title: String?
}

enum ArticleFeedbackReason: String, CaseIterable, Codable, Sendable, Identifiable {
    case notRelevant = "not_relevant"
    case tooSimple = "too_simple"
    case tooDifficult = "too_difficult"
    case seenSimilar = "seen_similar"
    case distrustSource = "distrust_source"
    case other

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .notRelevant: return "관심사와 관련 없어요"
        case .tooSimple: return "기사가 너무 단순해요"
        case .tooDifficult: return "기사가 너무 어려워요"
        case .seenSimilar: return "비슷한 내용을 이미 봤어요"
        case .distrustSource: return "출처를 신뢰하기 어려워요"
        case .other: return "기타"
        }
    }
}

struct ArticleFeedbackRecord: Codable, Equatable, Sendable {
    let articleID: String
    let reason: ArticleFeedbackReason
    let createdAt: Date
    let updatedAt: Date
}

struct AccountReaderSettingsWire: Codable, Equatable, Sendable {
    enum TypeSize: String, Codable, Sendable {
        case small
        case standard
        case large
    }

    enum Leading: String, Codable, Sendable {
        case compact
        case comfortable
    }

    enum Theme: String, Codable, Sendable {
        case system
        case light
        case dark
    }

    let typeSize: TypeSize
    let leading: Leading
    let theme: Theme
    let motionEnabled: Bool
    let mediaSaver: Bool
    let offlineCache: Bool

    private enum CodingKeys: String, CodingKey {
        case typeSize = "type_size"
        case leading
        case theme
        case motionEnabled = "motion_enabled"
        case mediaSaver = "media_saver"
        case offlineCache = "offline_cache"
    }

    init(_ settings: ReaderSettings) {
        switch settings.typeSize {
        case .small: typeSize = .small
        case .standard: typeSize = .standard
        case .large: typeSize = .large
        }
        switch settings.leading {
        case .compact: leading = .compact
        case .comfortable: leading = .comfortable
        }
        switch settings.theme {
        case .system: theme = .system
        case .light: theme = .light
        case .dark: theme = .dark
        }
        motionEnabled = settings.motionEnabled
        mediaSaver = settings.mediaSaver
        offlineCache = settings.offlineCache
    }

    var appSettings: ReaderSettings {
        let appTypeSize: ReaderTypeSize
        switch typeSize {
        case .small: appTypeSize = .small
        case .standard: appTypeSize = .standard
        case .large: appTypeSize = .large
        }
        let appLeading: ReaderLeading
        switch leading {
        case .compact: appLeading = .compact
        case .comfortable: appLeading = .comfortable
        }
        let appTheme: ReaderTheme
        switch theme {
        case .system: appTheme = .system
        case .light: appTheme = .light
        case .dark: appTheme = .dark
        }
        return ReaderSettings(
            typeSize: appTypeSize,
            leading: appLeading,
            theme: appTheme,
            motionEnabled: motionEnabled,
            mediaSaver: mediaSaver,
            offlineCache: offlineCache
        )
    }
}

struct AccountSavedArticle: Codable, Equatable, Sendable {
    let savedAt: Date
    let article: ArticleFeedItem

    private enum CodingKeys: String, CodingKey {
        case savedAt = "saved_at"
        case article
    }
}

struct AccountReadingState: Codable, Equatable, Sendable {
    var revision: Int
    var savedArticles: [AccountSavedArticle]
    var sceneProgress: [String: Int]
    var lastArticleID: String?
    var settings: ReaderSettings

    private enum CodingKeys: String, CodingKey {
        case revision
        case savedArticles = "saved_articles"
        case sceneProgress = "scene_progress"
        case lastArticleID = "last_article_id"
        case settings
    }

    init(
        revision: Int = 0,
        savedArticles: [AccountSavedArticle] = [],
        sceneProgress: [String: Int] = [:],
        lastArticleID: String? = nil,
        settings: ReaderSettings = ReaderSettings()
    ) {
        self.revision = revision
        self.savedArticles = savedArticles
        self.sceneProgress = sceneProgress
        self.lastArticleID = lastArticleID
        self.settings = settings
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        revision = try container.decode(Int.self, forKey: .revision)
        savedArticles = try container.decode([AccountSavedArticle].self, forKey: .savedArticles)
        sceneProgress = try container.decode([String: Int].self, forKey: .sceneProgress)
        lastArticleID = try container.decodeIfPresent(String.self, forKey: .lastArticleID)
        settings = try container.decode(AccountReaderSettingsWire.self, forKey: .settings).appSettings

        let savedIDs = savedArticles.map(\.article.articleID)
        guard revision >= 0,
              savedArticles.count <= 100,
              Set(savedIDs).count == savedIDs.count,
              sceneProgress.count <= 500,
              savedIDs.allSatisfy(Self.isValidArticleID),
              sceneProgress.allSatisfy({ Self.isValidArticleID($0.key) && (0...999).contains($0.value) }),
              lastArticleID.map(Self.isValidArticleID) ?? true else {
            throw DecodingError.dataCorruptedError(
                forKey: .revision,
                in: container,
                debugDescription: "invalid account reading state"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(revision, forKey: .revision)
        try container.encode(savedArticles, forKey: .savedArticles)
        try container.encode(sceneProgress, forKey: .sceneProgress)
        try container.encodeIfPresent(lastArticleID, forKey: .lastArticleID)
        try container.encode(AccountReaderSettingsWire(settings), forKey: .settings)
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains(where: { $0 == "\n" || $0 == "\r" || $0 == "\0" })
    }
}

struct AccountSavedChange: Codable, Equatable, Sendable {
    let articleID: String
    let saved: Bool
    let savedAt: Date?

    private enum CodingKeys: String, CodingKey {
        case articleID = "article_id"
        case saved
        case savedAt = "saved_at"
    }
}

struct AccountProgressChange: Codable, Equatable, Sendable {
    let articleID: String
    let sceneIndex: Int

    private enum CodingKeys: String, CodingKey {
        case articleID = "article_id"
        case sceneIndex = "scene_index"
    }
}

struct AccountReadingPatch: Codable, Equatable, Sendable {
    var savedChanges: [AccountSavedChange] = []
    var progressChanges: [AccountProgressChange] = []
    var settings: AccountReaderSettingsWire?
    var updateLastArticle = false
    var lastArticleID: String?

    private enum CodingKeys: String, CodingKey {
        case savedChanges = "saved_changes"
        case progressChanges = "progress_changes"
        case settings
        case updateLastArticle = "update_last_article"
        case lastArticleID = "last_article_id"
    }

    var isEmpty: Bool {
        savedChanges.isEmpty
            && progressChanges.isEmpty
            && settings == nil
            && !updateLastArticle
    }

    var isValid: Bool {
        let savedIDs = savedChanges.map(\.articleID)
        let progressIDs = progressChanges.map(\.articleID)
        return savedChanges.count <= 100
            && progressChanges.count <= 500
            && Set(savedIDs).count == savedIDs.count
            && Set(progressIDs).count == progressIDs.count
            && savedChanges.allSatisfy {
                Self.isValidArticleID($0.articleID) && ($0.saved == ($0.savedAt != nil))
            }
            && progressChanges.allSatisfy {
                Self.isValidArticleID($0.articleID) && (0...999).contains($0.sceneIndex)
            }
            && (updateLastArticle || lastArticleID == nil)
            && (lastArticleID.map(Self.isValidArticleID) ?? true)
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains(where: { $0 == "\n" || $0 == "\r" || $0 == "\0" })
    }
}

enum AccountActivityEventType: String, Codable, Sendable {
    case search
    case articleOpened = "article_opened"
    case sceneViewed = "scene_viewed"
    case articleCompleted = "article_completed"
}

struct AccountActivityEvent: Codable, Equatable, Sendable, Identifiable {
    let eventID: UUID
    let eventType: AccountActivityEventType
    let occurredAt: Date
    let timeZone: String
    let utcOffsetMinutes: Int
    let query: String?
    let resultCount: Int?
    let articleID: String?
    let sceneIndex: Int?
    let sceneCount: Int?

    var id: UUID { eventID }

    private enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case eventType = "event_type"
        case occurredAt = "occurred_at"
        case timeZone = "time_zone"
        case utcOffsetMinutes = "utc_offset_minutes"
        case query
        case resultCount = "result_count"
        case articleID = "article_id"
        case sceneIndex = "scene_index"
        case sceneCount = "scene_count"
    }

    static func search(
        query: String,
        resultCount: Int,
        occurredAt: Date = Date(),
        timeZone: TimeZone = .current
    ) -> AccountActivityEvent? {
        let normalized = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (2...80).contains(normalized.count),
              !normalized.contains(where: Self.isControlLine),
              (0...1000).contains(resultCount),
              let context = timeContext(at: occurredAt, in: timeZone)
        else { return nil }
        return AccountActivityEvent(
            eventID: UUID(),
            eventType: .search,
            occurredAt: occurredAt,
            timeZone: context.identifier,
            utcOffsetMinutes: context.offsetMinutes,
            query: normalized,
            resultCount: resultCount,
            articleID: nil,
            sceneIndex: nil,
            sceneCount: nil
        )
    }

    static func article(
        _ type: AccountActivityEventType,
        articleID: String,
        sceneIndex: Int,
        sceneCount: Int,
        occurredAt: Date = Date(),
        timeZone: TimeZone = .current
    ) -> AccountActivityEvent? {
        guard type != .search,
              isValidArticleID(articleID),
              (0...999).contains(sceneIndex),
              (1...1000).contains(sceneCount),
              sceneIndex < sceneCount,
              type != .articleCompleted || sceneIndex == sceneCount - 1,
              let context = timeContext(at: occurredAt, in: timeZone)
        else { return nil }
        return AccountActivityEvent(
            eventID: UUID(),
            eventType: type,
            occurredAt: occurredAt,
            timeZone: context.identifier,
            utcOffsetMinutes: context.offsetMinutes,
            query: nil,
            resultCount: nil,
            articleID: articleID,
            sceneIndex: sceneIndex,
            sceneCount: sceneCount
        )
    }

    var isValid: Bool {
        guard Self.isValidTimeZoneIdentifier(timeZone),
              (-720...840).contains(utcOffsetMinutes)
        else { return false }
        switch eventType {
        case .search:
            guard let query, let resultCount else { return false }
            return (2...80).contains(query.count)
                && !query.contains(where: Self.isControlLine)
                && (0...1000).contains(resultCount)
                && articleID == nil
                && sceneIndex == nil
                && sceneCount == nil
        case .articleOpened, .sceneViewed, .articleCompleted:
            guard let articleID, let sceneIndex, let sceneCount,
                  Self.isValidArticleID(articleID),
                  (0...999).contains(sceneIndex),
                  (1...1000).contains(sceneCount),
                  sceneIndex < sceneCount,
                  query == nil,
                  resultCount == nil
            else { return false }
            return eventType != .articleCompleted || sceneIndex == sceneCount - 1
        }
    }

    private static func timeContext(
        at date: Date,
        in timeZone: TimeZone
    ) -> (identifier: String, offsetMinutes: Int)? {
        let identifier = timeZone.identifier.isEmpty ? "GMT" : timeZone.identifier
        let offsetMinutes = timeZone.secondsFromGMT(for: date) / 60
        guard isValidTimeZoneIdentifier(identifier),
              (-720...840).contains(offsetMinutes)
        else { return nil }
        return (identifier, offsetMinutes)
    }

    private static func isValidTimeZoneIdentifier(_ value: String) -> Bool {
        guard (1...64).contains(value.count) else { return false }
        let allowed = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+./-"
        )
        return value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains(where: isControlLine)
    }

    private static func isControlLine(_ character: Character) -> Bool {
        character == "\n" || character == "\r" || character == "\0"
    }
}

struct AccountActivityBatchResponse: Codable, Equatable, Sendable {
    let accepted: Int
    let duplicates: Int
}

enum RecommendationSurface: String, Codable, Sendable {
    case home
    case articleBoundary = "article_boundary"
}

enum RecommendationStrategy: String, Codable, Sendable {
    case personalized
    case blendedColdStart = "blended_cold_start"
}

struct RecommendationTimeContext: Codable, Equatable, Sendable {
    let occurredAt: Date
    let timeZone: String
    let utcOffsetMinutes: Int

    private enum CodingKeys: String, CodingKey {
        case occurredAt = "occurred_at"
        case timeZone = "time_zone"
        case utcOffsetMinutes = "utc_offset_minutes"
    }

    init?(
        occurredAt: Date = Date(),
        timeZone: TimeZone = .current
    ) {
        let identifier = timeZone.identifier.isEmpty ? "GMT" : timeZone.identifier
        let offsetMinutes = timeZone.secondsFromGMT(for: occurredAt) / 60
        guard Self.isValidTimeZone(identifier),
              (-720...840).contains(offsetMinutes)
        else { return nil }
        self.occurredAt = occurredAt
        self.timeZone = identifier
        utcOffsetMinutes = offsetMinutes
    }

    var isValid: Bool {
        Self.isValidTimeZone(timeZone)
            && (-720...840).contains(utcOffsetMinutes)
    }

    private static func isValidTimeZone(_ value: String) -> Bool {
        guard (1...64).contains(value.count) else { return false }
        let allowed = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+./-"
        )
        return value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

struct AccountRecommendationRequest: Codable, Equatable, Sendable {
    let surface: RecommendationSurface
    let contextArticleID: String?
    let excludeArticleIDs: [String]
    let timeContext: RecommendationTimeContext
    let limit: Int
    let cursor: String?

    private enum CodingKeys: String, CodingKey {
        case surface
        case contextArticleID = "context_article_id"
        case excludeArticleIDs = "exclude_article_ids"
        case timeContext = "time_context"
        case limit
        case cursor
    }

    var isValid: Bool {
        let uniqueExclusions = Set(excludeArticleIDs)
        guard excludeArticleIDs.count <= 64,
              uniqueExclusions.count == excludeArticleIDs.count,
              excludeArticleIDs.allSatisfy(Self.isSHA256),
              contextArticleID.map(Self.isSHA256) ?? true,
              timeContext.isValid,
              (1...5).contains(limit),
              (cursor?.count ?? 0) <= 2_048
        else { return false }
        switch surface {
        case .home:
            return true
        case .articleBoundary:
            return contextArticleID != nil && limit == 3 && cursor == nil
        }
    }

    static func isSHA256(_ value: String) -> Bool {
        value.count == 64
            && value.unicodeScalars.allSatisfy {
                (48...57).contains($0.value) || (97...102).contains($0.value)
            }
    }
}

struct AccountRecommendationPage: Codable, Equatable, Sendable {
    let formatVersion: Int
    let surface: RecommendationSurface
    let strategy: RecommendationStrategy
    let rankingVersion: String
    let generatedAt: Date
    let items: [ArticleFeedItem]
    let nextCursor: String?

    private enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case surface
        case strategy
        case rankingVersion = "ranking_version"
        case generatedAt = "generated_at"
        case items
        case nextCursor = "next_cursor"
    }

    init(
        formatVersion: Int = 1,
        surface: RecommendationSurface,
        strategy: RecommendationStrategy,
        rankingVersion: String = "content-v1",
        generatedAt: Date,
        items: [ArticleFeedItem],
        nextCursor: String?
    ) {
        self.formatVersion = formatVersion
        self.surface = surface
        self.strategy = strategy
        self.rankingVersion = rankingVersion
        self.generatedAt = generatedAt
        self.items = items
        self.nextCursor = nextCursor
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        formatVersion = try container.decode(Int.self, forKey: .formatVersion)
        surface = try container.decode(RecommendationSurface.self, forKey: .surface)
        strategy = try container.decode(RecommendationStrategy.self, forKey: .strategy)
        rankingVersion = try container.decode(String.self, forKey: .rankingVersion)
        generatedAt = try container.decode(Date.self, forKey: .generatedAt)
        items = try container.decode([ArticleFeedItem].self, forKey: .items)
        nextCursor = try container.decodeIfPresent(String.self, forKey: .nextCursor)
        guard formatVersion == 1,
              rankingVersion == "content-v1",
              items.count <= (surface == .home ? 5 : 3),
              surface != .articleBoundary || nextCursor == nil,
              (nextCursor?.count ?? 0) <= 2_048
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .formatVersion,
                in: container,
                debugDescription: "invalid account recommendation page"
            )
        }
    }
}

struct PrivacyConsentLink: Codable, Equatable, Sendable {
    let consentID: UUID
    let accountID: UUID
    let policyVersion: String
    let policyURL: URL
    let acceptedAt: Date
    let linkedAt: Date

    private enum CodingKeys: String, CodingKey {
        case consentID = "consent_id"
        case accountID = "account_id"
        case policyVersion = "policy_version"
        case policyURL = "policy_url"
        case acceptedAt = "accepted_at"
        case linkedAt = "linked_at"
    }
}

struct AppleAuthorizationPayload: Equatable, Sendable {
    let appleUserID: String
    let identityToken: String
    let authorizationCode: String
    let rawNonce: String
    let givenName: String?
    let familyName: String?

    init(
        appleUserID: String,
        identityToken: String,
        authorizationCode: String,
        rawNonce: String,
        givenName: String? = nil,
        familyName: String? = nil
    ) {
        self.appleUserID = appleUserID
        self.identityToken = identityToken
        self.authorizationCode = authorizationCode
        self.rawNonce = rawNonce
        self.givenName = givenName
        self.familyName = familyName
    }

    init(credential: ASAuthorizationAppleIDCredential, rawNonce: String) throws {
        self = try Self.make(
            appleUserID: credential.user,
            authorizationCode: credential.authorizationCode,
            identityToken: credential.identityToken,
            rawNonce: rawNonce,
            fullName: credential.fullName
        )
    }

    static func make(
        appleUserID: String,
        authorizationCode: Data?,
        identityToken: Data?,
        rawNonce: String,
        fullName: PersonNameComponents? = nil
    ) throws -> AppleAuthorizationPayload {
        guard !appleUserID.isEmpty,
              !rawNonce.isEmpty,
              let authorizationCode,
              let identityToken,
              let code = String(data: authorizationCode, encoding: .utf8),
              let token = String(data: identityToken, encoding: .utf8),
              !code.isEmpty,
              !token.isEmpty else {
            throw AccountAuthenticationError.invalidAppleCredential
        }
        return AppleAuthorizationPayload(
            appleUserID: appleUserID,
            identityToken: token,
            authorizationCode: code,
            rawNonce: rawNonce,
            givenName: fullName?.givenName,
            familyName: fullName?.familyName
        )
    }
}

enum AccountStatus: Equatable, Sendable {
    case signedOut
    case restoring
    case signingIn
    case signedIn(AccountProfile)
    case deleting
    case error(message: String, cachedProfile: AccountProfile?)
}

enum AccountAuthenticationError: Error, Equatable {
    case invalidAppleCredential
    case secureStorage
    case noSession

    var userMessage: String {
        switch self {
        case .invalidAppleCredential:
            return "Apple 로그인 정보를 확인할 수 없어요. 다시 시도해 주세요."
        case .secureStorage:
            return "로그인 정보를 이 기기에 안전하게 저장하지 못했어요."
        case .noSession:
            return "로그인이 필요해요."
        }
    }
}

enum AccountAPIError: Error, Equatable {
    case unauthorized
    case privacyConsentRequired
    case invalidRequest
    case unavailable(String)
    case networkUnavailable
    case invalidResponse
    case server(String)

    var userMessage: String {
        switch self {
        case .unauthorized:
            return "로그인 정보가 만료됐어요. 다시 로그인해 주세요."
        case .privacyConsentRequired:
            return "새 개인정보처리방침에 동의하면 맞춤 기사를 받을 수 있어요."
        case .invalidRequest:
            return "Apple 로그인 정보를 처리하지 못했어요. 다시 시도해 주세요."
        case .unavailable:
            return "지금은 Apple 로그인을 사용할 수 없어요. 잠시 뒤 다시 시도해 주세요."
        case .networkUnavailable:
            return "인터넷 연결을 확인한 뒤 다시 시도해 주세요."
        case .invalidResponse, .server:
            return "계정 요청을 완료하지 못했어요. 잠시 뒤 다시 시도해 주세요."
        }
    }
}

enum AppleSignInNonce {
    struct Pair: Equatable, Sendable {
        let raw: String
        let hashed: String
    }

    static func make() throws -> Pair {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw AccountAuthenticationError.invalidAppleCredential
        }
        return make(bytes: bytes)
    }

    static func make(bytes: [UInt8]) -> Pair {
        let raw = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        let digest = SHA256.hash(data: Data(raw.utf8))
        return Pair(raw: raw, hashed: digest.map { String(format: "%02x", $0) }.joined())
    }
}

protocol AccountServicing: AnyObject {
    func restore() async -> AccountSession?
    func signIn(
        _ payload: AppleAuthorizationPayload,
        privacyConsentID: UUID
    ) async throws -> AccountSession
    func signOut() async
    func deleteAccount() async throws -> AccountDeletionResult
    func submitArticleFeedback(
        articleID: String,
        reason: ArticleFeedbackReason
    ) async throws -> ArticleFeedbackRecord
    func submitActivityEvents(
        _ events: [AccountActivityEvent]
    ) async throws -> AccountActivityBatchResponse
    func fetchRecommendations(
        _ request: AccountRecommendationRequest
    ) async throws -> AccountRecommendationPage
    func linkPrivacyConsent(_ consentID: UUID) async throws -> PrivacyConsentLink
    func fetchReadingState() async throws -> AccountReadingState
    func patchReadingState(_ patch: AccountReadingPatch) async throws -> AccountReadingState
    func fetchTimelineSubscriptions() async throws -> Set<String>
    func fetchTimelineSubscriptionDetails() async throws -> [TimelineSubscriptionDetail]
    func fetchTimelineSubscriptionHistory() async throws -> [TimelineSubscriptionHistoryEvent]
    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool
    ) async throws -> Bool
}

protocol AccountAPIRequesting: AnyObject {
    func signIn(
        _ payload: AppleAuthorizationPayload,
        privacyConsentID: UUID
    ) async throws -> AccountSession
    func profile(sessionToken: String) async throws -> AccountProfile
    func logout(sessionToken: String) async throws
    func deleteAccount(sessionToken: String) async throws -> AccountDeletionResult
    func submitArticleFeedback(
        articleID: String,
        reason: ArticleFeedbackReason,
        sessionToken: String
    ) async throws -> ArticleFeedbackRecord
    func submitActivityEvents(
        _ events: [AccountActivityEvent],
        sessionToken: String
    ) async throws -> AccountActivityBatchResponse
    func fetchRecommendations(
        _ request: AccountRecommendationRequest,
        sessionToken: String
    ) async throws -> AccountRecommendationPage
    func linkPrivacyConsent(
        _ consentID: UUID,
        sessionToken: String
    ) async throws -> PrivacyConsentLink
    func fetchReadingState(sessionToken: String) async throws -> AccountReadingState
    func patchReadingState(
        _ patch: AccountReadingPatch,
        sessionToken: String
    ) async throws -> AccountReadingState
    func fetchTimelineSubscriptions(sessionToken: String) async throws -> Set<String>
    func fetchTimelineSubscriptionDetails(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionDetail]
    func fetchTimelineSubscriptionHistory(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionHistoryEvent]
    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool,
        sessionToken: String
    ) async throws -> Bool
}

extension AccountServicing {
    func fetchRecommendations(
        _ request: AccountRecommendationRequest
    ) async throws -> AccountRecommendationPage {
        throw AccountAPIError.unavailable("recommendation_unavailable")
    }

    func linkPrivacyConsent(_ consentID: UUID) async throws -> PrivacyConsentLink {
        throw AccountAPIError.unavailable("privacy_consent_unavailable")
    }

    func fetchTimelineSubscriptions() async throws -> Set<String> {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func fetchTimelineSubscriptionDetails() async throws -> [TimelineSubscriptionDetail] {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func fetchTimelineSubscriptionHistory() async throws -> [TimelineSubscriptionHistoryEvent] {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool
    ) async throws -> Bool {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }
}

extension AccountAPIRequesting {
    func fetchRecommendations(
        _ request: AccountRecommendationRequest,
        sessionToken: String
    ) async throws -> AccountRecommendationPage {
        throw AccountAPIError.unavailable("recommendation_unavailable")
    }

    func linkPrivacyConsent(
        _ consentID: UUID,
        sessionToken: String
    ) async throws -> PrivacyConsentLink {
        throw AccountAPIError.unavailable("privacy_consent_unavailable")
    }

    func fetchTimelineSubscriptions(sessionToken: String) async throws -> Set<String> {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func fetchTimelineSubscriptionDetails(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionDetail] {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func fetchTimelineSubscriptionHistory(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionHistoryEvent] {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }

    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool,
        sessionToken: String
    ) async throws -> Bool {
        throw AccountAPIError.unavailable("timeline_subscriptions_unavailable")
    }
}

protocol AccountHTTPTransport: AnyObject {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}

final class URLSessionAccountHTTPTransport: AccountHTTPTransport {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
}

final class AccountAPIClient: AccountAPIRequesting {
    private let baseURL: URL
    private let transport: any AccountHTTPTransport

    init(
        baseURL: URL,
        transport: any AccountHTTPTransport = URLSessionAccountHTTPTransport()
    ) {
        self.baseURL = baseURL
        self.transport = transport
    }

    func signIn(
        _ payload: AppleAuthorizationPayload,
        privacyConsentID: UUID
    ) async throws -> AccountSession {
        let requestBody = AppleSignInRequest(
            privacyConsentID: privacyConsentID,
            identityToken: payload.identityToken,
            authorizationCode: payload.authorizationCode,
            nonce: payload.rawNonce,
            givenName: payload.givenName,
            familyName: payload.familyName
        )
        let data = try await send(
            path: "v1/account/apple",
            method: "POST",
            body: try Self.makeEncoder().encode(requestBody)
        )
        let response: AccountSessionResponse = try decode(data)
        return AccountSession(
            sessionToken: response.sessionToken,
            expiresAt: response.expiresAt,
            appleUserID: payload.appleUserID,
            account: response.account
        )
    }

    func profile(sessionToken: String) async throws -> AccountProfile {
        let data = try await send(
            path: "v1/account/me",
            method: "GET",
            sessionToken: sessionToken
        )
        return try decode(data)
    }

    func logout(sessionToken: String) async throws {
        _ = try await send(
            path: "v1/account/logout",
            method: "POST",
            sessionToken: sessionToken
        )
    }

    func deleteAccount(sessionToken: String) async throws -> AccountDeletionResult {
        let data = try await send(
            path: "v1/account",
            method: "DELETE",
            sessionToken: sessionToken
        )
        return try decode(data)
    }

    func submitArticleFeedback(
        articleID: String,
        reason: ArticleFeedbackReason,
        sessionToken: String
    ) async throws -> ArticleFeedbackRecord {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        guard !articleID.isEmpty,
              articleID.count <= 128,
              let encodedArticleID = articleID.addingPercentEncoding(withAllowedCharacters: allowed)
        else {
            throw AccountAPIError.invalidRequest
        }
        let data = try await send(
            path: "v1/account/article-feedback/\(encodedArticleID)",
            method: "PUT",
            sessionToken: sessionToken,
            body: try Self.makeEncoder().encode(ArticleFeedbackRequest(reason: reason))
        )
        let response: ArticleFeedbackResponse = try decode(data)
        guard response.articleId == articleID else {
            throw AccountAPIError.invalidResponse
        }
        return ArticleFeedbackRecord(
            articleID: response.articleId,
            reason: response.reason,
            createdAt: response.createdAt,
            updatedAt: response.updatedAt
        )
    }

    func submitActivityEvents(
        _ events: [AccountActivityEvent],
        sessionToken: String
    ) async throws -> AccountActivityBatchResponse {
        let eventIDs = events.map(\.eventID)
        guard (1...100).contains(events.count),
              Set(eventIDs).count == eventIDs.count,
              events.allSatisfy(\.isValid)
        else {
            throw AccountAPIError.invalidRequest
        }
        let body = try Self.makeActivityEncoder().encode(
            AccountActivityBatchRequest(events: events)
        )
        guard body.count <= 128 * 1024 else {
            throw AccountAPIError.invalidRequest
        }
        let data = try await send(
            path: "v1/account/activity-events",
            method: "POST",
            sessionToken: sessionToken,
            body: body
        )
        let response: AccountActivityBatchResponse = try decode(data)
        guard response.accepted >= 0,
              response.duplicates >= 0,
              response.accepted + response.duplicates == events.count
        else {
            throw AccountAPIError.invalidResponse
        }
        return response
    }

    func fetchRecommendations(
        _ request: AccountRecommendationRequest,
        sessionToken: String
    ) async throws -> AccountRecommendationPage {
        guard request.isValid else { throw AccountAPIError.invalidRequest }
        let body = try Self.makeActivityEncoder().encode(request)
        guard body.count <= 128 * 1_024 else {
            throw AccountAPIError.invalidRequest
        }
        let data = try await send(
            path: "v1/account/recommendations",
            method: "POST",
            sessionToken: sessionToken,
            body: body
        )
        let response: AccountRecommendationPage = try decodeAccountWire(data)
        guard response.surface == request.surface else {
            throw AccountAPIError.invalidResponse
        }
        return response
    }

    func linkPrivacyConsent(
        _ consentID: UUID,
        sessionToken: String
    ) async throws -> PrivacyConsentLink {
        let data = try await send(
            path: "v1/account/privacy-consents/\(consentID.uuidString.lowercased())",
            method: "POST",
            sessionToken: sessionToken
        )
        let response: PrivacyConsentLink = try decodeAccountWire(data)
        guard response.consentID == consentID else {
            throw AccountAPIError.invalidResponse
        }
        return response
    }

    func fetchReadingState(sessionToken: String) async throws -> AccountReadingState {
        let data = try await send(
            path: "v1/account/reading-state",
            method: "GET",
            sessionToken: sessionToken
        )
        return try decodeReadingState(data)
    }

    func patchReadingState(
        _ patch: AccountReadingPatch,
        sessionToken: String
    ) async throws -> AccountReadingState {
        guard !patch.isEmpty, patch.isValid else { throw AccountAPIError.invalidRequest }
        let body = try Self.makeReadingEncoder().encode(patch)
        guard body.count <= 128 * 1024 else { throw AccountAPIError.invalidRequest }
        let data = try await send(
            path: "v1/account/reading-state",
            method: "PATCH",
            sessionToken: sessionToken,
            body: body
        )
        return try decodeReadingState(data)
    }

    func fetchTimelineSubscriptions(sessionToken: String) async throws -> Set<String> {
        let data = try await send(
            path: "v1/account/timeline-subscriptions",
            method: "GET",
            sessionToken: sessionToken
        )
        let response: TimelineSubscriptionsResponse = try decode(data)
        guard response.stableKeys.count <= 64,
              response.stableKeys.allSatisfy(Self.isValidTimelineKey),
              Set(response.stableKeys).count == response.stableKeys.count
        else {
            throw AccountAPIError.invalidResponse
        }
        return Set(response.stableKeys)
    }

    func fetchTimelineSubscriptionDetails(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionDetail] {
        let data = try await send(
            path: "v1/account/timeline-subscriptions",
            method: "GET",
            sessionToken: sessionToken
        )
        let response: TimelineSubscriptionsResponse = try decode(data)
        guard response.subscriptions.count <= 64,
              response.subscriptions.allSatisfy({ Self.isValidTimelineKey($0.stableKey) }),
              Set(response.subscriptions.map(\.stableKey)).count == response.subscriptions.count
        else { throw AccountAPIError.invalidResponse }
        return response.subscriptions
    }

    func fetchTimelineSubscriptionHistory(
        sessionToken: String
    ) async throws -> [TimelineSubscriptionHistoryEvent] {
        let data = try await send(
            path: "v1/account/timeline-subscriptions/history",
            method: "GET",
            sessionToken: sessionToken
        )
        let response: TimelineSubscriptionHistoryResponse = try decode(data)
        guard response.events.count <= 100,
              response.events.allSatisfy({ Self.isValidTimelineKey($0.stableKey) }),
              Set(response.events.map(\.id)).count == response.events.count
        else { throw AccountAPIError.invalidResponse }
        return response.events
    }

    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool,
        sessionToken: String
    ) async throws -> Bool {
        guard Self.isValidTimelineKey(timelineKey) else {
            throw AccountAPIError.invalidRequest
        }
        let path = "v1/account/timeline-subscriptions/\(timelineKey)"
        if !subscribed {
            _ = try await send(
                path: path,
                method: "DELETE",
                sessionToken: sessionToken
            )
            return false
        }
        let data = try await send(
            path: path,
            method: "PUT",
            sessionToken: sessionToken
        )
        let response: TimelineSubscriptionMutation = try decode(data)
        guard response.stableKey == timelineKey, response.subscribed else {
            throw AccountAPIError.invalidResponse
        }
        return true
    }

    private func send(
        path: String,
        method: String,
        sessionToken: String? = nil,
        body: Data? = nil
    ) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let sessionToken {
            request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await transport.data(for: request)
        } catch {
            throw AccountAPIError.networkUnavailable
        }
        guard let http = response as? HTTPURLResponse else {
            throw AccountAPIError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw Self.apiError(statusCode: http.statusCode, data: data)
        }
        if !data.isEmpty {
            let contentType = http.value(forHTTPHeaderField: "Content-Type") ?? ""
            guard contentType.lowercased().hasPrefix("application/json") else {
                throw AccountAPIError.invalidResponse
            }
        }
        return data
    }

    private func decode<Value: Decodable>(_ data: Data) throws -> Value {
        do {
            return try Self.makeDecoder().decode(Value.self, from: data)
        } catch {
            throw AccountAPIError.invalidResponse
        }
    }

    private func decodeReadingState(_ data: Data) throws -> AccountReadingState {
        do {
            return try Self.makeReadingDecoder().decode(AccountReadingState.self, from: data)
        } catch {
            throw AccountAPIError.invalidResponse
        }
    }

    private func decodeAccountWire<Value: Decodable>(_ data: Data) throws -> Value {
        do {
            return try Self.makeReadingDecoder().decode(Value.self, from: data)
        } catch {
            throw AccountAPIError.invalidResponse
        }
    }

    private static func apiError(statusCode: Int, data: Data) -> AccountAPIError {
        let envelope = try? makeDecoder().decode(APIErrorEnvelope.self, from: data)
        let code = envelope?.error.code ?? "account_request_failed"
        switch statusCode {
        case 401: return .unauthorized
        case 409 where code == "privacy_consent_required":
            return .privacyConsentRequired
        case 422: return .invalidRequest
        case 500...599: return .unavailable(code)
        default: return .server(code)
        }
    }

    private static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }

    private static func isValidTimelineKey(_ value: String) -> Bool {
        let allowed = CharacterSet.alphanumerics.union(
            CharacterSet(charactersIn: "-._~:")
        )
        return (1...128).contains(value.count)
            && value.unicodeScalars.allSatisfy(allowed.contains)
    }

    private static func makeReadingEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func makeActivityEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let value = try decoder.singleValueContainer().decode(String.self)
            let fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = fractional.date(from: value) { return date }
            let standard = ISO8601DateFormatter()
            standard.formatOptions = [.withInternetDateTime]
            guard let date = standard.date(from: value) else {
                throw DecodingError.dataCorruptedError(
                    in: try decoder.singleValueContainer(),
                    debugDescription: "invalid server date"
                )
            }
            return date
        }
        return decoder
    }

    private static func makeReadingDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = serverDateDecodingStrategy
        return decoder
    }

    private static var serverDateDecodingStrategy: JSONDecoder.DateDecodingStrategy {
        .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            let fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = fractional.date(from: value) { return date }
            let standard = ISO8601DateFormatter()
            standard.formatOptions = [.withInternetDateTime]
            guard let date = standard.date(from: value) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "invalid server date"
                )
            }
            return date
        }
    }

    private struct AppleSignInRequest: Encodable {
        let privacyConsentID: UUID
        let identityToken: String
        let authorizationCode: String
        let nonce: String
        let givenName: String?
        let familyName: String?
    }

    private struct AccountSessionResponse: Decodable {
        let sessionToken: String
        let expiresAt: Date
        let account: AccountProfile
    }

    private struct ArticleFeedbackRequest: Encodable {
        let reason: ArticleFeedbackReason
    }

    private struct TimelineSubscriptionsResponse: Decodable {
        let stableKeys: [String]
        let subscriptions: [TimelineSubscriptionDetail]

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            stableKeys = try container.decode([String].self, forKey: .stableKeys)
            subscriptions = try container.decodeIfPresent(
                [TimelineSubscriptionDetail].self,
                forKey: .subscriptions
            ) ?? []
        }

        private enum CodingKeys: String, CodingKey {
            case stableKeys
            case subscriptions
        }
    }

    private struct TimelineSubscriptionHistoryResponse: Decodable {
        let events: [TimelineSubscriptionHistoryEvent]
    }

    private struct ArticleFeedbackResponse: Decodable {
        let articleId: String
        let reason: ArticleFeedbackReason
        let createdAt: Date
        let updatedAt: Date
    }

    private struct AccountActivityBatchRequest: Encodable {
        let events: [AccountActivityEvent]
    }

    private struct APIErrorEnvelope: Decodable {
        let error: APIErrorDetail
    }

    private struct APIErrorDetail: Decodable {
        let code: String
    }
}

protocol AccountCredentialStoring: AnyObject {
    func load() throws -> AccountSession?
    func save(_ session: AccountSession) throws
    func clear() throws
}

final class KeychainAccountCredentialStore: AccountCredentialStoring {
    private let service: String
    private let account: String

    init(
        service: String = "com.example.gyeol.account",
        account: String = "current-session"
    ) {
        self.service = service
        self.account = account
    }

    func load() throws -> AccountSession? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw AccountAuthenticationError.secureStorage
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        do {
            return try decoder.decode(AccountSession.self, from: data)
        } catch {
            try? clear()
            throw AccountAuthenticationError.secureStorage
        }
    }

    func save(_ session: AccountSession) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(session)
        guard data.count <= 16 * 1024 else {
            throw AccountAuthenticationError.secureStorage
        }
        var attributes = baseQuery
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem {
            let update = [kSecValueData as String: data] as CFDictionary
            guard SecItemUpdate(baseQuery as CFDictionary, update) == errSecSuccess else {
                throw AccountAuthenticationError.secureStorage
            }
        } else if status != errSecSuccess {
            throw AccountAuthenticationError.secureStorage
        }
    }

    func clear() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw AccountAuthenticationError.secureStorage
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

final class MemoryAccountCredentialStore: AccountCredentialStoring {
    var session: AccountSession?
    var failLoad = false
    var failSave = false
    var failClear = false

    init(session: AccountSession? = nil) {
        self.session = session
    }

    func load() throws -> AccountSession? {
        if failLoad { throw AccountAuthenticationError.secureStorage }
        return session
    }

    func save(_ session: AccountSession) throws {
        if failSave { throw AccountAuthenticationError.secureStorage }
        self.session = session
    }

    func clear() throws {
        if failClear { throw AccountAuthenticationError.secureStorage }
        session = nil
    }
}

enum AppleAccountCredentialState: Equatable, Sendable {
    case authorized
    case revoked
    case notFound
    case transferred
}

protocol AppleCredentialStateChecking: AnyObject {
    func state(for appleUserID: String) async throws -> AppleAccountCredentialState
}

final class SystemAppleCredentialStateChecker: AppleCredentialStateChecking {
    func state(for appleUserID: String) async throws -> AppleAccountCredentialState {
        try await withCheckedThrowingContinuation { continuation in
            ASAuthorizationAppleIDProvider().getCredentialState(forUserID: appleUserID) {
                state, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }
                switch state {
                case .authorized: continuation.resume(returning: .authorized)
                case .revoked: continuation.resume(returning: .revoked)
                case .notFound: continuation.resume(returning: .notFound)
                case .transferred: continuation.resume(returning: .transferred)
                @unknown default: continuation.resume(returning: .notFound)
                }
            }
        }
    }
}

actor LiveAccountService: AccountServicing {
    private let api: any AccountAPIRequesting
    private let credentials: any AccountCredentialStoring
    private let credentialState: any AppleCredentialStateChecking
    private let now: @Sendable () -> Date

    init(
        api: any AccountAPIRequesting,
        credentials: any AccountCredentialStoring,
        credentialState: any AppleCredentialStateChecking,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.api = api
        self.credentials = credentials
        self.credentialState = credentialState
        self.now = now
    }

    func restore() async -> AccountSession? {
        let cached: AccountSession
        do {
            guard let loaded = try credentials.load() else { return nil }
            cached = loaded
        } catch {
            try? credentials.clear()
            return nil
        }
        guard cached.expiresAt > now() else {
            try? credentials.clear()
            return nil
        }

        let appleState: AppleAccountCredentialState
        do {
            appleState = try await credentialState.state(for: cached.appleUserID)
        } catch {
            return cached
        }
        guard appleState == .authorized else {
            try? credentials.clear()
            return nil
        }

        do {
            let profile = try await api.profile(sessionToken: cached.sessionToken)
            let refreshed = AccountSession(
                sessionToken: cached.sessionToken,
                expiresAt: cached.expiresAt,
                appleUserID: cached.appleUserID,
                account: profile
            )
            do {
                try credentials.save(refreshed)
                return refreshed
            } catch {
                return cached
            }
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            return nil
        } catch {
            return cached
        }
    }

    func signIn(
        _ payload: AppleAuthorizationPayload,
        privacyConsentID: UUID
    ) async throws -> AccountSession {
        let session = try await api.signIn(
            payload,
            privacyConsentID: privacyConsentID
        )
        do {
            try credentials.save(session)
        } catch {
            try? await api.logout(sessionToken: session.sessionToken)
            try? credentials.clear()
            throw AccountAuthenticationError.secureStorage
        }
        return session
    }

    func signOut() async {
        if let session = try? credentials.load() {
            try? await api.logout(sessionToken: session.sessionToken)
        }
        try? credentials.clear()
    }

    func deleteAccount() async throws -> AccountDeletionResult {
        guard let session = try credentials.load() else {
            throw AccountAuthenticationError.noSession
        }
        let result = try await api.deleteAccount(sessionToken: session.sessionToken)
        try credentials.clear()
        return result
    }

    func submitArticleFeedback(
        articleID: String,
        reason: ArticleFeedbackReason
    ) async throws -> ArticleFeedbackRecord {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.submitArticleFeedback(
                articleID: articleID,
                reason: reason,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func submitActivityEvents(
        _ events: [AccountActivityEvent]
    ) async throws -> AccountActivityBatchResponse {
        let eventIDs = events.map(\.eventID)
        guard (1...100).contains(events.count),
              Set(eventIDs).count == eventIDs.count,
              events.allSatisfy(\.isValid)
        else {
            throw AccountAPIError.invalidRequest
        }
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.submitActivityEvents(
                events,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func fetchRecommendations(
        _ request: AccountRecommendationRequest
    ) async throws -> AccountRecommendationPage {
        guard request.isValid else { throw AccountAPIError.invalidRequest }
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.fetchRecommendations(
                request,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func linkPrivacyConsent(_ consentID: UUID) async throws -> PrivacyConsentLink {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.linkPrivacyConsent(
                consentID,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func fetchReadingState() async throws -> AccountReadingState {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.fetchReadingState(sessionToken: session.sessionToken)
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func patchReadingState(_ patch: AccountReadingPatch) async throws -> AccountReadingState {
        guard !patch.isEmpty, patch.isValid else { throw AccountAPIError.invalidRequest }
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.patchReadingState(
                patch,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func fetchTimelineSubscriptions() async throws -> Set<String> {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.fetchTimelineSubscriptions(
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }

    func fetchTimelineSubscriptionDetails() async throws -> [TimelineSubscriptionDetail] {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        return try await api.fetchTimelineSubscriptionDetails(
            sessionToken: session.sessionToken
        )
    }

    func fetchTimelineSubscriptionHistory() async throws -> [TimelineSubscriptionHistoryEvent] {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        return try await api.fetchTimelineSubscriptionHistory(
            sessionToken: session.sessionToken
        )
    }

    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool
    ) async throws -> Bool {
        guard let session = try credentials.load(), session.expiresAt > now() else {
            try? credentials.clear()
            throw AccountAuthenticationError.noSession
        }
        do {
            return try await api.setTimelineSubscription(
                timelineKey: timelineKey,
                subscribed: subscribed,
                sessionToken: session.sessionToken
            )
        } catch let error as AccountAPIError where error == .unauthorized {
            try? credentials.clear()
            throw error
        }
    }
}

actor MemoryAccountService: AccountServicing {
    private var session: AccountSession?
    private let profile: AccountProfile
    private var articleCatalog: [String: ArticleFeedItem]
    private(set) var readingState: AccountReadingState
    private(set) var readingPatches: [AccountReadingPatch] = []
    private(set) var activityBatches: [[AccountActivityEvent]] = []
    private(set) var recommendationRequests: [AccountRecommendationRequest] = []
    private(set) var linkedPrivacyConsentIDs: [UUID] = []
    private var readingError: Error?
    private var activityError: Error?
    private var recommendationResults: [Result<AccountRecommendationPage, Error>]
    private var privacyLinkError: Error?
    private(set) var timelineSubscriptionKeys: Set<String>
    private var timelineSubscriptionError: Error?

    init(
        session: AccountSession? = nil,
        readingState: AccountReadingState = AccountReadingState(),
        readingArticles: [ArticleFeedItem] = [],
        readingError: Error? = nil,
        activityError: Error? = nil,
        recommendationResults: [Result<AccountRecommendationPage, Error>] = [],
        privacyLinkError: Error? = nil,
        timelineSubscriptionKeys: Set<String> = [],
        timelineSubscriptionError: Error? = nil
    ) {
        self.session = session
        self.readingState = readingState
        self.readingError = readingError
        self.activityError = activityError
        self.recommendationResults = recommendationResults
        self.privacyLinkError = privacyLinkError
        self.timelineSubscriptionKeys = timelineSubscriptionKeys
        self.timelineSubscriptionError = timelineSubscriptionError
        articleCatalog = Dictionary(
            (readingArticles + readingState.savedArticles.map(\.article))
                .map { ($0.articleID, $0) },
            uniquingKeysWith: { _, latest in latest }
        )
        profile = session?.account ?? AccountProfile(
            id: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!,
            displayName: "읽는 사람",
            email: "contact@example.com"
        )
    }

    func restore() async -> AccountSession? { session }

    func signIn(
        _ payload: AppleAuthorizationPayload,
        privacyConsentID: UUID
    ) async throws -> AccountSession {
        let signedIn = AccountSession(
            sessionToken: "memory-account-session",
            expiresAt: Date().addingTimeInterval(30 * 24 * 60 * 60),
            appleUserID: payload.appleUserID,
            account: profile
        )
        session = signedIn
        return signedIn
    }

    func signOut() async {
        session = nil
    }

    func deleteAccount() async throws -> AccountDeletionResult {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        session = nil
        readingState = AccountReadingState()
        return AccountDeletionResult(deleted: true, appleTokenRevoked: true)
    }

    func submitArticleFeedback(
        articleID: String,
        reason: ArticleFeedbackReason
    ) async throws -> ArticleFeedbackRecord {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        let now = Date()
        return ArticleFeedbackRecord(
            articleID: articleID,
            reason: reason,
            createdAt: now,
            updatedAt: now
        )
    }

    func submitActivityEvents(
        _ events: [AccountActivityEvent]
    ) async throws -> AccountActivityBatchResponse {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        let eventIDs = events.map(\.eventID)
        guard (1...100).contains(events.count),
              Set(eventIDs).count == eventIDs.count,
              events.allSatisfy(\.isValid)
        else {
            throw AccountAPIError.invalidRequest
        }
        if let activityError { throw activityError }
        activityBatches.append(events)
        return AccountActivityBatchResponse(
            accepted: events.count,
            duplicates: 0
        )
    }

    func fetchRecommendations(
        _ request: AccountRecommendationRequest
    ) async throws -> AccountRecommendationPage {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        guard request.isValid else { throw AccountAPIError.invalidRequest }
        recommendationRequests.append(request)
        if !recommendationResults.isEmpty {
            return try recommendationResults.removeFirst().get()
        }
        return AccountRecommendationPage(
            surface: request.surface,
            strategy: .blendedColdStart,
            generatedAt: Date(),
            items: [],
            nextCursor: nil
        )
    }

    func linkPrivacyConsent(_ consentID: UUID) async throws -> PrivacyConsentLink {
        guard let session else { throw AccountAuthenticationError.noSession }
        linkedPrivacyConsentIDs.append(consentID)
        if let privacyLinkError { throw privacyLinkError }
        let now = Date()
        return PrivacyConsentLink(
            consentID: consentID,
            accountID: session.account.id,
            policyVersion: PrivacyPolicy.currentVersion,
            policyURL: PrivacyPolicy.url,
            acceptedAt: now,
            linkedAt: now
        )
    }

    func fetchReadingState() async throws -> AccountReadingState {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        if let readingError { throw readingError }
        return readingState
    }

    func patchReadingState(_ patch: AccountReadingPatch) async throws -> AccountReadingState {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        guard !patch.isEmpty, patch.isValid else { throw AccountAPIError.invalidRequest }
        if let readingError { throw readingError }
        readingPatches.append(patch)

        let original = readingState
        for change in patch.savedChanges {
            if change.saved, let savedAt = change.savedAt {
                if let index = readingState.savedArticles.firstIndex(where: {
                    $0.article.articleID == change.articleID
                }) {
                    readingState.savedArticles[index] = AccountSavedArticle(
                        savedAt: savedAt,
                        article: readingState.savedArticles[index].article
                    )
                } else if let article = articleCatalog[change.articleID] {
                    readingState.savedArticles.append(
                        AccountSavedArticle(savedAt: savedAt, article: article)
                    )
                }
            } else {
                readingState.savedArticles.removeAll {
                    $0.article.articleID == change.articleID
                }
            }
        }
        readingState.savedArticles.sort { $0.savedAt > $1.savedAt }
        if readingState.savedArticles.count > 100 {
            readingState.savedArticles.removeLast(readingState.savedArticles.count - 100)
        }
        for change in patch.progressChanges {
            readingState.sceneProgress[change.articleID] = change.sceneIndex
        }
        if let settings = patch.settings {
            readingState.settings = settings.appSettings
        }
        if patch.updateLastArticle {
            readingState.lastArticleID = patch.lastArticleID
        }
        if readingState.savedArticles != original.savedArticles
            || readingState.sceneProgress != original.sceneProgress
            || readingState.lastArticleID != original.lastArticleID
            || readingState.settings != original.settings {
            readingState.revision += 1
        }
        return readingState
    }

    func fetchTimelineSubscriptions() async throws -> Set<String> {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        if let timelineSubscriptionError { throw timelineSubscriptionError }
        return timelineSubscriptionKeys
    }

    func fetchTimelineSubscriptionDetails() async throws -> [TimelineSubscriptionDetail] {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        if let timelineSubscriptionError { throw timelineSubscriptionError }
        return timelineSubscriptionKeys.sorted().map {
            TimelineSubscriptionDetail(
                stableKey: $0,
                title: nil,
                summary: nil,
                startedAt: nil,
                endedAt: nil,
                subscribedAt: Date(timeIntervalSince1970: 0)
            )
        }
    }

    func fetchTimelineSubscriptionHistory() async throws -> [TimelineSubscriptionHistoryEvent] {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        if let timelineSubscriptionError { throw timelineSubscriptionError }
        return []
    }

    func setTimelineSubscription(
        timelineKey: String,
        subscribed: Bool
    ) async throws -> Bool {
        guard session != nil else { throw AccountAuthenticationError.noSession }
        guard (1...128).contains(timelineKey.count) else {
            throw AccountAPIError.invalidRequest
        }
        if let timelineSubscriptionError { throw timelineSubscriptionError }
        if subscribed {
            guard timelineSubscriptionKeys.contains(timelineKey)
                || timelineSubscriptionKeys.count < 64 else {
                throw AccountAPIError.server("timeline_subscription_limit")
            }
            timelineSubscriptionKeys.insert(timelineKey)
        } else {
            timelineSubscriptionKeys.remove(timelineKey)
        }
        return subscribed
    }
}
