import ArticleFeedKit
import Foundation

struct PersistedReadingState: Codable, Equatable, Sendable {
    var savedFeed: [ArticleFeedItem]
    var savedArticleIDs: Set<String>
    var savedAt: [String: Date]
    var sceneProgress: [String: Int]
    var lastArticleID: String?
    var settings: ReaderSettings

    static var empty: PersistedReadingState {
        PersistedReadingState(
            savedFeed: [],
            savedArticleIDs: [],
            savedAt: [:],
            sceneProgress: [:],
            lastArticleID: nil,
            settings: ReaderSettings()
        )
    }

    mutating func normalize() {
        var seen: Set<String> = []
        savedFeed = savedFeed
            .filter { savedArticleIDs.contains($0.articleID) && seen.insert($0.articleID).inserted }
            .sorted {
                (savedAt[$0.articleID] ?? .distantPast)
                    > (savedAt[$1.articleID] ?? .distantPast)
            }
        if savedFeed.count > PersistedAppSnapshot.maximumSavedArticles {
            savedFeed.removeLast(savedFeed.count - PersistedAppSnapshot.maximumSavedArticles)
        }
        let displayableSavedIDs = Set(savedFeed.map(\.articleID))
        savedArticleIDs.formIntersection(displayableSavedIDs)
        savedAt = savedAt.filter { savedArticleIDs.contains($0.key) }
        sceneProgress = Dictionary(
            sceneProgress
                .filter { Self.isValidArticleID($0.key) && (0...999).contains($0.value) }
                .sorted { $0.key < $1.key }
                .prefix(500)
                .map { ($0.key, $0.value) },
            uniquingKeysWith: { current, _ in current }
        )
        if let lastArticleID, !Self.isValidArticleID(lastArticleID) {
            self.lastArticleID = nil
        }
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains(where: { $0 == "\n" || $0 == "\r" || $0 == "\0" })
    }
}

struct PendingSavedChange: Codable, Equatable, Sendable {
    let isSaved: Bool
    let savedAt: Date?
}

struct PersistedReadingDelta: Codable, Equatable, Sendable {
    var savedChanges: [String: PendingSavedChange] = [:]
    var progressChanges: [String: Int] = [:]
    var settings: ReaderSettings?
    var updateLastArticle = false
    var lastArticleID: String?

    var isEmpty: Bool {
        savedChanges.isEmpty
            && progressChanges.isEmpty
            && settings == nil
            && !updateLastArticle
    }

    mutating func merge(_ newer: PersistedReadingDelta) {
        savedChanges.merge(newer.savedChanges) { _, latest in latest }
        progressChanges.merge(newer.progressChanges) { _, latest in latest }
        if let settings = newer.settings { self.settings = settings }
        if newer.updateLastArticle {
            updateLastArticle = true
            lastArticleID = newer.lastArticleID
        }
        normalize()
    }

    mutating func acknowledge(_ sent: PersistedReadingDelta) {
        for (articleID, change) in sent.savedChanges
        where savedChanges[articleID] == change {
            savedChanges.removeValue(forKey: articleID)
        }
        for (articleID, sceneIndex) in sent.progressChanges
        where progressChanges[articleID] == sceneIndex {
            progressChanges.removeValue(forKey: articleID)
        }
        if settings == sent.settings { settings = nil }
        if sent.updateLastArticle,
           updateLastArticle,
           lastArticleID == sent.lastArticleID {
            updateLastArticle = false
            lastArticleID = nil
        }
    }

    mutating func normalize() {
        savedChanges = Dictionary(
            savedChanges
                .filter {
                    Self.isValidArticleID($0.key)
                        && ($0.value.isSaved == ($0.value.savedAt != nil))
                }
                .sorted { $0.key < $1.key }
                .prefix(100)
                .map { ($0.key, $0.value) },
            uniquingKeysWith: { current, _ in current }
        )
        progressChanges = Dictionary(
            progressChanges
                .filter { Self.isValidArticleID($0.key) && (0...999).contains($0.value) }
                .sorted { $0.key < $1.key }
                .prefix(500)
                .map { ($0.key, $0.value) },
            uniquingKeysWith: { current, _ in current }
        )
        if let lastArticleID, !Self.isValidArticleID(lastArticleID) {
            self.lastArticleID = nil
        }
        if !updateLastArticle { lastArticleID = nil }
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains(where: { $0 == "\n" || $0 == "\r" || $0 == "\0" })
    }
}

struct PersistedRecommendationCache: Codable, Equatable, Sendable {
    var items: [ArticleFeedItem]
    var generatedAt: Date
    var rankingVersion: String
    var nextCursor: String?

    mutating func normalize() -> Bool {
        guard rankingVersion == "content-v1" else { return false }
        var seenArticleIDs = Set<String>()
        items = items.filter {
            Self.isSHA256($0.articleID)
                && seenArticleIDs.insert($0.articleID).inserted
        }
        if items.count > 15 {
            items.removeLast(items.count - 15)
        }
        if let nextCursor {
            let normalized = nextCursor.trimmingCharacters(in: .whitespacesAndNewlines)
            self.nextCursor = normalized.isEmpty || normalized.count > 2_048
                ? nil
                : normalized
        }
        return true
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.count == 64
            && value.unicodeScalars.allSatisfy {
                (48...57).contains($0.value) || (97...102).contains($0.value)
            }
    }
}

struct PersistedAccountReadingState: Codable, Equatable, Sendable {
    var state: PersistedReadingState
    var pending: PersistedReadingDelta
    var pendingActivityEvents: [AccountActivityEvent]
    var homeRecommendationCache: PersistedRecommendationCache?
    var serverRevision: Int
    var guestMergeNeedsServerCheck = false

    private enum CodingKeys: String, CodingKey {
        case state
        case pending
        case pendingActivityEvents
        case homeRecommendationCache
        case serverRevision
        case guestMergeNeedsServerCheck
    }

    init(
        state: PersistedReadingState,
        pending: PersistedReadingDelta,
        pendingActivityEvents: [AccountActivityEvent] = [],
        homeRecommendationCache: PersistedRecommendationCache? = nil,
        serverRevision: Int,
        guestMergeNeedsServerCheck: Bool = false
    ) {
        self.state = state
        self.pending = pending
        self.pendingActivityEvents = pendingActivityEvents
        self.homeRecommendationCache = homeRecommendationCache
        self.serverRevision = serverRevision
        self.guestMergeNeedsServerCheck = guestMergeNeedsServerCheck
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        state = try values.decode(PersistedReadingState.self, forKey: .state)
        pending = try values.decode(PersistedReadingDelta.self, forKey: .pending)
        pendingActivityEvents = try values.decodeIfPresent(
            [AccountActivityEvent].self,
            forKey: .pendingActivityEvents
        ) ?? []
        homeRecommendationCache = try values.decodeIfPresent(
            PersistedRecommendationCache.self,
            forKey: .homeRecommendationCache
        )
        serverRevision = try values.decodeIfPresent(Int.self, forKey: .serverRevision) ?? 0
        guestMergeNeedsServerCheck = try values.decodeIfPresent(
            Bool.self,
            forKey: .guestMergeNeedsServerCheck
        ) ?? false
    }

    static var empty: PersistedAccountReadingState {
        PersistedAccountReadingState(
            state: .empty,
            pending: PersistedReadingDelta(),
            pendingActivityEvents: [],
            homeRecommendationCache: nil,
            serverRevision: 0,
            guestMergeNeedsServerCheck: false
        )
    }

    mutating func normalize() {
        state.normalize()
        pending.normalize()
        var seenActivityIDs = Set<UUID>()
        pendingActivityEvents = pendingActivityEvents.filter {
            $0.isValid && seenActivityIDs.insert($0.eventID).inserted
        }
        if var cache = homeRecommendationCache {
            homeRecommendationCache = cache.normalize() ? cache : nil
        }
        serverRevision = max(serverRevision, 0)
    }
}

struct PersistedAppSnapshot: Codable, Equatable, Sendable {
    static let currentVersion = 10
    static let maximumCachedArticles = 15
    static let maximumSavedArticles = 100

    var version: Int
    var hasCompletedOnboarding: Bool
    var hasSeenReaderGuide: Bool
    var cachedFeed: [ArticleFeedItem]
    var savedFeed: [ArticleFeedItem]
    var savedArticleIDs: Set<String>
    var savedAt: [String: Date]
    var sceneProgress: [String: Int]
    var lastArticleID: String?
    var settings: ReaderSettings
    var lastCheckedAt: Date?
    var lastPresentedAppVersion: String?
    var cachedDiscovery: DiscoveryResponse?
    var acceptedPrivacyPolicyVersion: String?
    var privacyConsentReceiptID: UUID?
    var guestReadingState: PersistedReadingState
    var accountReadingStates: [String: PersistedAccountReadingState]
    var guestMergedAccountID: UUID?
    var pendingArticleViews: [ArticleViewEvent]

    init(
        version: Int = currentVersion,
        hasCompletedOnboarding: Bool,
        hasSeenReaderGuide: Bool = false,
        cachedFeed: [ArticleFeedItem],
        savedFeed: [ArticleFeedItem],
        savedArticleIDs: Set<String>,
        savedAt: [String: Date],
        sceneProgress: [String: Int],
        lastArticleID: String?,
        settings: ReaderSettings,
        lastCheckedAt: Date?,
        lastPresentedAppVersion: String? = nil,
        cachedDiscovery: DiscoveryResponse? = nil,
        acceptedPrivacyPolicyVersion: String? = nil,
        privacyConsentReceiptID: UUID? = nil,
        guestReadingState: PersistedReadingState? = nil,
        accountReadingStates: [String: PersistedAccountReadingState] = [:],
        guestMergedAccountID: UUID? = nil,
        pendingArticleViews: [ArticleViewEvent] = []
    ) {
        self.version = version
        self.hasCompletedOnboarding = hasCompletedOnboarding
        self.hasSeenReaderGuide = hasSeenReaderGuide
        self.cachedFeed = cachedFeed
        self.savedFeed = savedFeed
        self.savedArticleIDs = savedArticleIDs
        self.savedAt = savedAt
        self.sceneProgress = sceneProgress
        self.lastArticleID = lastArticleID
        self.settings = settings
        self.lastCheckedAt = lastCheckedAt
        self.lastPresentedAppVersion = lastPresentedAppVersion
        self.cachedDiscovery = cachedDiscovery
        self.acceptedPrivacyPolicyVersion = acceptedPrivacyPolicyVersion
        self.privacyConsentReceiptID = privacyConsentReceiptID
        self.guestReadingState = guestReadingState ?? PersistedReadingState(
            savedFeed: savedFeed,
            savedArticleIDs: savedArticleIDs,
            savedAt: savedAt,
            sceneProgress: sceneProgress,
            lastArticleID: lastArticleID,
            settings: settings
        )
        self.accountReadingStates = accountReadingStates
        self.guestMergedAccountID = guestMergedAccountID
        self.pendingArticleViews = pendingArticleViews
    }

    static var empty: PersistedAppSnapshot {
        PersistedAppSnapshot(
            hasCompletedOnboarding: false,
            hasSeenReaderGuide: false,
            cachedFeed: [],
            savedFeed: [],
            savedArticleIDs: [],
            savedAt: [:],
            sceneProgress: [:],
            lastArticleID: nil,
            settings: ReaderSettings(),
            lastCheckedAt: nil,
            lastPresentedAppVersion: nil,
            cachedDiscovery: nil,
            acceptedPrivacyPolicyVersion: nil,
            privacyConsentReceiptID: nil
        )
    }

    mutating func normalize() {
        let sourceVersion = version
        if lastPresentedAppVersion?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true {
            lastPresentedAppVersion = nil
        }
        if acceptedPrivacyPolicyVersion?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true {
            acceptedPrivacyPolicyVersion = nil
            privacyConsentReceiptID = nil
        }
        if acceptedPrivacyPolicyVersion == nil {
            privacyConsentReceiptID = nil
        }
        cachedFeed = Self.unique(cachedFeed, limit: Self.maximumCachedArticles)
        savedFeed = Self.unique(savedFeed, limit: Self.maximumSavedArticles)

        if sourceVersion == 1, savedFeed.isEmpty {
            savedFeed = cachedFeed.filter { savedArticleIDs.contains($0.articleID) }
        }

        savedFeed = savedFeed.filter { savedArticleIDs.contains($0.articleID) }
        cachedDiscovery = cachedDiscovery.flatMap(Self.normalizedDiscovery)
        let discoveryIDs = Set(
            cachedDiscovery?.routes.flatMap { route in
                route.items.map(\.article.articleID)
            } ?? []
        )
        let availableIDs = Set(cachedFeed.map(\.articleID))
            .union(savedFeed.map(\.articleID))
            .union(discoveryIDs)
        savedArticleIDs.formIntersection(availableIDs)
        savedAt = savedAt.filter { savedArticleIDs.contains($0.key) }
        sceneProgress = sceneProgress.filter { availableIDs.contains($0.key) && $0.value >= 0 }
        if let lastArticleID, !availableIDs.contains(lastArticleID) {
            self.lastArticleID = nil
        }
        let rootReadingState = PersistedReadingState(
            savedFeed: savedFeed,
            savedArticleIDs: savedArticleIDs,
            savedAt: savedAt,
            sceneProgress: sceneProgress,
            lastArticleID: lastArticleID,
            settings: settings
        )
        if sourceVersion < 7 {
            guestReadingState = rootReadingState
            accountReadingStates = [:]
            guestMergedAccountID = nil
        } else if guestReadingState == .empty,
                  accountReadingStates.isEmpty,
                  guestMergedAccountID == nil,
                  rootReadingState != .empty {
            guestReadingState = rootReadingState
        }
        guestReadingState.normalize()
        accountReadingStates = Dictionary(
            accountReadingStates.compactMap { key, value in
                let normalizedKey = key.lowercased()
                guard UUID(uuidString: normalizedKey) != nil else { return nil }
                var value = value
                value.normalize()
                return (normalizedKey, value)
            },
            uniquingKeysWith: { current, _ in current }
        )
        var seenArticleViewIDs = Set<UUID>()
        pendingArticleViews = pendingArticleViews.filter {
            $0.isValid && seenArticleViewIDs.insert($0.eventID).inserted
        }
        version = Self.currentVersion
    }

    private static func unique(_ items: [ArticleFeedItem], limit: Int) -> [ArticleFeedItem] {
        var seen: Set<String> = []
        return items.filter { seen.insert($0.articleID).inserted }.prefix(limit).map { $0 }
    }

    private static func normalizedDiscovery(_ response: DiscoveryResponse) -> DiscoveryResponse? {
        let routes = Array(response.routes.prefix(8)).map { route in
            DiscoveryRoute(
                id: route.id,
                rank: route.rank,
                slug: route.slug,
                title: route.title,
                summary: route.summary,
                topic: route.topic,
                isRepresentative: route.isRepresentative,
                items: Array(route.items.prefix(4))
            )
        }
        guard !routes.isEmpty,
              routes.allSatisfy({ $0.items.count == 4 }),
              Set(routes.map(\.id)).count == routes.count,
              let representativeID = response.representativeRouteID,
              routes.contains(where: { $0.id == representativeID }),
              routes.filter(\.isRepresentative).count == 1,
              routes.first(where: \.isRepresentative)?.id == representativeID
        else { return nil }

        let articleIDs = routes.flatMap { route in route.items.map(\.article.articleID) }
        guard Set(articleIDs).count == articleIDs.count else { return nil }

        return DiscoveryResponse(
            formatVersion: response.formatVersion,
            generatedAt: response.generatedAt,
            expiresAt: response.expiresAt,
            representativeRouteID: representativeID,
            routes: routes,
            searchSuggestions: Array(response.searchSuggestions.prefix(8)),
            trendingSearchSuggestions: Array(response.trendingSearchSuggestions.prefix(3)),
            query: nil,
            results: [],
            cooldownSeconds: response.cooldownSeconds,
            timelineStatus: response.timelineStatus,
            timelinesGeneratedAt: response.timelinesGeneratedAt,
            timelinesRefreshedAt: response.timelinesRefreshedAt,
            featuredTimelines: Array(response.featuredTimelines.prefix(8)),
            timelineResults: []
        )
    }

    private enum CodingKeys: String, CodingKey {
        case version
        case hasCompletedOnboarding
        case hasSeenReaderGuide
        case cachedFeed
        case savedFeed
        case savedArticleIDs
        case savedAt
        case sceneProgress
        case lastArticleID
        case settings
        case lastCheckedAt
        case lastPresentedAppVersion
        case cachedDiscovery
        case acceptedPrivacyPolicyVersion
        case privacyConsentReceiptID
        case guestReadingState
        case accountReadingStates
        case guestMergedAccountID
        case pendingArticleViews
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = try values.decodeIfPresent(Int.self, forKey: .version) ?? 1
        hasCompletedOnboarding = try values.decodeIfPresent(Bool.self, forKey: .hasCompletedOnboarding) ?? false
        hasSeenReaderGuide = try values.decodeIfPresent(Bool.self, forKey: .hasSeenReaderGuide) ?? false
        cachedFeed = try values.decodeIfPresent([ArticleFeedItem].self, forKey: .cachedFeed) ?? []
        savedFeed = try values.decodeIfPresent([ArticleFeedItem].self, forKey: .savedFeed) ?? []
        savedArticleIDs = try values.decodeIfPresent(Set<String>.self, forKey: .savedArticleIDs) ?? []
        savedAt = try values.decodeIfPresent([String: Date].self, forKey: .savedAt) ?? [:]
        sceneProgress = try values.decodeIfPresent([String: Int].self, forKey: .sceneProgress) ?? [:]
        lastArticleID = try values.decodeIfPresent(String.self, forKey: .lastArticleID)
        settings = try values.decodeIfPresent(ReaderSettings.self, forKey: .settings) ?? ReaderSettings()
        lastCheckedAt = try values.decodeIfPresent(Date.self, forKey: .lastCheckedAt)
        lastPresentedAppVersion = try values.decodeIfPresent(
            String.self,
            forKey: .lastPresentedAppVersion
        )
        cachedDiscovery = try values.decodeIfPresent(
            DiscoveryResponse.self,
            forKey: .cachedDiscovery
        )
        acceptedPrivacyPolicyVersion = try values.decodeIfPresent(
            String.self,
            forKey: .acceptedPrivacyPolicyVersion
        )
        privacyConsentReceiptID = try values.decodeIfPresent(
            UUID.self,
            forKey: .privacyConsentReceiptID
        )
        guestReadingState = try values.decodeIfPresent(
            PersistedReadingState.self,
            forKey: .guestReadingState
        ) ?? .empty
        accountReadingStates = try values.decodeIfPresent(
            [String: PersistedAccountReadingState].self,
            forKey: .accountReadingStates
        ) ?? [:]
        guestMergedAccountID = try values.decodeIfPresent(
            UUID.self,
            forKey: .guestMergedAccountID
        )
        pendingArticleViews = try values.decodeIfPresent(
            [ArticleViewEvent].self,
            forKey: .pendingArticleViews
        ) ?? []
    }
}

enum OnboardingPage: String, Codable, Equatable, Sendable {
    case introReader
    case introNavigation
    case whatsNew
    case privacyConsent
    case signIn
}

enum LaunchOnboardingPlan {
    static func make(
        isFirstInstall: Bool,
        lastPresentedVersion: String?,
        currentVersion: String,
        isSignedIn: Bool,
        hasCurrentPrivacyConsent: Bool
    ) -> [OnboardingPage] {
        var pages: [OnboardingPage]
        if isFirstInstall {
            pages = [.introReader, .introNavigation, .whatsNew]
        } else if lastPresentedVersion != currentVersion {
            pages = [.whatsNew]
        } else {
            pages = []
        }
        if !hasCurrentPrivacyConsent {
            pages.append(.privacyConsent)
            if !isSignedIn {
                pages.append(.signIn)
            }
        } else if !pages.isEmpty, !isSignedIn {
            pages.append(.signIn)
        }
        return pages
    }
}

protocol AppPersisting: Sendable {
    func load() async -> PersistedAppSnapshot
    func save(_ snapshot: PersistedAppSnapshot) async throws
    func storedByteCount() async -> Int
}

actor FileAppPersistence: AppPersisting {
    private let fileURL: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(fileURL: URL) {
        self.fileURL = fileURL
        encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
    }

    static func live(fileManager: FileManager = .default) -> FileAppPersistence {
        let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.temporaryDirectory
        // Keep the original filename so installed v1 snapshots can migrate in place.
        return FileAppPersistence(fileURL: base.appendingPathComponent("TextShortsApp/state-v1.json"))
    }

    func load() async -> PersistedAppSnapshot {
        guard let data = try? Data(contentsOf: fileURL),
              var snapshot = try? decoder.decode(PersistedAppSnapshot.self, from: data),
              (1...PersistedAppSnapshot.currentVersion).contains(snapshot.version) else {
            return .empty
        }
        snapshot.normalize()
        return snapshot
    }

    func save(_ source: PersistedAppSnapshot) async throws {
        var snapshot = source
        snapshot.normalize()
        let data = try encoder.encode(snapshot)
        let directory = fileURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try data.write(to: fileURL, options: .atomic)
    }

    func storedByteCount() async -> Int {
        let values = try? fileURL.resourceValues(forKeys: [.fileSizeKey])
        return values?.fileSize ?? 0
    }
}

actor MemoryAppPersistence: AppPersisting {
    private(set) var snapshot: PersistedAppSnapshot
    var failWrites = false

    init(snapshot: PersistedAppSnapshot = .empty) {
        var snapshot = snapshot
        snapshot.normalize()
        self.snapshot = snapshot
    }

    func load() async -> PersistedAppSnapshot { snapshot }

    func save(_ source: PersistedAppSnapshot) async throws {
        if failWrites { throw CocoaError(.fileWriteUnknown) }
        var snapshot = source
        snapshot.normalize()
        self.snapshot = snapshot
    }

    func storedByteCount() async -> Int {
        (try? JSONEncoder().encode(snapshot).count) ?? 0
    }
}
