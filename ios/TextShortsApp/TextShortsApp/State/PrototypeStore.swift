import ArticleFeedKit
import SwiftUI
import UIKit

enum RecommendationLoadState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case fallback
}

enum HomeRecommendationTrigger: Equatable, Sendable {
    case initial
    case pull
    case pagination
}

enum HomeCatalogPolicy {
    static let recentArticleLimit = 4
}

enum TimelineEntrance: Equatable, Sendable {
    case initial
    case selection
    case inserted(UUID)
    case occurred(UUID)
    case none
}

struct TimelineEntranceRequest: Equatable, Sendable, Identifiable {
    let id: UUID
    let timelineKey: String
    let nodeID: UUID
    let entrance: TimelineEntrance
}

@MainActor
protocol BookmarkHapticFeedbackProviding: AnyObject {
    func prepare()
    func playConfirmation()
}

@MainActor
final class SystemBookmarkHapticFeedback: BookmarkHapticFeedbackProviding {
    private var generator: UIImpactFeedbackGenerator?

    func prepare() {
        let generator = UIImpactFeedbackGenerator(style: .rigid)
        self.generator = generator
        generator.prepare()
    }

    func playConfirmation() {
        let generator = generator ?? UIImpactFeedbackGenerator(style: .rigid)
        generator.impactOccurred(intensity: 0.9)
        self.generator = nil
    }
}

@MainActor
final class TextShortsStore: ObservableObject {
    @Published var currentScreen: PrototypeScreen = .home
    @Published var previousScreen: PrototypeScreen = .reader
    @Published private(set) var readerOriginScreen: PrototypeScreen = .home
    @Published var articleIndex = 0
    @Published var sceneIndex = 0
    @Published private(set) var articles: [PrototypeArticle] = []
    @Published private(set) var savedArticles: [PrototypeArticle] = []
    @Published private(set) var savedArticleIDs: Set<String> = []
    @Published private(set) var savedAt: [String: Date] = [:]
    @Published private(set) var isOffline = false
    @Published private(set) var isLoadingFeed = false
    @Published private(set) var isLoadingNewArticle = false
    @Published private(set) var cacheStats = CacheStats()
    @Published private(set) var lastCheckedAt: Date?
    @Published private(set) var homeFeedStatus: HomeFeedStatus = .idle
    @Published private(set) var homePaginationState: HomePaginationState = .idle
    @Published private(set) var supportCode = "APP-ATTEST-VERIFY-02"
    @Published var showContext = false
    @Published var showOverflow = false
    @Published var showArticleFeedback = false
    @Published private(set) var showReaderGuide = false
    @Published var toastMessage: String?
    @Published var showAnnotations = false
    @Published var showPrototypeTools = false
    @Published var settings = ReaderSettings()
    @Published private(set) var accountStatus: AccountStatus = .signedOut
    @Published private(set) var accountFeedback: String?
    @Published private(set) var editorialPushState: EditorialPushState = .signedOut
    @Published private(set) var pushNotificationPreferences: PushNotificationPreferences = .none
    @Published private(set) var isSubmittingArticleFeedback = false
    @Published private(set) var onboardingPages: [OnboardingPage] = []
    @Published private(set) var onboardingIndex = 0
    @Published private(set) var isPrivacyConsentSelected = false
    @Published private(set) var privacyConsentState: PrivacyConsentState = .idle
    @Published private(set) var hasAcknowledgedNewsDisclaimer = false
    @Published var isAttestRetrying = false
    @Published var showTechnicalDetails = false
    @Published var sharePayload: SharePayload?
    @Published var pendingExternalURL: URL?
    @Published private(set) var discoveryResponse: DiscoveryResponse?
    @Published private(set) var discoveryState: DiscoveryLoadState = .idle
    @Published var discoveryQuery = ""
    @Published private(set) var selectedCurationRouteID: UUID?
    @Published private(set) var selectedFeaturedTimelineKey: String?
    /// Timeline opened from a search result, kept here rather than in the
    /// Explore view so reading one of its nodes and coming back lands on the
    /// timeline again instead of the search result list behind it.
    @Published private(set) var selectedTimelineSearchKey: String?
    @Published private(set) var subscribedTimelineKeys: Set<String> = []
    @Published private(set) var pendingTimelineSubscriptionKeys: Set<String> = []
    @Published private(set) var pendingTimelineOpenKey: String?
    @Published private(set) var timelineSubscriptionDetails: [TimelineSubscriptionDetail] = []
    @Published private(set) var timelineSubscriptionHistory: [TimelineSubscriptionHistoryEvent] = []
    @Published private(set) var isTimelineManagementLoading = false
    @Published private(set) var timelineManagementError: String?
    @Published private(set) var timelineEntranceRequest: TimelineEntranceRequest?
    @Published private(set) var activeReadingRouteID: UUID?
    /// Timeline the article being read was opened from, so the boundary can
    /// offer the next event of that timeline instead of loose recommendations.
    @Published private(set) var activeReadingTimelineKey: String?
    @Published private(set) var activeReadingCollection: DiscoveryCollection?
    @Published private(set) var isDiscoverySearchLoading = false
    @Published private(set) var boundaryRecommendations: [PrototypeArticle] = []
    @Published private(set) var recommendationState: RecommendationLoadState = .idle
    @Published private(set) var homeRecommendationCursor: String?
    @Published private(set) var homeLatestArticles: [PrototypeArticle] = []

    private let loader: any ArticleFeedLoading
    private let discoveryLoader: any DiscoveryLoading
    private let persistence: any AppPersisting
    private let connectivity: any ConnectivityMonitoring
    private let accountService: any AccountServicing
    private let editorialPushService: any EditorialPushServicing
    private let privacyConsentService: any PrivacyConsentServicing
    private let articleViewService: any ArticleViewRecording
    private let bookmarkHaptics: any BookmarkHapticFeedbackProviding
    let mediaCache: any ArticleMediaCaching
    private let projector: ArticleProjector
    private let displayPolicy = ArticleDisplayPolicy()
    private let clock: @Sendable () -> Date
    private let currentAppVersion: String
    private var snapshot = PersistedAppSnapshot.empty
    private var catalogFeedItems: [ArticleFeedItem] = []
    private var cachedFeedItems: [ArticleFeedItem] = []
    private var homeRecommendationItems: [ArticleFeedItem] = []
    private var homeLatestFeedItems: [ArticleFeedItem] = []
    private var boundaryRecommendationItems: [ArticleFeedItem] = []
    private var savedFeedItems: [ArticleFeedItem] = []
    private var selectedSavedArticle: PrototypeArticle?
    private var selectedDiscoveryArticle: PrototypeArticle?
    private var selectedDiscoveryFeedItem: ArticleFeedItem?
    private var articleFeedbackReasons: [String: ArticleFeedbackReason] = [:]
    private var hasBootstrapped = false
    private var sceneProgress: [String: Int] = [:]
    private var connectivityState: ConnectivityState = .unknown
    private var persistTask: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var paginationTask: Task<Void, Never>?
    private var mediaCleanupTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var discoveryTask: Task<Void, Never>?
    private var discoverySearchTask: Task<Void, Never>?
    private var discoveryPaginationTask: Task<Void, Never>?
    private var activeDiscoverySearchID: UUID?
    private var activeReadingAccountID: UUID?
    private var readingNamespaceToken = UUID()
    private var readingSyncTask: Task<Void, Never>?
    private var readingSyncNeedsRerun = false
    private var articleViewTask: Task<Void, Never>?
    private var articleViewSyncNeedsRerun = false
    private var recommendationGeneration: UUID?
    private var boundaryRecommendationGeneration: UUID?
    private var timelineManagementGeneration: UUID?
    private var boundaryRecommendationTask: Task<Void, Never>?
    private var pushRegistrationAttemptID: UUID?
    private var pushRegistrationToken: Data?
    private let hasForcedScreen: Bool

    convenience init(arguments: [String] = ProcessInfo.processInfo.arguments) {
        let environment = AppEnvironment.make(arguments: arguments)
        self.init(
            arguments: arguments,
            loader: environment.loader,
            discoveryLoader: environment.discoveryLoader,
            persistence: environment.persistence,
            mediaCache: environment.mediaCache,
            connectivity: environment.connectivity,
            accountService: environment.accountService,
            editorialPushService: environment.editorialPushService,
            privacyConsentService: environment.privacyConsentService,
            articleViewService: environment.articleViewService
        )
    }

    init(
        arguments: [String],
        loader: any ArticleFeedLoading,
        discoveryLoader: (any DiscoveryLoading)? = nil,
        persistence: any AppPersisting,
        mediaCache: any ArticleMediaCaching,
        connectivity: any ConnectivityMonitoring = NoopConnectivityMonitor(),
        accountService: any AccountServicing = MemoryAccountService(),
        editorialPushService: (any EditorialPushServicing)? = nil,
        privacyConsentService: (any PrivacyConsentServicing)? = nil,
        articleViewService: (any ArticleViewRecording)? = nil,
        bookmarkHaptics: (any BookmarkHapticFeedbackProviding)? = nil,
        projector: ArticleProjector = ArticleProjector(),
        appVersion: String? = nil,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.loader = loader
        self.discoveryLoader = discoveryLoader
            ?? UnavailableDiscoveryLoader(code: "discovery_unavailable")
        self.persistence = persistence
        self.mediaCache = mediaCache
        self.connectivity = connectivity
        self.accountService = accountService
        self.editorialPushService = editorialPushService ?? MemoryEditorialPushService()
        pushNotificationPreferences = self.editorialPushService.preferences()
        self.privacyConsentService = privacyConsentService ?? MemoryPrivacyConsentService()
        self.articleViewService = articleViewService ?? MemoryArticleViewService()
        self.bookmarkHaptics = bookmarkHaptics ?? SystemBookmarkHapticFeedback()
        self.projector = projector
        self.clock = clock
        let bundleVersion = Bundle.main.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String
        let resolvedVersion = (appVersion ?? bundleVersion ?? "1.0")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        currentAppVersion = resolvedVersion.isEmpty ? "1.0" : resolvedVersion
        hasForcedScreen = Self.value(after: "-prototype-screen", in: arguments) != nil

        if let screenValue = Self.value(after: "-prototype-screen", in: arguments),
           let screen = PrototypeScreen(rawValue: screenValue) {
            currentScreen = screen
        }
        if let annotationValue = Self.value(after: "-prototype-annotations", in: arguments) {
            showAnnotations = annotationValue.lowercased() == "true"
        }
        if let toolsValue = Self.value(after: "-prototype-tools", in: arguments) {
            showPrototypeTools = toolsValue.lowercased() == "true"
        }
        if hasForcedScreen {
            articles = PrototypeData.articles
            if currentScreen == .onboarding {
                switch Self.value(after: "-onboarding-fixture", in: arguments) {
                case "update": onboardingPages = [.whatsNew, .privacyConsent, .signIn]
                case "signed-in-update": onboardingPages = [.whatsNew, .privacyConsent]
                default: onboardingPages = [
                    .introReader, .introNavigation, .whatsNew, .privacyConsent, .signIn,
                ]
                }
            }
        }
    }

    var availableArticles: [PrototypeArticle] { articles }
    var homeArticles: [PrototypeArticle] {
        articles.filter { !isCompletedForHome($0) }
    }
    var clampedArticleIndex: Int { min(max(articleIndex, 0), max(articles.count - 1, 0)) }
    var article: PrototypeArticle {
        selectedDiscoveryArticle
            ?? selectedSavedArticle
            ?? articles[safe: clampedArticleIndex]
            ?? PrototypeData.articles[0]
    }
    var nextArticles: [PrototypeArticle] {
        guard !articles.isEmpty else { return [] }

        let currentArticleID = article.id
        let startIndex: Int
        if let currentIndex = articles.firstIndex(where: { $0.id == currentArticleID }) {
            startIndex = (currentIndex + 1) % articles.count
        } else {
            startIndex = clampedArticleIndex
        }

        var seenArticleIDs: Set<String> = []
        var recommendations: [PrototypeArticle] = []
        for offset in articles.indices {
            let candidate = articles[(startIndex + offset) % articles.count]
            guard candidate.id != currentArticleID,
                  seenArticleIDs.insert(candidate.id).inserted
            else { continue }
            recommendations.append(candidate)
            if recommendations.count == 3 { break }
        }
        return recommendations
    }
    var displayedNextArticles: [PrototypeArticle] {
        if let nextRouteArticle { return [nextRouteArticle] }
        if let nextTimelineArticle { return [nextTimelineArticle] }
        if !boundaryRecommendations.isEmpty { return boundaryRecommendations }
        return nextArticles
    }
    var nextArticle: PrototypeArticle {
        displayedNextArticles.first ?? article
    }
    var clampedSceneIndex: Int { min(max(sceneIndex, 0), max(article.scenes.count - 1, 0)) }
    var scene: PrototypeScene { article.scenes[clampedSceneIndex] }
    var isSaved: Bool { savedArticleIDs.contains(article.id) }
    var cachedArticleCount: Int { displayPolicy.filter(cachedFeedItems).count }
    var hasReadableContent: Bool {
        selectedDiscoveryArticle != nil || selectedSavedArticle != nil || !articles.isEmpty
    }
    var hasNextFeedArticle: Bool { !nextArticles.isEmpty }
    var canResumeOffline: Bool { cachedArticleCount > 0 }
    var offlineTitle: String {
        canResumeOffline ? "연결 없이도 이어 읽을 수 있어요" : "인터넷 연결을 확인해 주세요"
    }
    var offlineMessage: String {
        canResumeOffline
            ? "저장된 기사 \(cachedArticleCount)개가 준비되어 있습니다."
            : "저장된 기사가 없습니다. 연결이 돌아오면 자동으로 다시 확인합니다."
    }
    var lastCheckedText: String {
        guard let lastCheckedAt else { return "아직 확인하지 않음" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.dateFormat = Calendar.current.isDateInToday(lastCheckedAt) ? "오늘 HH:mm" : "M월 d일 HH:mm"
        return formatter.string(from: lastCheckedAt)
    }
    var homePrimaryActionLabel: String {
        sceneProgress[article.id] == nil ? "첫 기사 읽기" : "이어 읽기"
    }
    var homeProgressText: String {
        "\(clampedSceneIndex + 1) / \(max(article.scenes.count, 1)) 장면"
    }
    var currentOnboardingPage: OnboardingPage? {
        onboardingPages[safe: onboardingIndex]
    }
    var hasCurrentPrivacyConsent: Bool {
        snapshot.acceptedPrivacyPolicyVersion == PrivacyPolicy.currentVersion
            && snapshot.privacyConsentReceiptID != nil
    }
    var isSignedIn: Bool {
        if case .signedIn = accountStatus { return true }
        return false
    }
    private var canUsePersonalizedRecommendations: Bool {
        guard isSignedIn,
              hasCurrentPrivacyConsent,
              let activeReadingAccountID,
              accountProfile?.id == activeReadingAccountID
        else { return false }
        return true
    }
    var currentArticleFeedbackReason: ArticleFeedbackReason? {
        articleFeedbackReasons[article.id]
    }
    var accountProfile: AccountProfile? {
        switch accountStatus {
        case let .signedIn(profile): return profile
        case let .error(_, cachedProfile): return cachedProfile
        case .signedOut, .restoring, .signingIn, .deleting: return nil
        }
    }
    var selectedCurationRoute: DiscoveryRoute? {
        if let selectedCurationRouteID,
           let selected = discoveryResponse?.routes.first(where: { $0.id == selectedCurationRouteID }) {
            return selected
        }
        return discoveryResponse?.representativeRoute
    }
    var activeReadingRoute: DiscoveryRoute? {
        guard let activeReadingRouteID else { return nil }
        return discoveryResponse?.routes.first { $0.id == activeReadingRouteID }
    }
    var nextRouteItem: DiscoveryRouteItem? {
        guard let route = activeReadingRoute,
              let currentIndex = route.items.firstIndex(where: { $0.article.articleID == article.id }),
              route.items.indices.contains(currentIndex + 1)
        else { return nil }
        return route.items[currentIndex + 1]
    }
    var nextRouteArticle: PrototypeArticle? {
        guard let nextRouteItem else { return nil }
        return projector.projectDisplayable(nextRouteItem.article)
    }
    var routeContinuationProgressText: String? {
        guard let route = activeReadingRoute, let nextRouteItem else { return nil }
        return String(format: "%02d / %02d", nextRouteItem.position, route.items.count)
    }
    var activeReadingTimeline: FeaturedTimeline? {
        guard let activeReadingTimelineKey else { return nil }
        let timelines = (discoveryResponse?.featuredTimelines ?? [])
            + (discoveryResponse?.timelineResults ?? [])
        return timelines.first { $0.stableKey == activeReadingTimelineKey }
    }
    /// The event right after the one being read, in the rail's own order
    /// (oldest on the left, newest on the right).
    var nextTimelineNode: FeaturedTimelineNode? {
        guard let timeline = activeReadingTimeline else { return nil }
        let ordered = timeline.chronologicalNodes
        guard let currentIndex = ordered.firstIndex(where: {
            $0.article.articleID == article.id
        }), ordered.indices.contains(currentIndex + 1) else { return nil }
        return ordered[currentIndex + 1]
    }
    var nextTimelineArticle: PrototypeArticle? {
        guard let nextTimelineNode else { return nil }
        return projector.projectDisplayable(nextTimelineNode.article)
    }
    var timelineContinuationProgressText: String? {
        guard let timeline = activeReadingTimeline, let nextTimelineNode else { return nil }
        let ordered = timeline.chronologicalNodes
        guard let nextIndex = ordered.firstIndex(where: { $0.id == nextTimelineNode.id }) else {
            return nil
        }
        return String(format: "%02d / %02d", nextIndex + 1, ordered.count)
    }
    var quickDiscoveryResults: [DiscoverySearchResult] {
        discoveryResponse?.results.filter {
            $0.retrieval == .keyword && $0.depth == .quick
        } ?? []
    }
    var deepDiscoveryResults: [DiscoverySearchResult] {
        discoveryResponse?.results.filter {
            $0.retrieval == .keyword && $0.depth == .deep
        } ?? []
    }
    var ragDiscoveryResults: [DiscoverySearchResult] {
        discoveryResponse?.results.filter { $0.retrieval == .rag } ?? []
    }
    var timelineDiscoveryResults: [FeaturedTimeline] {
        discoveryResponse?.timelineResults ?? []
    }
    var isEditorialDiscovery: Bool {
        discoveryResponse?.collection == .editorial && discoveryQuery == "Editorial"
    }
    var isWeeklyInsightDiscovery: Bool {
        discoveryResponse?.collection == .weeklyInsight
            && discoveryQuery == "주간 인사이트"
    }
    var isPublicationDiscovery: Bool {
        isEditorialDiscovery || isWeeklyInsightDiscovery
    }
    var isReadingEditorial: Bool {
        activeReadingCollection == .editorial
    }
    var hasMoreEditorialResults: Bool {
        isEditorialDiscovery && discoveryLoader.hasMoreEditorialPages
    }
    var hasMoreWeeklyInsightResults: Bool {
        isWeeklyInsightDiscovery && discoveryLoader.hasMoreWeeklyInsightPages
    }
    var hasMorePublicationResults: Bool {
        hasMoreEditorialResults || hasMoreWeeklyInsightResults
    }
    var editorialPaginationSentinelID: String? {
        guard isEditorialDiscovery else { return nil }
        return deepDiscoveryResults.last?.id ?? quickDiscoveryResults.last?.id
    }
    var publicationPaginationSentinelID: String? {
        guard isPublicationDiscovery else { return nil }
        return deepDiscoveryResults.last?.id ?? quickDiscoveryResults.last?.id
    }

    func homeProgressText(for article: PrototypeArticle) -> String? {
        guard let index = sceneProgress[article.id] else { return nil }
        let total = max(article.scenes.count, 1)
        let percent = Int((Double(min(index + 1, total)) / Double(total) * 100).rounded())
        return "읽는 중 · \(percent)%"
    }

    func bootstrap() async {
        guard !hasBootstrapped else { return }
        hasBootstrapped = true
        if !hasForcedScreen { currentScreen = .loading }
        snapshot = await persistence.load()
        cachedFeedItems = snapshot.cachedFeed
        catalogFeedItems = cachedFeedItems
        applyReadingState(snapshot.guestReadingState, applyCacheSideEffects: false)
        lastCheckedAt = snapshot.lastCheckedAt
        discoveryResponse = snapshot.cachedDiscovery.map(displayPolicy.filter)
        if let cachedDiscovery = discoveryResponse {
            discoveryState = cachedDiscovery.routes.isEmpty
                && cachedDiscovery.featuredTimelines.isEmpty
                ? .empty
                : .loaded
            selectedCurationRouteID = cachedDiscovery.representativeRoute?.id
            selectedFeaturedTimelineKey = cachedDiscovery.featuredTimelines.first?.stableKey
        }
        savedArticles = projector.project(savedFeedItems)

        if !cachedFeedItems.isEmpty {
            let projected = projector.project(cachedFeedItems)
            if !projected.isEmpty { articles = projected }
        }
        restoreProgress()
        await updateCacheStats()

        accountStatus = .restoring
        let restoredAccount = await accountService.restore()
        accountStatus = restoredAccount.map { .signedIn($0.account) } ?? .signedOut
        if let restoredAccount {
            await activateReadingNamespace(for: restoredAccount.account.id, mergeGuest: true)
            await refreshTimelineSubscriptions()
        }
        await refreshEditorialPushState()
        scheduleArticleViewSync()

        guard !hasForcedScreen else { return }
        onboardingPages = LaunchOnboardingPlan.make(
            isFirstInstall: !snapshot.hasCompletedOnboarding,
            lastPresentedVersion: snapshot.lastPresentedAppVersion,
            currentVersion: currentAppVersion,
            isSignedIn: isSignedIn,
            hasCurrentPrivacyConsent: hasCurrentPrivacyConsent
        )
        onboardingIndex = 0
        isPrivacyConsentSelected = false
        privacyConsentState = hasCurrentPrivacyConsent ? .accepted : .idle
        hasAcknowledgedNewsDisclaimer = false
        if !onboardingPages.isEmpty {
            currentScreen = .onboarding
            showReaderGuide = false
            return
        }
        snapshot.hasSeenReaderGuide = true
        currentScreen = .home
        showReaderGuide = false
        await refreshFeed(showLoading: true, destination: .home)
    }

    func go(to screen: PrototypeScreen) {
        if screen == .settings, currentScreen != .settings { previousScreen = currentScreen }
        if screen == .reader, let origin = stableReaderOrigin(from: currentScreen) {
            readerOriginScreen = origin
        }
        currentScreen = screen
        if screen == .reader, showPrototypeTools {
            prepareReaderGuide()
        } else {
            showReaderGuide = false
        }
        closeOverlays()
    }

    func completeOnboarding() {
        finishOnboarding()
    }

    func advanceOnboarding() {
        guard !onboardingPages.isEmpty else {
            finishOnboarding()
            return
        }
        if currentOnboardingPage == .privacyConsent {
            guard hasCurrentPrivacyConsent else { return }
        }
        if onboardingIndex < onboardingPages.count - 1 {
            onboardingIndex += 1
        } else {
            finishOnboarding()
        }
    }

    func retreatOnboarding() {
        onboardingIndex = max(onboardingIndex - 1, 0)
    }

    func skipOnboardingIntro() {
        if let privacyIndex = onboardingPages.firstIndex(of: .privacyConsent) {
            onboardingIndex = privacyIndex
        } else if let signInIndex = onboardingPages.firstIndex(of: .signIn) {
            onboardingIndex = signInIndex
        } else {
            finishOnboarding()
        }
    }

    func setPrivacyConsentSelected(_ selected: Bool) {
        guard currentScreen == .onboarding,
              currentOnboardingPage == .privacyConsent,
              privacyConsentState != .saving else { return }
        isPrivacyConsentSelected = selected
        if case .failed = privacyConsentState {
            privacyConsentState = .idle
        }
    }

    func acceptPrivacyPolicy() async {
        guard currentScreen == .onboarding,
              currentOnboardingPage == .privacyConsent,
              isPrivacyConsentSelected,
              privacyConsentState != .saving else { return }
        privacyConsentState = .saving
        do {
            let receipt = try await privacyConsentService.record(
                policyVersion: PrivacyPolicy.currentVersion
            )
            guard receipt.policyVersion == PrivacyPolicy.currentVersion,
                  receipt.policyURL == PrivacyPolicy.url else {
                throw ArticleFeedError.invalidResponse
            }
            if isSignedIn, let profile = accountProfile {
                let link = try await accountService.linkPrivacyConsent(receipt.consentID)
                guard link.consentID == receipt.consentID,
                      link.accountID == profile.id,
                      link.policyVersion == receipt.policyVersion,
                      link.policyURL == receipt.policyURL else {
                    throw ArticleFeedError.invalidResponse
                }
            }
            snapshot.acceptedPrivacyPolicyVersion = receipt.policyVersion
            snapshot.privacyConsentReceiptID = receipt.consentID
            try await persistence.save(snapshot)
            privacyConsentState = .accepted
            isPrivacyConsentSelected = false
            scheduleArticleViewSync()
            advanceOnboarding()
        } catch {
            privacyConsentState = .failed(Self.privacyConsentMessage(for: error))
        }
    }

    func setNewsDisclaimerAcknowledged(_ acknowledged: Bool) {
        guard currentScreen == .onboarding, currentOnboardingPage == .signIn else { return }
        hasAcknowledgedNewsDisclaimer = acknowledged
    }

    func continueWithoutAccount() {
        guard currentOnboardingPage == .signIn,
              hasCurrentPrivacyConsent,
              hasAcknowledgedNewsDisclaimer else { return }
        finishOnboarding()
    }

    func completeAppleSignIn(payload: AppleAuthorizationPayload) async {
        guard accountStatus != .signingIn, accountStatus != .deleting else { return }
        let shouldRequestOnboardingNotificationAuthorization = currentScreen == .onboarding
            && currentOnboardingPage == .signIn
        if currentScreen == .onboarding {
            guard currentOnboardingPage == .signIn,
                  hasAcknowledgedNewsDisclaimer else { return }
        }
        guard let privacyConsentID = snapshot.privacyConsentReceiptID,
              hasCurrentPrivacyConsent else {
            accountStatus = .error(
                message: "개인정보 처리방침 동의를 먼저 완료해 주세요.",
                cachedProfile: accountProfile
            )
            return
        }
        accountStatus = .signingIn
        accountFeedback = nil
        do {
            let session = try await accountService.signIn(
                payload,
                privacyConsentID: privacyConsentID
            )
            accountStatus = .signedIn(session.account)
            await activateReadingNamespace(for: session.account.id, mergeGuest: true)
            guard isSignedIn else {
                accountStatus = .error(
                    message: AccountAPIError.unauthorized.userMessage,
                    cachedProfile: nil
                )
                return
            }
            await refreshTimelineSubscriptions()
            await refreshEditorialPushState()
            if shouldRequestOnboardingNotificationAuthorization {
                await requestOnboardingNotificationAuthorization()
            }
            if currentScreen == .onboarding {
                finishOnboarding()
            } else {
                accountFeedback = "Apple 계정으로 로그인했어요."
            }
        } catch {
            accountStatus = .error(
                message: Self.accountMessage(for: error),
                cachedProfile: nil
            )
        }
    }

    func cancelAppleSignIn() {
        if accountStatus == .signingIn {
            accountStatus = .signedOut
        }
    }

    func handleAppleSignInError(_ error: Error) {
        accountStatus = .error(
            message: Self.accountMessage(for: error),
            cachedProfile: accountProfile
        )
    }

    func signOutAccount() async {
        guard isSignedIn || accountProfile != nil else { return }
        await persistNow()
        accountStatus = .restoring
        accountFeedback = nil
        await editorialPushService.disable()
        await accountService.signOut()
        await deactivateReadingNamespace(removingAccountID: nil)
        articleFeedbackReasons.removeAll()
        subscribedTimelineKeys = []
        pendingTimelineSubscriptionKeys = []
        clearTimelineManagementState()
        showArticleFeedback = false
        accountStatus = .signedOut
        editorialPushState = .signedOut
        pushNotificationPreferences = .none
        accountFeedback = "로그아웃했어요. 계정 보관함은 숨기고 비회원 기록으로 전환했어요."
    }

    func deleteAccount() async {
        guard let cachedProfile = accountProfile else { return }
        await persistNow()
        accountStatus = .deleting
        accountFeedback = nil
        do {
            await editorialPushService.disable()
            let result = try await accountService.deleteAccount()
            await deactivateReadingNamespace(removingAccountID: cachedProfile.id)
            articleFeedbackReasons.removeAll()
            subscribedTimelineKeys = []
            pendingTimelineSubscriptionKeys = []
            clearTimelineManagementState()
            showArticleFeedback = false
            accountStatus = .signedOut
            editorialPushState = .signedOut
            pushNotificationPreferences = .none
            accountFeedback = result.appleTokenRevoked
                ? "계정과 서버 읽기·검색·열람 기록을 삭제했어요. 비회원 기록은 이 기기에 남아 있어요."
                : "계정과 서버 읽기·검색·열람 기록을 삭제했어요. Apple 설정에서 연결 상태를 확인해 주세요."
        } catch {
            accountStatus = .error(
                message: Self.accountMessage(for: error),
                cachedProfile: cachedProfile
            )
        }
    }

    func clearAccountFeedback() {
        accountFeedback = nil
        if case let .error(_, cachedProfile) = accountStatus {
            accountStatus = cachedProfile.map(AccountStatus.signedIn) ?? .signedOut
        }
    }

    var isEditorialNotificationSwitchOn: Bool {
        pushNotificationPreferences.editorial && editorialPushState.isDeliveryActive
    }

    var isWeeklyInsightNotificationSwitchOn: Bool {
        pushNotificationPreferences.weeklyInsight && editorialPushState.isDeliveryActive
    }

    var isPushRegistrationRepairNeeded: Bool {
        isSignedIn
            && pushNotificationPreferences.anyEnabled
            && !editorialPushState.isDeliveryActive
    }

    var pushRegistrationRepairDescription: String {
        switch editorialPushState {
        case .denied:
            return "iOS 설정에서 알림 권한을 허용해야 이 기기로 보낼 수 있어요."
        case .failed:
            return "이 기기 등록을 마치지 못했어요. 다시 시도해 주세요."
        case .signedOut, .disabled, .requesting, .registering, .enabled:
            return "알림을 켜 두었지만 이 기기는 아직 등록되지 않았어요."
        }
    }

    var canChangeEditorialNotificationSwitch: Bool {
        editorialPushState.canChangeSwitch
    }

    var canChangeWeeklyInsightNotificationSwitch: Bool {
        editorialPushState.canChangeSwitch
    }

    var isTimelineRefreshNotificationSwitchOn: Bool {
        pushNotificationPreferences.timelineRefreshes
    }

    var isTimelineAdditionNotificationSwitchOn: Bool {
        pushNotificationPreferences.timelineAdditions
    }

    var canChangeTimelineNotificationSwitch: Bool {
        editorialPushState.canChangeSwitch
    }

    func setEditorialNotificationsEnabled(_ enabled: Bool) async {
        await setNotificationsEnabled(.editorial, enabled: enabled)
    }

    func setWeeklyInsightNotificationsEnabled(_ enabled: Bool) async {
        await setNotificationsEnabled(.weeklyInsight, enabled: enabled)
    }

    func setTimelineRefreshNotificationsEnabled(_ enabled: Bool) async {
        await setNotificationsEnabled(.timelineRefreshes, enabled: enabled)
    }

    func setTimelineAdditionNotificationsEnabled(_ enabled: Bool) async {
        await setNotificationsEnabled(.timelineAdditions, enabled: enabled)
    }

    private func setNotificationsEnabled(
        _ kind: NotificationPreferenceKind,
        enabled: Bool
    ) async {
        guard editorialPushState.canChangeSwitch else { return }
        let current: Bool
        switch kind {
        case .editorial: current = pushNotificationPreferences.editorial
        case .weeklyInsight: current = pushNotificationPreferences.weeklyInsight
        case .timelineUpdates: current = pushNotificationPreferences.timelineUpdates
        case .timelineRefreshes: current = pushNotificationPreferences.timelineRefreshes
        case .timelineAdditions: current = pushNotificationPreferences.timelineAdditions
        }
        if enabled, current, isPushRegistrationRepairNeeded {
            await repairPushRegistration()
            return
        }
        guard current != enabled else { return }
        if enabled && !pushNotificationPreferences.anyEnabled {
            editorialPushState = .requesting
        }
        do {
            editorialPushState = try await editorialPushService.setEnabled(
                kind,
                enabled: enabled
            )
            pushNotificationPreferences = editorialPushService.preferences()
            if editorialPushState == .denied {
                announce("알림 권한이 꺼져 있어요. iOS 설정에서 허용해 주세요")
            }
        } catch let ArticleFeedError.server(code, _)
            where code == "invalid_account_session" {
            await editorialPushService.disable()
            await accountService.signOut()
            accountStatus = .signedOut
            editorialPushState = .signedOut
            pushNotificationPreferences = .none
            announce("로그인이 만료됐어요. 다시 로그인해 주세요.")
        } catch {
            pushNotificationPreferences = editorialPushService.preferences()
            editorialPushState = .failed("preference_update_failed")
            announce("알림 설정을 바꾸지 못했어요. 잠시 뒤 다시 시도해 주세요")
        }
    }

    func refreshEditorialPushState() async {
        guard isSignedIn else {
            editorialPushState = .signedOut
            pushNotificationPreferences = .none
            return
        }
        pushNotificationPreferences = editorialPushService.preferences()
        guard pushNotificationPreferences.anyEnabled else {
            editorialPushState = .disabled
            return
        }
        editorialPushState = await editorialPushService.refresh()
    }

    func requestOnboardingNotificationAuthorization() async {
        guard currentScreen == .onboarding,
              currentOnboardingPage == .signIn,
              isSignedIn else { return }
        await requestPushAuthorizationForCurrentPreferences()
    }

    func repairPushRegistration() async {
        guard isSignedIn else {
            editorialPushState = .signedOut
            pushNotificationPreferences = .none
            return
        }
        await requestPushAuthorizationForCurrentPreferences()
    }

    private func requestPushAuthorizationForCurrentPreferences() async {
        pushNotificationPreferences = editorialPushService.preferences()
        guard pushNotificationPreferences.anyEnabled else {
            editorialPushState = .disabled
            return
        }
        editorialPushState = .requesting
        do {
            editorialPushState = try await editorialPushService
                .requestAuthorizationForCurrentPreferences()
            pushNotificationPreferences = editorialPushService.preferences()
            if editorialPushState == .denied {
                announce("알림 권한이 꺼져 있어요. iOS 설정에서 허용해 주세요")
            }
        } catch {
            pushNotificationPreferences = editorialPushService.preferences()
            editorialPushState = .failed("authorization_failed")
            announce("알림 권한을 확인하지 못했어요. 설정에서 다시 시도해 주세요")
        }
    }

    func enableEditorialNotifications() async {
        guard isSignedIn else {
            editorialPushState = .signedOut
            announce("로그인 후 새 editorial 알림을 받을 수 있어요")
            return
        }
        await setNotificationsEnabled(.editorial, enabled: true)
    }

    func disableEditorialNotifications() async {
        await setNotificationsEnabled(.editorial, enabled: false)
    }

    func handlePushDeviceToken(_ token: Data) async {
        guard isSignedIn, editorialPushService.isOptedIn() else { return }
        guard pushRegistrationToken != token else { return }
        let attemptID = UUID()
        let previousState = editorialPushState
        pushRegistrationAttemptID = attemptID
        pushRegistrationToken = token
        editorialPushState = .registering
        defer {
            if pushRegistrationAttemptID == attemptID {
                pushRegistrationAttemptID = nil
                pushRegistrationToken = nil
            }
        }
        do {
            try await editorialPushService.register(deviceToken: token)
            guard pushRegistrationAttemptID == attemptID else { return }
            pushNotificationPreferences = editorialPushService.preferences()
            editorialPushState = .enabled
        } catch is CancellationError {
            guard pushRegistrationAttemptID == attemptID else { return }
            editorialPushState = previousState
        } catch let ArticleFeedError.server(code, _) where code == "invalid_account_session" {
            guard pushRegistrationAttemptID == attemptID else { return }
            await editorialPushService.disable()
            guard pushRegistrationAttemptID == attemptID else { return }
            await accountService.signOut()
            guard pushRegistrationAttemptID == attemptID else { return }
            accountStatus = .signedOut
            editorialPushState = .signedOut
            pushNotificationPreferences = .none
            announce("로그인이 만료됐어요. 다시 로그인해 주세요.")
        } catch {
            guard pushRegistrationAttemptID == attemptID else { return }
            editorialPushState = .failed("registration_failed")
            announce("알림 기기 등록을 완료하지 못했어요")
        }
    }

    func refreshTimelineSubscriptions() async {
        guard isSignedIn else {
            subscribedTimelineKeys = []
            pendingTimelineSubscriptionKeys = []
            return
        }
        do {
            subscribedTimelineKeys = try await accountService.fetchTimelineSubscriptions()
        } catch let error as AccountAPIError where error == .unauthorized {
            await expireAccountAfterTimelineRequest()
        } catch is AccountAuthenticationError {
            await expireAccountAfterTimelineRequest()
        } catch {
            // Preserve the last known set on a transient read failure.
        }
    }

    func refreshTimelineManagement() async {
        guard isSignedIn, let accountID = accountProfile?.id else {
            clearTimelineManagementState()
            return
        }
        let generation = UUID()
        timelineManagementGeneration = generation
        isTimelineManagementLoading = true
        timelineManagementError = nil
        defer {
            if timelineManagementGeneration == generation {
                isTimelineManagementLoading = false
            }
        }
        do {
            let loadedDetails = try await accountService.fetchTimelineSubscriptionDetails()
            let loadedHistory = try await accountService.fetchTimelineSubscriptionHistory()
            guard timelineManagementGeneration == generation,
                  isSignedIn,
                  accountProfile?.id == accountID else { return }
            timelineSubscriptionDetails = Array(loadedDetails.prefix(64))
            timelineSubscriptionHistory = Array(loadedHistory.prefix(100))
            subscribedTimelineKeys = Set(loadedDetails.map(\.stableKey))
        } catch let error as AccountAPIError where error == .unauthorized {
            await expireAccountAfterTimelineRequest()
        } catch is AccountAuthenticationError {
            await expireAccountAfterTimelineRequest()
        } catch {
            timelineManagementError = "타임라인 관리 정보를 불러오지 못했어요."
        }
    }

    private func clearTimelineManagementState() {
        timelineManagementGeneration = nil
        timelineSubscriptionDetails = []
        timelineSubscriptionHistory = []
        isTimelineManagementLoading = false
        timelineManagementError = nil
    }

    func toggleTimelineSubscription(_ stableKey: String) async {
        guard (1...128).contains(stableKey.count),
              stableKey.unicodeScalars.allSatisfy({
                  !CharacterSet.controlCharacters.contains($0)
              }),
              !pendingTimelineSubscriptionKeys.contains(stableKey) else { return }
        guard isSignedIn else {
            announce("로그인 후 타임라인을 구독할 수 있어요. 설정에서 로그인할 수 있습니다.")
            return
        }

        pendingTimelineSubscriptionKeys.insert(stableKey)
        defer { pendingTimelineSubscriptionKeys.remove(stableKey) }
        let isSubscribing = !subscribedTimelineKeys.contains(stableKey)
        let hadTimelinePushPreference = pushNotificationPreferences.timelineUpdates

        if isSubscribing {
            guard await prepareTimelinePushDelivery(
                alreadyEnabled: hadTimelinePushPreference
            ) else { return }
            bookmarkHaptics.prepare()
        }

        do {
            let subscribed = try await accountService.setTimelineSubscription(
                timelineKey: stableKey,
                subscribed: isSubscribing
            )
            guard subscribed == isSubscribing else {
                throw AccountAPIError.invalidResponse
            }
            if subscribed {
                subscribedTimelineKeys.insert(stableKey)
                bookmarkHaptics.playConfirmation()
                announce("업데이트가 발생하면 알려드려요.")
            } else {
                subscribedTimelineKeys.remove(stableKey)
                if subscribedTimelineKeys.isEmpty,
                   pushNotificationPreferences.timelineUpdates {
                    try await setTimelinePushPreference(false)
                }
                announce("타임라인 구독을 해제했어요.")
            }
        } catch let error as AccountAPIError where error == .unauthorized {
            if isSubscribing && !hadTimelinePushPreference {
                try? await setTimelinePushPreference(false)
            }
            await expireAccountAfterTimelineRequest()
        } catch is AccountAuthenticationError {
            if isSubscribing && !hadTimelinePushPreference {
                try? await setTimelinePushPreference(false)
            }
            await expireAccountAfterTimelineRequest()
        } catch {
            if isSubscribing && !hadTimelinePushPreference {
                try? await setTimelinePushPreference(false)
            }
            announce("타임라인 구독을 바꾸지 못했어요. 잠시 뒤 다시 시도해 주세요")
        }
    }

    private func prepareTimelinePushDelivery(alreadyEnabled: Bool) async -> Bool {
        do {
            let state: EditorialPushState
            if alreadyEnabled {
                state = try await editorialPushService
                    .requestAuthorizationForCurrentPreferences()
            } else {
                state = try await editorialPushService.setEnabled(
                    .timelineUpdates,
                    enabled: true
                )
            }
            editorialPushState = state
            pushNotificationPreferences = editorialPushService.preferences()
            switch state {
            case .enabled, .registering:
                return true
            case .denied:
                if !alreadyEnabled { try? await setTimelinePushPreference(false) }
                announce("알림 권한이 꺼져 있어요. iOS 설정에서 허용해 주세요")
            case .signedOut:
                announce("로그인 후 타임라인을 구독할 수 있어요. 설정에서 로그인할 수 있습니다.")
            case .disabled, .requesting, .failed:
                announce("알림 설정을 완료하지 못했어요. 잠시 뒤 다시 시도해 주세요")
            }
        } catch {
            pushNotificationPreferences = editorialPushService.preferences()
            editorialPushState = .failed("timeline_authorization_failed")
            announce("알림 설정을 완료하지 못했어요. 잠시 뒤 다시 시도해 주세요")
        }
        return false
    }

    private func setTimelinePushPreference(_ enabled: Bool) async throws {
        editorialPushState = try await editorialPushService.setEnabled(
            .timelineUpdates,
            enabled: enabled
        )
        pushNotificationPreferences = editorialPushService.preferences()
    }

    private func expireAccountAfterTimelineRequest() async {
        await editorialPushService.disable()
        await accountService.signOut()
        accountStatus = .signedOut
        editorialPushState = .signedOut
        pushNotificationPreferences = .none
        subscribedTimelineKeys = []
        pendingTimelineSubscriptionKeys = []
        clearTimelineManagementState()
        announce("로그인이 만료됐어요. 다시 로그인해 주세요.")
    }

    func presentArticleFeedback() {
        guard !isSubmittingArticleFeedback else { return }
        guard isSignedIn else {
            announce("로그인 후 의견을 보낼 수 있어요. 설정에서 로그인할 수 있습니다.")
            return
        }
        closeOverlays()
        showArticleFeedback = true
    }

    func submitArticleFeedback(_ reason: ArticleFeedbackReason) async {
        guard !isSubmittingArticleFeedback else { return }
        guard isSignedIn else {
            showArticleFeedback = false
            announce("로그인 후 의견을 보낼 수 있어요. 설정에서 로그인할 수 있습니다.")
            return
        }
        let submittedArticleID = article.id
        showArticleFeedback = false
        isSubmittingArticleFeedback = true
        defer { isSubmittingArticleFeedback = false }
        do {
            let record = try await accountService.submitArticleFeedback(
                articleID: submittedArticleID,
                reason: reason
            )
            guard record.articleID == submittedArticleID else {
                throw AccountAPIError.invalidResponse
            }
            articleFeedbackReasons[submittedArticleID] = record.reason
            announce("의견을 반영했어요.")
        } catch let error as AccountAPIError where error == .unauthorized {
            await editorialPushService.disable()
            await accountService.signOut()
            articleFeedbackReasons.removeAll()
            accountStatus = .signedOut
            editorialPushState = .signedOut
            announce("로그인이 만료됐어요. 다시 로그인해 주세요.")
        } catch {
            announce(Self.articleFeedbackMessage(for: error))
        }
    }

    private func finishOnboarding() {
        if currentScreen == .onboarding {
            guard hasCurrentPrivacyConsent else { return }
        }
        if currentScreen == .onboarding, currentOnboardingPage == .signIn {
            guard hasAcknowledgedNewsDisclaimer else { return }
        }
        snapshot.hasCompletedOnboarding = true
        snapshot.hasSeenReaderGuide = true
        snapshot.lastPresentedAppVersion = currentAppVersion
        onboardingPages = []
        onboardingIndex = 0
        hasAcknowledgedNewsDisclaimer = false
        isPrivacyConsentSelected = false
        currentScreen = .home
        schedulePersist()
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            await self?.refreshFeed(showLoading: true, destination: .home)
        }
    }

    func continueReadingFromHome() {
        guard hasReadableContent else {
            homeFeedStatus = .empty
            go(to: .home)
            return
        }
        prepareCurrentArticleEntry()
        go(to: .reader)
    }

    func openExplore() {
        if currentScreen == .reader || currentScreen == .boundary {
            storeCurrentProgress()
        }
        go(to: .explore)
        reconcileSelectedCurationRoute()
        reconcileSelectedFeaturedTimeline()
        let expired = discoveryResponse?.expiresAt.map { $0 <= clock() } ?? true
        guard discoveryResponse == nil || expired else { return }
        startDiscoveryRefresh()
    }

    func loadHomeCurationIfNeeded() {
        guard currentScreen == .home else { return }
        reconcileSelectedCurationRoute()
        let hasNoRoutes = discoveryResponse?.routes.isEmpty ?? true
        let expired = discoveryResponse?.expiresAt.map { $0 <= clock() } ?? true
        guard hasNoRoutes || expired else { return }
        startDiscoveryRefresh()
    }

    func openHomeCuration() {
        guard currentScreen == .home,
              let route = selectedCurationRoute,
              let firstItem = route.items.first
        else { return }
        openRouteArticle(firstItem, in: route)
    }

    func refreshDiscovery() async {
        await discoveryLoader.loadCatalog()
        applyDiscoveryLoaderState(isSearch: false)
    }

    func retryDiscovery() {
        startDiscoveryRefresh(replacingCurrent: true)
    }

    func searchDiscovery(_ query: String) {
        discoverySearchTask?.cancel()
        let normalized = normalizedDiscoveryQuery(query)
        guard !normalized.isEmpty else {
            clearDiscoverySearch()
            return
        }
        let requestID = beginDiscoverySearch(query: normalized)
        discoverySearchTask = Task { [weak self] in
            guard let self else { return }
            await self.performDiscoverySearch(normalized, requestID: requestID)
        }
    }

    func submitDiscoverySearch(_ query: String) async {
        let normalized = normalizedDiscoveryQuery(query)
        guard !normalized.isEmpty else {
            clearDiscoverySearch()
            return
        }
        let requestID = beginDiscoverySearch(query: normalized)
        await performDiscoverySearch(normalized, requestID: requestID)
    }

    func showEditorialDiscovery() {
        discoverySearchTask?.cancel()
        discoveryPaginationTask?.cancel()
        discoveryPaginationTask = nil
        let requestID = beginDiscoverySearch(query: "Editorial")
        discoverySearchTask = Task { [weak self] in
            guard let self else { return }
            await self.performEditorialDiscovery(requestID: requestID)
        }
    }

    func submitEditorialDiscovery() async {
        let requestID = beginDiscoverySearch(query: "Editorial")
        await performEditorialDiscovery(requestID: requestID)
    }

    func showWeeklyInsightDiscovery() {
        discoverySearchTask?.cancel()
        discoveryPaginationTask?.cancel()
        discoveryPaginationTask = nil
        let requestID = beginDiscoverySearch(query: "주간 인사이트")
        discoverySearchTask = Task { [weak self] in
            guard let self else { return }
            await self.performWeeklyInsightDiscovery(requestID: requestID)
        }
    }

    func submitWeeklyInsightDiscovery() async {
        let requestID = beginDiscoverySearch(query: "주간 인사이트")
        await performWeeklyInsightDiscovery(requestID: requestID)
    }

    func loadMoreEditorialResultsIfNeeded(visibleResultID: String) {
        loadMorePublicationResultsIfNeeded(visibleResultID: visibleResultID)
    }

    func loadMorePublicationResultsIfNeeded(visibleResultID: String) {
        guard isPublicationDiscovery,
              visibleResultID == publicationPaginationSentinelID,
              hasMorePublicationResults,
              discoveryPaginationTask == nil
        else { return }
        let collection = discoveryResponse?.collection
        discoveryState = .loading
        discoveryPaginationTask = Task { [weak self] in
            guard let self else { return }
            switch collection {
            case .editorial:
                await self.discoveryLoader.loadNextEditorialPage()
            case .weeklyInsight:
                await self.discoveryLoader.loadNextWeeklyInsightPage()
            case nil:
                break
            }
            guard !Task.isCancelled else {
                self.discoveryPaginationTask = nil
                return
            }
            self.applyDiscoveryLoaderState(isSearch: true)
            self.discoveryPaginationTask = nil
        }
    }

    func clearDiscoverySearch() {
        discoverySearchTask?.cancel()
        discoverySearchTask = nil
        activeDiscoverySearchID = nil
        isDiscoverySearchLoading = false
        discoveryPaginationTask?.cancel()
        discoveryPaginationTask = nil
        discoveryQuery = ""
        if let discoveryResponse {
            self.discoveryResponse = catalogOnly(discoveryResponse)
            discoveryState = discoveryResponse.routes.isEmpty
                && discoveryResponse.featuredTimelines.isEmpty
                ? .empty
                : .loaded
        }
        reconcileSelectedCurationRoute()
        reconcileSelectedFeaturedTimeline()
        reconcileSelectedTimelineSearchResult()
    }

    func selectCurationRoute(_ routeID: UUID) {
        guard discoveryResponse?.routes.contains(where: { $0.id == routeID }) == true else {
            return
        }
        selectedCurationRouteID = routeID
    }

    func selectFeaturedTimeline(_ stableKey: String) {
        guard discoveryResponse?.featuredTimelines.contains(where: {
            $0.stableKey == stableKey
        }) == true else { return }
        selectedFeaturedTimelineKey = stableKey
    }

    func selectTimelineSearchResult(_ stableKey: String) {
        guard timelineDiscoveryResults.contains(where: {
            $0.stableKey == stableKey
        }) else { return }
        selectedTimelineSearchKey = stableKey
    }

    func clearTimelineSearchSelection() {
        selectedTimelineSearchKey = nil
    }

    /// Leave settings and open one subscribed timeline in Explore.
    ///
    /// A subscription outlives the edition it was made from — the catalog keeps
    /// 4..8 timelines while an account keeps up to 64 subscriptions — so the key
    /// may no longer be in the published catalog.  That is a normal state, not an
    /// error, and it is reported instead of leaving Explore on some other card.
    func openSubscribedTimeline(_ stableKey: String) async {
        guard Self.isOpenableTimelineKey(stableKey),
              pendingTimelineOpenKey == nil else { return }
        pendingTimelineOpenKey = stableKey
        defer { pendingTimelineOpenKey = nil }

        if currentScreen == .reader || currentScreen == .boundary {
            storeCurrentProgress()
        }
        discoverySearchTask?.cancel()
        discoverySearchTask = nil
        discoveryPaginationTask?.cancel()
        discoveryPaginationTask = nil
        activeDiscoverySearchID = nil
        isDiscoverySearchLoading = false
        discoveryQuery = ""
        selectedTimelineSearchKey = nil
        go(to: .explore)

        if !isTimelinePublished(stableKey) {
            await discoveryLoader.loadCatalog()
            applyDiscoveryLoaderState(isSearch: false)
        }
        guard isTimelinePublished(stableKey) else {
            announce("이 타임라인은 지금 둘러보기 판에 없어요.")
            return
        }
        selectedFeaturedTimelineKey = stableKey
    }

    private func isTimelinePublished(_ stableKey: String) -> Bool {
        discoveryResponse?.featuredTimelines.contains(where: {
            $0.stableKey == stableKey
        }) == true
    }

    private static func isOpenableTimelineKey(_ stableKey: String) -> Bool {
        (1...128).contains(stableKey.count)
            && stableKey.unicodeScalars.allSatisfy {
                !CharacterSet.controlCharacters.contains($0)
            }
    }

    func openTimelineArticle(_ node: FeaturedTimelineNode) {
        let timelines = (discoveryResponse?.featuredTimelines ?? [])
            + (discoveryResponse?.timelineResults ?? [])
        guard let timeline = timelines.first(where: { timeline in
            timeline.nodes.contains(where: { $0.id == node.id })
        }) else {
            announce("이 타임라인의 기사를 열 수 없어요")
            return
        }
        openDiscoveryArticle(
            node.article,
            routeID: nil,
            readingCollection: nil,
            timelineKey: timeline.stableKey
        )
    }

    func openRouteArticle(_ item: DiscoveryRouteItem, in route: DiscoveryRoute) {
        guard let canonicalRoute = discoveryResponse?.routes.first(where: { $0.id == route.id }),
              let canonicalItem = canonicalRoute.items.first(where: { $0.id == item.id })
        else {
            announce("이 읽기 루트의 기사를 열 수 없어요")
            return
        }
        openDiscoveryArticle(canonicalItem.article, routeID: canonicalRoute.id, readingCollection: nil)
    }

    func openDiscoveryArticle(_ item: ArticleFeedItem) {
        openDiscoveryArticle(item, routeID: nil, readingCollection: nil)
    }

    func openDiscoverySearchResult(_ item: ArticleFeedItem) {
        let readingCollection: DiscoveryCollection? = isEditorialDiscovery
            && discoveryResponse?.results.contains(where: { $0.article.articleID == item.articleID }) == true
            ? .editorial
            : nil
        openDiscoveryArticle(item, routeID: nil, readingCollection: readingCollection)
    }

    private func openDiscoveryArticle(
        _ item: ArticleFeedItem,
        routeID: UUID?,
        readingCollection: DiscoveryCollection?,
        timelineKey: String? = nil
    ) {
        guard let projected = projector.projectDisplayable(item), !projected.scenes.isEmpty else {
            announce("이 기사를 읽기 화면으로 열 수 없어요")
            return
        }
        if currentScreen == .reader || currentScreen == .boundary {
            storeCurrentProgress()
        }
        selectedSavedArticle = nil
        selectedDiscoveryArticle = projected
        selectedDiscoveryFeedItem = item
        activeReadingRouteID = routeID
        activeReadingTimelineKey = timelineKey
        activeReadingCollection = readingCollection
        prepareCurrentArticleEntry()
        go(to: .reader)
    }

    func handleEditorialNotification(
        articleID: String,
        collection: DiscoveryCollection? = nil
    ) async {
        let allowed = CharacterSet.alphanumerics.union(
            CharacterSet(charactersIn: "-._~")
        )
        guard !articleID.isEmpty,
              articleID.count <= 128,
              articleID.unicodeScalars.allSatisfy(allowed.contains) else {
            announce("이 알림의 기사를 열 수 없어요")
            return
        }
        if let known = rawFeedItem(for: articleID) {
            openDiscoveryArticle(known, routeID: nil, readingCollection: collection)
            return
        }

        if let collection,
           let published = await discoveryLoader.findCollectionArticle(
               articleID,
               in: collection
           ) {
            mergeCatalog([published], placingNewFirst: true)
            await syncOfflineCacheFromCatalog()
            schedulePersist()
            openDiscoveryArticle(published, routeID: nil, readingCollection: collection)
            return
        }

        await loader.loadInitial()
        guard let refreshed = loader.items.first(where: { $0.articleID == articleID }) else {
            announce("이 editorial은 아직 이 기기에서 불러올 수 없어요")
            return
        }
        mergeCatalog([refreshed], placingNewFirst: true)
        await syncOfflineCacheFromCatalog()
        schedulePersist()
        openDiscoveryArticle(refreshed, routeID: nil, readingCollection: nil)
    }

    func handleTimelineNotification(_ pending: PendingTimelinePush) async {
        discoverySearchTask?.cancel()
        discoverySearchTask = nil
        discoveryPaginationTask?.cancel()
        discoveryPaginationTask = nil
        activeDiscoverySearchID = nil
        isDiscoverySearchLoading = false
        discoveryQuery = ""
        selectedTimelineSearchKey = nil
        if currentScreen == .reader || currentScreen == .boundary {
            storeCurrentProgress()
        }
        go(to: .explore)

        await discoveryLoader.loadCatalog()
        applyDiscoveryLoaderState(isSearch: false)

        guard let timeline = discoveryResponse?.featuredTimelines.first(where: {
            $0.stableKey == pending.stableKey
        }),
        let node = timeline.nodes.first(where: { $0.id == pending.nodeID }),
        node.updateKind == pending.updateKind else {
            announce("업데이트된 사건을 아직 불러오지 못했어요.")
            return
        }
        selectedFeaturedTimelineKey = timeline.stableKey
        let entrance: TimelineEntrance = pending.updateKind == .inserted
            ? .inserted(node.id)
            : .occurred(node.id)
        timelineEntranceRequest = TimelineEntranceRequest(
            id: pending.eventID,
            timelineKey: timeline.stableKey,
            nodeID: node.id,
            entrance: entrance
        )
    }

    func consumeTimelineEntrance(_ eventID: UUID) {
        guard timelineEntranceRequest?.id == eventID else { return }
        timelineEntranceRequest = nil
    }

    func goHome() {
        if currentScreen == .reader || currentScreen == .boundary { storeCurrentProgress() }
        clearSelectedDiscoveryArticle()
        go(to: .home)
    }

    func selectPrimaryTab(_ tab: PrimaryAppTab) {
        switch tab {
        case .home:
            goHome()
        case .reader:
            openExplore()
        case .library:
            go(to: .library)
        case .settings:
            go(to: .settings)
        }
    }

    func openArticleFromHome(_ selectedArticle: PrototypeArticle) {
        guard let index = articles.firstIndex(where: { $0.id == selectedArticle.id }) else {
            guard let latest = homeLatestFeedItems.first(where: {
                $0.articleID == selectedArticle.id
            }) else { return }
            openDiscoveryArticle(latest, routeID: nil, readingCollection: nil)
            return
        }
        if currentScreen == .reader || currentScreen == .boundary { storeCurrentProgress() }
        openArticle(at: index)
    }

    func dismissReaderGuide() {
        showReaderGuide = false
        snapshot.hasSeenReaderGuide = true
        schedulePersist()
    }

    func connectivityUpdates() -> AsyncStream<ConnectivityState> {
        connectivity.updates()
    }

    func handleConnectivity(_ state: ConnectivityState) async {
        let previous = connectivityState
        connectivityState = state
        switch state {
        case .unknown:
            return
        case .offline:
            isOffline = true
        case .online:
            let shouldRefresh = previous == .offline
                && snapshot.hasCompletedOnboarding
                && (currentScreen == .home || currentScreen == .offline || currentScreen == .empty)
            if currentScreen == .reader { isOffline = false }
            if shouldRefresh, !isLoadingFeed {
                let destination: PrototypeScreen = currentScreen == .home ? .home : .reader
                await refreshFeed(showLoading: true, destination: destination)
            }
            scheduleReadingSync()
            scheduleArticleViewSync()
        }
    }

    func handleScenePhase(_ phase: ScenePhase) async {
        switch phase {
        case .inactive, .background:
            await flushMediaCleanup()
            await flushPersistence()
        case .active:
            scheduleReadingSync()
            scheduleArticleViewSync()
        @unknown default:
            break
        }
    }

    func refreshFeed(
        showLoading: Bool = true,
        destination: PrototypeScreen = .reader
    ) async {
        guard !isLoadingFeed else { return }
        isLoadingFeed = true
        if showLoading {
            if destination == .home {
                currentScreen = .home
                homeFeedStatus = .loading
            } else {
                currentScreen = .loading
            }
        }
        closeOverlays()
        if destination == .home, canUsePersonalizedRecommendations {
            await loadPersonalizedHome(trigger: .initial, replacing: true)
            await refreshLatestHomeArticles()
            isLoadingFeed = false
            schedulePersist()
            return
        }
        await loader.loadInitial()
        lastCheckedAt = clock()
        snapshot.lastCheckedAt = lastCheckedAt

        switch loader.state {
        case .loaded:
            await acceptLoadedFeed(loader.items, destination: destination)
        case .empty:
            routeEmptyOrCached(destination: destination)
        case let .failed(message, retryAt):
            routeFailure(code: message, retryAt: retryAt, destination: destination)
        case .idle:
            routeFailure(code: "feed_cancelled", retryAt: nil, destination: destination)
        case .loading:
            break
        }
        isLoadingFeed = false
        schedulePersist()
    }

    func refreshHomeFromPull() async {
        guard !isLoadingFeed else { return }
        isLoadingFeed = true
        closeOverlays()
        if canUsePersonalizedRecommendations {
            paginationTask?.cancel()
            paginationTask = nil
            homePaginationState = .idle
            await loadPersonalizedHome(trigger: .pull, replacing: true)
            await refreshLatestHomeArticles()
            isLoadingFeed = false
            schedulePersist()
            return
        }
        let excludedArticleIDs = Array(catalogFeedItems.prefix(32).map(\.articleID))
        await loader.loadRandom(excludingArticleIDs: excludedArticleIDs)
        lastCheckedAt = clock()
        snapshot.lastCheckedAt = lastCheckedAt

        switch loader.state {
        case .loaded:
            await acceptLoadedFeed(loader.items, destination: .home)
        case .empty:
            currentScreen = .home
            if catalogFeedItems.isEmpty {
                routeEmptyOrCached(destination: .home)
            } else {
                homeFeedStatus = .idle
            }
        case let .failed(message, retryAt):
            supportCode = message.uppercased().replacingOccurrences(of: "_", with: "-")
            currentScreen = .home
            if catalogFeedItems.isEmpty {
                routeFailure(code: message, retryAt: retryAt, destination: .home)
            } else if message == "feed_cooldown" {
                homeFeedStatus = .cooldown
                announce("잠시 뒤에 다시 시도해 주세요")
            } else {
                homeFeedStatus = .failed
            }
        case .idle:
            currentScreen = .home
            if catalogFeedItems.isEmpty {
                routeFailure(code: "feed_cancelled", retryAt: nil, destination: .home)
            }
        case .loading:
            break
        }
        isLoadingFeed = false
        schedulePersist()
    }

    func toggleContext() {
        showContext.toggle()
        showOverflow = false
    }

    func toggleOverflow() {
        showOverflow.toggle()
        showContext = false
    }

    func nextScene() {
        if sceneIndex >= article.scenes.count - 1 {
            let completedArticleID = article.id
            sceneProgress[completedArticleID] = sceneIndex
            snapshot.lastArticleID = completedArticleID
            enqueueReadingSync(
                PersistedReadingDelta(
                    progressChanges: [completedArticleID: sceneIndex],
                    updateLastArticle: true,
                    lastArticleID: completedArticleID
                )
            )
            enqueueCurrentArticleActivity(.articleCompleted)
            schedulePersist()
            go(to: .boundary)
            scheduleCompletedArticleMediaCleanup(articleID: completedArticleID)
        } else {
            sceneIndex += 1
            storeCurrentProgress()
            enqueueCurrentArticleActivity(.sceneViewed)
            closeOverlays()
        }
    }

    func previousScene() {
        guard sceneIndex > 0 else { return }
        sceneIndex -= 1
        storeCurrentProgress()
        enqueueCurrentArticleActivity(.sceneViewed)
        closeOverlays()
    }

    func requestNewArticle() {
        guard !isLoadingNewArticle else { return }
        isLoadingNewArticle = true
        closeOverlays()
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            await self?.loadNewArticle()
        }
    }

    func downloadNewArticlesFromHome() {
        startHomePagination()
    }

    func loadNewArticlesForHome() async {
        await loadMoreHomeArticles()
    }

    func homeArticleDidAppear(_ articleID: String) {
        let visibleArticles = homeArticles
        let hasMorePages = canUsePersonalizedRecommendations
            ? homeRecommendationCursor != nil
            : loader.hasMorePages
        guard currentScreen == .home,
              homePaginationState == .idle,
              hasMorePages,
              paginationTask == nil,
              let index = visibleArticles.firstIndex(where: { $0.id == articleID }),
              index >= max(visibleArticles.count - 2, 0)
        else { return }
        startHomePagination()
    }

    func retryHomePagination() {
        guard homePaginationState == .failed else { return }
        homePaginationState = .idle
        startHomePagination()
    }

    func loadMoreHomeArticles() async {
        guard !isLoadingNewArticle else { return }
        isLoadingNewArticle = true
        homePaginationState = .loading
        closeOverlays()
        defer { isLoadingNewArticle = false }

        if canUsePersonalizedRecommendations {
            await loadPersonalizedHome(trigger: .pagination, replacing: false)
            schedulePersist()
            return
        }

        let selectedID = hasReadableContent ? article.id : nil
        var cooldownRetries = 0

        while true {
            let existingIDs = Set(catalogFeedItems.map(\.articleID))
            await loader.loadNextBatch()
            lastCheckedAt = clock()
            snapshot.lastCheckedAt = lastCheckedAt

            switch loader.state {
            case .loaded:
                let page = Array(loader.items.prefix(5))
                let addedCount = displayPolicy.filter(page).reduce(into: 0) { count, item in
                    if !existingIDs.contains(item.articleID) { count += 1 }
                }
                mergeCatalog(page, placingNewFirst: false)
                rebuildCatalogArticles(preserving: selectedID)
                await syncOfflineCacheFromCatalog()
                isOffline = false
                homeFeedStatus = addedCount > 0 ? .downloaded(addedCount) : .empty
                homePaginationState = loader.hasMorePages ? .idle : .end
                await updateCacheStats()
                schedulePersist()
                return
            case .empty, .idle:
                homeFeedStatus = .empty
                homePaginationState = .end
                schedulePersist()
                return
            case .loading:
                homePaginationState = .idle
                return
            case let .failed(message, retryAt):
                if message == "feed_cooldown", cooldownRetries == 0 {
                    cooldownRetries += 1
                    let seconds = min(max(retryAt?.timeIntervalSinceNow ?? 1, 0.05), 30)
                    do {
                        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                    } catch {
                        homePaginationState = .idle
                        return
                    }
                    guard currentScreen == .home else {
                        homePaginationState = .idle
                        return
                    }
                    continue
                }
                if Self.isAttestFailure(message) {
                    supportCode = message.uppercased().replacingOccurrences(of: "_", with: "-")
                    homeFeedStatus = .failed
                    homePaginationState = .failed
                    currentScreen = .attestError
                } else {
                    isOffline = true
                    homeFeedStatus = canResumeOffline ? .offlineCached(cachedArticleCount) : .failed
                    homePaginationState = .failed
                }
                schedulePersist()
                return
            }
        }
    }

    func loadNewArticle() async {
        if !isLoadingNewArticle {
            isLoadingNewArticle = true
            closeOverlays()
        }
        defer { isLoadingNewArticle = false }

        let currentArticleID = article.id
        sceneProgress[currentArticleID] = clampedSceneIndex
        snapshot.lastArticleID = currentArticleID
        schedulePersist()

        if openFirstUnreadArticle(in: articles, after: currentArticleID) {
            return
        }

        await loader.loadNextBatch()
        lastCheckedAt = clock()
        snapshot.lastCheckedAt = lastCheckedAt

        switch loader.state {
        case .loaded:
            let limited = Array(loader.items.prefix(5))
            let projected = projector.project(limited)
            guard !projected.isEmpty,
                  let candidateIndex = firstUnreadArticleIndex(in: projected, after: currentArticleID)
            else {
                announce("지금은 불러올 새 기사가 없어요")
                schedulePersist()
                return
            }

            articles = projected
            if settings.offlineCache {
                cachedFeedItems = limited
                snapshot.cachedFeed = limited
            }
            isOffline = false
            openArticle(at: candidateIndex)
            if settings.offlineCache { await prefetchCurrentImages() }
            await updateCacheStats()
        case .empty, .idle, .loading:
            announce("지금은 불러올 새 기사가 없어요")
        case let .failed(message, _):
            if message == "feed_cooldown" {
                announce("잠시 뒤에 다시 시도해 주세요")
            } else if Self.isAttestFailure(message) {
                announce("새 기사를 확인할 수 없어요")
            } else {
                announce("연결을 확인한 뒤 다시 시도해 주세요")
            }
        }
        schedulePersist()
    }

    func jump(to index: Int) {
        let previousIndex = sceneIndex
        sceneIndex = min(max(index, 0), article.scenes.count - 1)
        storeCurrentProgress()
        if sceneIndex != previousIndex {
            enqueueCurrentArticleActivity(.sceneViewed)
        }
        closeOverlays()
    }

    func continueNextArticle() {
        guard let selectedArticle = displayedNextArticles.first else {
            go(to: .empty)
            return
        }
        continueNextArticle(selectedArticle)
    }

    func continueNextArticle(_ selectedArticle: PrototypeArticle) {
        if let index = articles.firstIndex(where: { $0.id == selectedArticle.id }) {
            openArticle(at: index)
            return
        }
        guard boundaryRecommendationItems.contains(where: {
            $0.articleID == selectedArticle.id
        }) else { return }
        articles.append(selectedArticle)
        let index = articles.count - 1
        openArticle(at: index)
    }

    func continueNextRouteArticle() {
        guard let routeID = activeReadingRouteID, let nextRouteItem else {
            announce("다음 루트 기사를 찾을 수 없어요")
            return
        }
        openDiscoveryArticle(nextRouteItem.article, routeID: routeID, readingCollection: nil)
    }

    func continueNextTimelineArticle() {
        guard let timelineKey = activeReadingTimelineKey, let nextTimelineNode else {
            announce("다음 사건을 찾을 수 없어요")
            return
        }
        openDiscoveryArticle(
            nextTimelineNode.article,
            routeID: nil,
            readingCollection: nil,
            timelineKey: timelineKey
        )
    }

    func returnToLastScene() {
        sceneIndex = max(article.scenes.count - 1, 0)
        storeCurrentProgress()
        go(to: .reader)
    }

    func returnToReaderOrigin() {
        guard currentScreen == .reader || currentScreen == .boundary else { return }
        storeCurrentProgress()
        let destination = stableReaderOrigin(from: readerOriginScreen) ?? .home
        clearSelectedDiscoveryArticle()
        selectedSavedArticle = nil
        go(to: destination)
    }

    func toggleSaved() {
        if isSaved {
            removeSaved(articleID: article.id)
        } else {
            guard let item = rawFeedItem(for: article.id) else {
                announce("이 기사의 원본 데이터를 저장할 수 없어요")
                return
            }
            bookmarkHaptics.prepare()
            let savedDate = clock()
            savedArticleIDs.insert(article.id)
            savedAt[article.id] = savedDate
            savedFeedItems.removeAll { $0.articleID == item.articleID }
            savedFeedItems.append(item)
            trimSavedFeed()
            rebuildSavedArticles()
            bookmarkHaptics.playConfirmation()
            announce("보관함에 저장했어요")
            enqueueReadingSync(
                PersistedReadingDelta(
                    savedChanges: [
                        article.id: PendingSavedChange(isSaved: true, savedAt: savedDate),
                    ]
                )
            )
            schedulePersist()
        }
    }

    func removeSaved(articleID: String? = nil) {
        let id = articleID ?? article.id
        savedArticleIDs.remove(id)
        savedAt.removeValue(forKey: id)
        savedFeedItems.removeAll { $0.articleID == id }
        rebuildSavedArticles()
        if selectedSavedArticle?.id == id {
            selectedSavedArticle = nil
        }
        enqueueReadingSync(
            PersistedReadingDelta(
                savedChanges: [id: PendingSavedChange(isSaved: false, savedAt: nil)]
            )
        )
        schedulePersist()
        announce("저장을 해제했어요")
    }

    func openSaved(_ saved: PrototypeArticle) {
        clearSelectedDiscoveryArticle()
        selectedSavedArticle = saved
        prepareCurrentArticleEntry()
        go(to: .reader)
    }

    func share() {
        closeOverlays()
        guard let url = article.sourceURL else {
            announce("공유할 원문 주소가 없어요")
            return
        }
        sharePayload = SharePayload(headline: article.scenes.first?.title ?? article.source, url: url)
    }

    func openOriginal() {
        closeOverlays()
        guard let url = article.sourceURL else {
            announce("원문 주소를 찾을 수 없어요")
            return
        }
        pendingExternalURL = url
    }

    func consumeExternalURL() { pendingExternalURL = nil }

    func handleOpenURLResult(wasDiscarded: Bool) {
        if wasDiscarded { announce("원문을 열 수 없어요") }
    }

    func resumeOffline() {
        let projected = projector.project(cachedFeedItems)
        guard !projected.isEmpty else {
            currentScreen = .empty
            return
        }
        articles = projected
        isOffline = true
        restoreProgress()
        prepareCurrentArticleEntry()
        go(to: .reader)
    }

    func dismissOffline() {
        if !canResumeOffline {
            go(to: .empty)
        } else {
            go(to: .reader)
        }
    }

    func retryLoad() {
        refreshTask?.cancel()
        let destination: PrototypeScreen = currentScreen == .home ? .home : .reader
        refreshTask = Task { [weak self] in
            await self?.refreshFeed(showLoading: true, destination: destination)
        }
    }

    func retryHomeRandom() {
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            await self?.refreshHomeFromPull()
        }
    }

    func retryAttest() {
        guard !isAttestRetrying else { return }
        isAttestRetrying = true
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            await self?.refreshFeed(showLoading: true, destination: .home)
            self?.isAttestRetrying = false
        }
    }

    func setTypeSize(_ value: ReaderTypeSize) { settings.typeSize = value; settingsDidChange() }
    func setLeading(_ value: ReaderLeading) { settings.leading = value; settingsDidChange() }
    func setTheme(_ value: ReaderTheme) { settings.theme = value; settingsDidChange() }
    func setMotionEnabled(_ value: Bool) { settings.motionEnabled = value; settingsDidChange() }
    func setMediaSaver(_ value: Bool) { settings.mediaSaver = value; settingsDidChange() }

    func setOfflineCache(_ value: Bool) {
        settings.offlineCache = value
        if value {
            snapshot.cachedFeed = Array(catalogFeedItems.prefix(PersistedAppSnapshot.maximumCachedArticles))
            cachedFeedItems = snapshot.cachedFeed
            Task { [weak self] in await self?.prefetchCurrentImages() }
        } else {
            snapshot.cachedFeed = []
            cachedFeedItems = []
            scheduleTransientMediaClear()
        }
        settingsDidChange()
    }

    func resetSettings() {
        settings = ReaderSettings()
        if settings.offlineCache, !catalogFeedItems.isEmpty {
            snapshot.cachedFeed = Array(catalogFeedItems.prefix(PersistedAppSnapshot.maximumCachedArticles))
            cachedFeedItems = snapshot.cachedFeed
        }
        settingsDidChange()
        announce("읽기 설정을 기본값으로 돌렸어요")
    }

    func finishSettings() {
        schedulePersist()
        go(to: previousScreen)
    }

    func closeOverlays() {
        showContext = false
        showOverflow = false
        showArticleFeedback = false
    }

    func announce(_ message: String) {
        toastTask?.cancel()
        toastMessage = message
        UIAccessibility.post(notification: .announcement, argument: message)
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_600_000_000)
            guard !Task.isCancelled else { return }
            self?.toastMessage = nil
        }
    }

    func flushPersistence() async {
        persistTask?.cancel()
        await persistNow()
    }

    func flushReadingSync() async {
        await runReadingSyncImmediately()
        while let task = readingSyncTask {
            await task.value
        }
    }

    func flushArticleViewSync() async {
        if let task = articleViewTask {
            await task.value
        } else {
            await runArticleViewSyncImmediately()
        }
        while let task = articleViewTask {
            await task.value
        }
    }

    func flushMediaCleanup() async {
        await mediaCleanupTask?.value
    }

    func savedDateText(for articleID: String) -> String {
        guard let date = savedAt[articleID] else { return "저장됨" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: clock())
    }

    private func acceptLoadedFeed(
        _ items: [ArticleFeedItem],
        destination: PrototypeScreen
    ) async {
        let limited = Array(items.prefix(5))
        guard !limited.isEmpty else {
            routeEmptyOrCached(destination: destination)
            return
        }
        let projected = projector.project(limited)
        guard !projected.isEmpty else {
            routeEmptyOrCached(destination: destination)
            return
        }
        let selectedID = hasReadableContent ? article.id : snapshot.lastArticleID
        if destination == .home {
            mergeCatalog(limited, placingNewFirst: true)
            rebuildCatalogArticles(preserving: selectedID)
            homeFeedStatus = .idle
            homePaginationState = loader.hasMorePages ? .idle : .end
        } else {
            catalogFeedItems = limited
            articles = projected
            articleIndex = min(articleIndex, projected.count - 1)
        }
        if settings.offlineCache {
            cachedFeedItems = Array(catalogFeedItems.prefix(PersistedAppSnapshot.maximumCachedArticles))
            snapshot.cachedFeed = cachedFeedItems
        } else {
            cachedFeedItems = []
            snapshot.cachedFeed = []
        }
        isOffline = false
        restoreProgress()
        go(to: destination)
        if destination == .home {
            await refreshLatestHomeArticles()
        }
        if settings.offlineCache { await prefetchCurrentImages() }
        await updateCacheStats()
    }

    private func routeEmptyOrCached(destination: PrototypeScreen) {
        if destination == .home {
            currentScreen = .home
            clearLatestHomeArticles()
            if canResumeOffline {
                catalogFeedItems = cachedFeedItems
                rebuildCatalogArticles(preserving: snapshot.lastArticleID)
                isOffline = true
                homeFeedStatus = .offlineCached(cachedArticleCount)
            } else {
                homeFeedStatus = .empty
                homePaginationState = .end
            }
            return
        }
        if canResumeOffline {
            isOffline = true
            currentScreen = .offline
        } else {
            currentScreen = .empty
        }
    }

    private func routeFailure(code: String, retryAt: Date?, destination: PrototypeScreen) {
        supportCode = code.uppercased().replacingOccurrences(of: "_", with: "-")
        if Self.isAttestFailure(code) {
            currentScreen = .attestError
        } else if destination == .home {
            currentScreen = .home
            if code == "feed_cooldown" {
                homeFeedStatus = .cooldown
                homePaginationState = .failed
                announce("잠시 뒤에 다시 시도해 주세요")
            } else if canResumeOffline {
                clearLatestHomeArticles()
                catalogFeedItems = cachedFeedItems
                rebuildCatalogArticles(preserving: snapshot.lastArticleID)
                isOffline = true
                homeFeedStatus = .offlineCached(cachedArticleCount)
                homePaginationState = .failed
            } else {
                isOffline = true
                homeFeedStatus = .failed
                homePaginationState = .failed
            }
        } else if code == "feed_cooldown" {
            if hasReadableContent {
                go(to: .reader)
            } else {
                go(to: .empty)
            }
            announce("잠시 뒤에 다시 시도해 주세요")
        } else if canResumeOffline {
            isOffline = true
            currentScreen = .offline
        } else if hasReadableContent {
            isOffline = true
            go(to: .reader)
            announce("연결이 돌아오면 새 기사를 확인할게요")
        } else {
            isOffline = true
            currentScreen = .offline
        }
    }

    private func mergeCatalog(_ items: [ArticleFeedItem], placingNewFirst: Bool) {
        let combined = placingNewFirst ? items + catalogFeedItems : catalogFeedItems + items
        var seen = Set<String>()
        catalogFeedItems = combined.filter { seen.insert($0.articleID).inserted }
    }

    private func refreshLatestHomeArticles() async {
        guard currentScreen == .home, !isOffline, let lead = homeArticles.first else {
            clearLatestHomeArticles()
            return
        }
        var seen = Set<String>()
        var exclusions = [lead.id]
        seen.insert(lead.id)
        for candidate in articles + homeLatestArticles
        where isCompletedForHome(candidate) && seen.insert(candidate.id).inserted {
            exclusions.append(candidate.id)
            if exclusions.count == 32 { break }
        }

        await loader.loadLatest(excludingArticleIDs: exclusions)
        guard loader.latestState == .loaded else {
            clearLatestHomeArticles()
            return
        }
        let eligibleItems = displayPolicy.filter(loader.latestItems)
        let projected = projector.project(eligibleItems)
        var visibleIDs = Set<String>()
        let visible = projected.filter {
            $0.id != lead.id
                && !isCompletedForHome($0)
                && visibleIDs.insert($0.id).inserted
        }
        let selected = Array(visible.prefix(HomeCatalogPolicy.recentArticleLimit))
        let selectedIDs = Set(selected.map(\.id))
        homeLatestFeedItems = eligibleItems.filter {
            selectedIDs.contains($0.articleID)
        }
        homeLatestArticles = selected
    }

    private func clearLatestHomeArticles() {
        homeLatestFeedItems = []
        homeLatestArticles = []
    }

    func loadPersonalizedHome(
        trigger: HomeRecommendationTrigger,
        replacing: Bool
    ) async {
        guard canUsePersonalizedRecommendations,
              let accountID = activeReadingAccountID,
              let timeContext = RecommendationTimeContext(occurredAt: clock())
        else {
            recommendationState = .fallback
            return
        }

        let generation = UUID()
        recommendationGeneration = generation
        recommendationState = .loading
        if replacing {
            homePaginationState = .idle
        }
        let exclusions = recommendationExclusions(
            replacing: replacing,
            trigger: trigger
        )
        let request = AccountRecommendationRequest(
            surface: .home,
            contextArticleID: nil,
            excludeArticleIDs: exclusions,
            timeContext: timeContext,
            limit: 5,
            cursor: replacing ? nil : homeRecommendationCursor
        )
        guard request.isValid else {
            applyRecommendationFallback()
            return
        }

        do {
            let page = try await accountService.fetchRecommendations(request)
            guard recommendationGeneration == generation,
                  activeReadingAccountID == accountID,
                  accountProfile?.id == accountID,
                  canUsePersonalizedRecommendations,
                  page.surface == .home else { return }

            let pageItems = displayPolicy.filter(page.items)
            let combined = replacing
                ? pageItems
                : homeRecommendationItems + pageItems
            var seenArticleIDs = Set<String>()
            homeRecommendationItems = Array(
                combined
                    .filter {
                        AccountRecommendationRequest.isSHA256($0.articleID)
                            && seenArticleIDs.insert($0.articleID).inserted
                    }
                    .prefix(15)
            )
            homeRecommendationCursor = page.nextCursor
            recommendationState = .loaded
            homePaginationState = page.nextCursor == nil ? .end : .idle
            homeFeedStatus = pageItems.isEmpty && homeRecommendationItems.isEmpty
                ? .empty
                : .idle
            lastCheckedAt = clock()
            snapshot.lastCheckedAt = lastCheckedAt

            persistHomeRecommendationCache(
                generatedAt: page.generatedAt,
                rankingVersion: page.rankingVersion
            )
            if !homeRecommendationItems.isEmpty {
                rebuildPersonalizedHomeArticles(
                    preserving: hasReadableContent ? article.id : snapshot.lastArticleID
                )
                currentScreen = .home
                isOffline = false
                if settings.offlineCache {
                    await mediaCache.prefetch(
                        homeRecommendationItems.flatMap { $0.map.images.map(\.url) }
                    )
                    await updateCacheStats()
                }
            } else {
                applyRecommendationFallback()
            }
        } catch {
            guard recommendationGeneration == generation,
                  activeReadingAccountID == accountID else { return }
            await handleRecommendationFailure(error)
        }
    }

    private func recommendationExclusions(
        replacing: Bool,
        trigger: HomeRecommendationTrigger
    ) -> [String] {
        let candidates: [String]
        if replacing, trigger == .initial {
            candidates = []
        } else {
            candidates = articles.map(\.id)
        }
        var seen = Set<String>()
        return candidates
            .filter {
                AccountRecommendationRequest.isSHA256($0)
                    && seen.insert($0).inserted
            }
            .prefix(32)
            .map { $0 }
    }

    private func persistHomeRecommendationCache(
        generatedAt: Date,
        rankingVersion: String
    ) {
        guard let accountID = activeReadingAccountID else { return }
        let key = Self.accountReadingKey(accountID)
        var accountState = snapshot.accountReadingStates[key] ?? .empty
        if homeRecommendationItems.isEmpty {
            accountState.homeRecommendationCache = nil
        } else {
            accountState.homeRecommendationCache = PersistedRecommendationCache(
                items: homeRecommendationItems,
                generatedAt: generatedAt,
                rankingVersion: rankingVersion,
                nextCursor: homeRecommendationCursor
            )
        }
        snapshot.accountReadingStates[key] = accountState
    }

    private func rebuildPersonalizedHomeArticles(preserving selectedID: String?) {
        let projected = projector.project(homeRecommendationItems)
        guard !projected.isEmpty else {
            homeRecommendationItems = []
            homeRecommendationCursor = nil
            if let accountID = activeReadingAccountID {
                let key = Self.accountReadingKey(accountID)
                snapshot.accountReadingStates[key]?.homeRecommendationCache = nil
            }
            applyRecommendationFallback()
            return
        }
        articles = projected
        if let selectedID,
           let index = projected.firstIndex(where: { $0.id == selectedID }) {
            selectedSavedArticle = nil
            articleIndex = index
            sceneIndex = min(
                sceneProgress[selectedID] ?? 0,
                max(projected[index].scenes.count - 1, 0)
            )
        } else {
            articleIndex = min(max(articleIndex, 0), max(projected.count - 1, 0))
        }
        restoreProgress()
    }

    private func applyRecommendationFallback() {
        recommendationState = .fallback
        if !homeRecommendationItems.isEmpty {
            rebuildPersonalizedHomeArticles(
                preserving: hasReadableContent ? article.id : snapshot.lastArticleID
            )
            currentScreen = .home
            homeFeedStatus = .idle
            homePaginationState = homeRecommendationCursor == nil ? .end : .idle
            return
        }
        if !catalogFeedItems.isEmpty {
            rebuildCatalogArticles(
                preserving: hasReadableContent ? article.id : snapshot.lastArticleID
            )
            currentScreen = .home
            homeFeedStatus = .idle
            homePaginationState = loader.hasMorePages ? .idle : .end
            return
        }
        if !nextArticles.isEmpty {
            currentScreen = .home
            homeFeedStatus = .idle
            return
        }
        currentScreen = .home
        homeFeedStatus = .failed
        homePaginationState = .failed
    }

    private func handleRecommendationFailure(
        _ error: Error,
        affectsHome: Bool = true
    ) async {
        if let accountError = error as? AccountAPIError {
            switch accountError {
            case .unauthorized:
                accountStatus = .signedOut
                await deactivateReadingNamespace(removingAccountID: nil)
                recommendationState = .fallback
                return
            case .privacyConsentRequired:
                snapshot.acceptedPrivacyPolicyVersion = nil
                snapshot.privacyConsentReceiptID = nil
                onboardingPages = [.privacyConsent]
                onboardingIndex = 0
                isPrivacyConsentSelected = false
                privacyConsentState = .idle
                currentScreen = .onboarding
                schedulePersist()
                recommendationState = .fallback
                return
            case .invalidRequest, .unavailable, .networkUnavailable,
                    .invalidResponse, .server:
                break
            }
        } else if error is AccountAuthenticationError {
            accountStatus = .signedOut
            await deactivateReadingNamespace(removingAccountID: nil)
            recommendationState = .fallback
            return
        }
        if affectsHome {
            applyRecommendationFallback()
        } else {
            recommendationState = .fallback
        }
    }

    private func startHomePagination() {
        guard paginationTask == nil, !isLoadingNewArticle else { return }
        paginationTask = Task { [weak self] in
            await self?.loadMoreHomeArticles()
            self?.paginationTask = nil
        }
    }

    private func syncOfflineCacheFromCatalog() async {
        guard settings.offlineCache else {
            cachedFeedItems = []
            snapshot.cachedFeed = []
            return
        }
        cachedFeedItems = Array(catalogFeedItems.prefix(PersistedAppSnapshot.maximumCachedArticles))
        snapshot.cachedFeed = cachedFeedItems
        await prefetchCurrentImages()
    }

    private func rebuildCatalogArticles(preserving selectedID: String?) {
        let projected = projector.project(catalogFeedItems)
        articles = projected
        if let selectedID,
           let index = projected.firstIndex(where: { $0.id == selectedID }) {
            selectedSavedArticle = nil
            articleIndex = index
            sceneIndex = min(
                sceneProgress[selectedID] ?? 0,
                max(projected[index].scenes.count - 1, 0)
            )
        } else {
            articleIndex = min(max(articleIndex, 0), max(projected.count - 1, 0))
        }
    }

    private func restoreProgress() {
        if let lastArticleID = snapshot.lastArticleID,
           let index = articles.firstIndex(where: { $0.id == lastArticleID }) {
            selectedSavedArticle = nil
            articleIndex = index
        } else if let lastArticleID = snapshot.lastArticleID,
                  let saved = savedArticles.first(where: { $0.id == lastArticleID }) {
            selectedSavedArticle = saved
        } else {
            articleIndex = min(max(articleIndex, 0), max(articles.count - 1, 0))
        }
        sceneIndex = min(sceneProgress[article.id] ?? 0, max(article.scenes.count - 1, 0))
    }

    private func storeCurrentProgress() {
        sceneProgress[article.id] = clampedSceneIndex
        snapshot.lastArticleID = article.id
        enqueueReadingSync(
            PersistedReadingDelta(
                progressChanges: [article.id: clampedSceneIndex],
                updateLastArticle: true,
                lastArticleID: article.id
            )
        )
        schedulePersist()
    }

    private func openFirstUnreadArticle(in candidates: [PrototypeArticle], after currentArticleID: String) -> Bool {
        guard let candidateIndex = firstUnreadArticleIndex(in: candidates, after: currentArticleID) else {
            return false
        }
        openArticle(at: candidateIndex)
        return true
    }

    private func firstUnreadArticleIndex(in candidates: [PrototypeArticle], after currentArticleID: String) -> Int? {
        guard !candidates.isEmpty else { return nil }
        let currentIndex = candidates.firstIndex { $0.id == currentArticleID }
        let start = currentIndex.map { ($0 + 1) % candidates.count } ?? 0
        for offset in candidates.indices {
            let index = (start + offset) % candidates.count
            let candidate = candidates[index]
            if candidate.id != currentArticleID, sceneProgress[candidate.id] == nil {
                return index
            }
        }
        return nil
    }

    private func isCompletedForHome(_ article: PrototypeArticle) -> Bool {
        guard let lastSceneIndex = article.scenes.indices.last,
              let savedIndex = sceneProgress[article.id]
        else { return false }
        return savedIndex >= lastSceneIndex
    }

    private func openArticle(at index: Int) {
        guard articles.indices.contains(index) else { return }
        clearSelectedDiscoveryArticle()
        selectedSavedArticle = nil
        articleIndex = index
        prepareCurrentArticleEntry()
        go(to: .reader)
    }

    private func entrySceneIndex(for article: PrototypeArticle) -> Int {
        let lastIndex = max(article.scenes.count - 1, 0)
        guard lastIndex > 0, let savedIndex = sceneProgress[article.id] else { return 0 }
        let normalized = min(max(savedIndex, 0), lastIndex)
        return normalized >= lastIndex ? 0 : normalized
    }

    private func prepareCurrentArticleEntry() {
        sceneIndex = entrySceneIndex(for: article)
        snapshot.lastArticleID = article.id
        enqueueReadingSync(
            PersistedReadingDelta(
                updateLastArticle: true,
                lastArticleID: article.id
            )
        )
        enqueueCurrentArticleActivity(.articleOpened)
        enqueueCurrentArticleView()
        prefetchBoundaryRecommendations(for: article.id)
        schedulePersist()
    }

    func prefetchBoundaryRecommendations(for articleID: String) {
        boundaryRecommendationTask?.cancel()
        boundaryRecommendationTask = nil
        boundaryRecommendationGeneration = nil
        boundaryRecommendationItems = []
        boundaryRecommendations = []
        guard canUsePersonalizedRecommendations,
              AccountRecommendationRequest.isSHA256(articleID),
              let accountID = activeReadingAccountID,
              let timeContext = RecommendationTimeContext(occurredAt: clock())
        else { return }

        let generation = UUID()
        boundaryRecommendationGeneration = generation
        var seen = Set<String>()
        let exclusions = ([articleID] + articles.map(\.id))
            .filter {
                AccountRecommendationRequest.isSHA256($0)
                    && seen.insert($0).inserted
            }
            .prefix(64)
            .map { $0 }
        let request = AccountRecommendationRequest(
            surface: .articleBoundary,
            contextArticleID: articleID,
            excludeArticleIDs: exclusions,
            timeContext: timeContext,
            limit: 3,
            cursor: nil
        )
        guard request.isValid else { return }

        boundaryRecommendationTask = Task { [weak self] in
            guard let self else { return }
            do {
                let page = try await self.accountService.fetchRecommendations(request)
                guard !Task.isCancelled,
                      self.boundaryRecommendationGeneration == generation,
                      self.activeReadingAccountID == accountID,
                      self.accountProfile?.id == accountID,
                      self.canUsePersonalizedRecommendations,
                      self.article.id == articleID,
                      page.surface == .articleBoundary else { return }
                var seenArticleIDs = Set<String>()
                let items = self.displayPolicy.filter(page.items).filter {
                    $0.articleID != articleID
                        && seenArticleIDs.insert($0.articleID).inserted
                }
                self.boundaryRecommendationItems = Array(items.prefix(3))
                self.boundaryRecommendations = self.projector.project(
                    self.boundaryRecommendationItems
                )
                if self.boundaryRecommendationGeneration == generation {
                    self.boundaryRecommendationTask = nil
                }
            } catch {
                guard !Task.isCancelled,
                      self.boundaryRecommendationGeneration == generation,
                      self.activeReadingAccountID == accountID else { return }
                await self.handleRecommendationFailure(error, affectsHome: false)
                if self.boundaryRecommendationGeneration == generation {
                    self.boundaryRecommendationTask = nil
                }
            }
        }
    }

    private func prepareReaderGuide() {
        showReaderGuide = !snapshot.hasSeenReaderGuide
    }

    private func stableReaderOrigin(from screen: PrototypeScreen) -> PrototypeScreen? {
        switch screen {
        case .home, .explore, .library, .offline, .empty:
            return screen
        case .onboarding, .reader, .boundary, .settings, .loading, .attestError:
            return nil
        }
    }

    private func startDiscoveryRefresh(replacingCurrent: Bool = false) {
        if replacingCurrent {
            discoveryTask?.cancel()
            discoveryTask = nil
        }
        guard discoveryTask == nil else { return }
        discoveryState = .loading
        discoveryTask = Task { [weak self] in
            guard let self else { return }
            await self.refreshDiscovery()
            self.discoveryTask = nil
        }
    }

    private func applyDiscoveryLoaderState(isSearch: Bool) {
        switch discoveryLoader.state {
        case .loaded, .empty:
            guard let incoming = discoveryLoader.response else {
                if discoveryLoader.state == .empty, discoveryResponse == nil {
                    discoveryState = .empty
                } else {
                    discoveryState = .failed(message: "discovery_invalid_response", retryAt: nil)
                }
                return
            }
            let displayable = displayPolicy.filter(incoming)
            let previousCatalog = discoveryResponse
            let presentedResponse = isSearch
                ? preservingTimelineCatalog(displayable, from: previousCatalog)
                : catalogOnly(displayable)
            discoveryResponse = presentedResponse
            discoveryState = displayable.routes.isEmpty
                && displayable.results.isEmpty
                && displayable.featuredTimelines.isEmpty
                ? .empty
                : .loaded
            if isSearch {
                let resolvedQuery = displayable.query ?? discoveryQuery
                if resolvedQuery != discoveryQuery {
                    discoveryQuery = resolvedQuery
                }
            } else if !discoveryQuery.isEmpty {
                discoveryQuery = ""
            }
            reconcileSelectedCurationRoute()
            reconcileSelectedFeaturedTimeline()
            reconcileSelectedTimelineSearchResult()
            if isSearch,
               let previousCatalog,
               incoming.featuredTimelines.isEmpty {
                snapshot.cachedDiscovery = catalogOnly(previousCatalog)
            } else {
                snapshot.cachedDiscovery = catalogOnly(incoming)
            }
            schedulePersist()
        case let .failed(message, retryAt):
            discoveryState = .failed(message: message, retryAt: retryAt)
            if isSearch {
                announce(message == "discovery_cooldown"
                    ? "잠시 뒤에 다시 검색해 주세요"
                    : "검색 결과를 불러오지 못했어요")
            }
        case .idle:
            discoveryState = discoveryResponse == nil ? .idle : .loaded
        case .loading:
            discoveryState = .loading
        }
    }

    private func reconcileSelectedCurationRoute() {
        guard let response = discoveryResponse else {
            selectedCurationRouteID = nil
            return
        }
        if let selectedCurationRouteID,
           response.routes.contains(where: { $0.id == selectedCurationRouteID }) {
            return
        }
        selectedCurationRouteID = response.representativeRoute?.id
    }

    private func reconcileSelectedFeaturedTimeline() {
        guard let timelines = discoveryResponse?.featuredTimelines,
              !timelines.isEmpty else {
            selectedFeaturedTimelineKey = nil
            return
        }
        if let selectedFeaturedTimelineKey,
           timelines.contains(where: { $0.stableKey == selectedFeaturedTimelineKey }) {
            return
        }
        selectedFeaturedTimelineKey = timelines.first?.stableKey
    }

    private func reconcileSelectedTimelineSearchResult() {
        guard let selectedTimelineSearchKey else { return }
        if !timelineDiscoveryResults.contains(where: {
            $0.stableKey == selectedTimelineSearchKey
        }) {
            self.selectedTimelineSearchKey = nil
        }
    }

    private func catalogOnly(_ response: DiscoveryResponse) -> DiscoveryResponse {
        DiscoveryResponse(
            formatVersion: response.formatVersion,
            generatedAt: response.generatedAt,
            expiresAt: response.expiresAt,
            representativeRouteID: response.representativeRouteID,
            routes: Array(response.routes.prefix(8)).map { route in
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
            },
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

    private func preservingTimelineCatalog(
        _ response: DiscoveryResponse,
        from previous: DiscoveryResponse?
    ) -> DiscoveryResponse {
        guard response.featuredTimelines.isEmpty,
              let previous,
              !previous.featuredTimelines.isEmpty else { return response }
        return DiscoveryResponse(
            formatVersion: response.formatVersion,
            generatedAt: response.generatedAt,
            expiresAt: response.expiresAt,
            representativeRouteID: response.representativeRouteID,
            routes: response.routes,
            searchSuggestions: response.searchSuggestions,
            trendingSearchSuggestions: response.trendingSearchSuggestions,
            query: response.query,
            results: response.results,
            cooldownSeconds: response.cooldownSeconds,
            collection: response.collection,
            timelineStatus: previous.timelineStatus,
            timelinesGeneratedAt: previous.timelinesGeneratedAt,
            timelinesRefreshedAt: previous.timelinesRefreshedAt,
            featuredTimelines: previous.featuredTimelines,
            timelineResults: response.timelineResults,
            nextCursor: response.nextCursor
        )
    }

    private func clearSelectedDiscoveryArticle() {
        selectedDiscoveryArticle = nil
        selectedDiscoveryFeedItem = nil
        activeReadingRouteID = nil
        activeReadingTimelineKey = nil
        activeReadingCollection = nil
    }

    private func normalizedDiscoveryQuery(_ query: String) -> String {
        query
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
    }

    private func beginDiscoverySearch(query: String) -> UUID {
        let requestID = UUID()
        activeDiscoverySearchID = requestID
        discoveryQuery = query
        selectedTimelineSearchKey = nil
        discoveryState = .loading
        isDiscoverySearchLoading = true
        return requestID
    }

    private func performDiscoverySearch(_ query: String, requestID: UUID) async {
        async let minimumFeedback: Void = Self.waitForMinimumSearchFeedback()
        await discoveryLoader.search(query)
        await minimumFeedback
        finishDiscoverySearch(requestID: requestID, activityQuery: query)
    }

    private func performEditorialDiscovery(requestID: UUID) async {
        async let minimumFeedback: Void = Self.waitForMinimumSearchFeedback()
        await discoveryLoader.loadEditorials()
        await minimumFeedback
        finishDiscoverySearch(requestID: requestID)
    }

    private func performWeeklyInsightDiscovery(requestID: UUID) async {
        async let minimumFeedback: Void = Self.waitForMinimumSearchFeedback()
        await discoveryLoader.loadWeeklyInsights()
        await minimumFeedback
        finishDiscoverySearch(requestID: requestID)
    }

    private func finishDiscoverySearch(
        requestID: UUID,
        activityQuery: String? = nil
    ) {
        guard !Task.isCancelled, activeDiscoverySearchID == requestID else { return }
        applyDiscoveryLoaderState(isSearch: true)
        if let activityQuery {
            switch discoveryState {
            case .loaded, .empty:
                if discoveryLoader.response != nil {
                    enqueueAccountActivity(AccountActivityEvent.search(
                        query: activityQuery,
                        resultCount: discoveryResponse?.results.count ?? 0,
                        occurredAt: clock(),
                        timeZone: .current
                    ))
                }
            case .idle, .loading, .failed:
                break
            }
        }
        activeDiscoverySearchID = nil
        isDiscoverySearchLoading = false
    }

    private nonisolated static func waitForMinimumSearchFeedback() async {
        try? await Task.sleep(nanoseconds: 220_000_000)
    }

    private func captureReadingState() -> PersistedReadingState {
        var state = PersistedReadingState(
            savedFeed: savedFeedItems,
            savedArticleIDs: savedArticleIDs,
            savedAt: savedAt,
            sceneProgress: sceneProgress,
            lastArticleID: snapshot.lastArticleID,
            settings: settings
        )
        state.normalize()
        return state
    }

    private func applyReadingState(
        _ source: PersistedReadingState,
        applyCacheSideEffects: Bool = true
    ) {
        var state = source
        state.normalize()
        let previousOfflineCache = settings.offlineCache
        savedFeedItems = state.savedFeed
        savedArticleIDs = state.savedArticleIDs
        savedAt = state.savedAt
        sceneProgress = state.sceneProgress
        snapshot.lastArticleID = state.lastArticleID
        settings = state.settings
        rebuildSavedArticles()
        if let selectedSavedArticle,
           !savedArticleIDs.contains(selectedSavedArticle.id) {
            self.selectedSavedArticle = nil
        }
        restoreProgress()

        guard applyCacheSideEffects, previousOfflineCache != settings.offlineCache else { return }
        if settings.offlineCache {
            cachedFeedItems = Array(
                catalogFeedItems.prefix(PersistedAppSnapshot.maximumCachedArticles)
            )
            Task { [weak self] in await self?.prefetchCurrentImages() }
        } else {
            cachedFeedItems = []
            scheduleTransientMediaClear()
        }
    }

    private func activateReadingNamespace(for accountID: UUID, mergeGuest: Bool) async {
        await persistNow()
        cancelRecommendationWork()
        readingSyncTask?.cancel()
        readingSyncTask = nil
        readingSyncNeedsRerun = false
        readingNamespaceToken = UUID()
        activeReadingAccountID = accountID
        let key = Self.accountReadingKey(accountID)

        if snapshot.accountReadingStates[key] == nil {
            if mergeGuest, snapshot.guestMergedAccountID == nil {
                var state = snapshot.guestReadingState
                var savedChanges: [String: PendingSavedChange] = [:]
                for articleID in state.savedArticleIDs {
                    let savedDate = state.savedAt[articleID] ?? clock()
                    state.savedAt[articleID] = savedDate
                    savedChanges[articleID] = PendingSavedChange(
                        isSaved: true,
                        savedAt: savedDate
                    )
                }
                snapshot.accountReadingStates[key] = PersistedAccountReadingState(
                    state: state,
                    pending: PersistedReadingDelta(
                        savedChanges: savedChanges,
                        progressChanges: state.sceneProgress
                    ),
                    serverRevision: 0,
                    guestMergeNeedsServerCheck: true
                )
                snapshot.guestMergedAccountID = accountID
            } else {
                snapshot.accountReadingStates[key] = .empty
            }
        }
        if let accountState = snapshot.accountReadingStates[key]?.state {
            applyReadingState(accountState)
        }
        restoreHomeRecommendationCache(for: accountID)
        await persistNow()
        await runReadingSyncImmediately()
    }

    private func deactivateReadingNamespace(removingAccountID: UUID?) async {
        cancelRecommendationWork()
        readingSyncTask?.cancel()
        readingSyncTask = nil
        readingSyncNeedsRerun = false
        readingNamespaceToken = UUID()
        activeReadingAccountID = nil
        if let removingAccountID {
            snapshot.accountReadingStates.removeValue(
                forKey: Self.accountReadingKey(removingAccountID)
            )
        }
        homeRecommendationItems = []
        homeRecommendationCursor = nil
        boundaryRecommendationItems = []
        boundaryRecommendations = []
        recommendationState = .idle
        applyReadingState(snapshot.guestReadingState)
        if !catalogFeedItems.isEmpty {
            rebuildCatalogArticles(preserving: snapshot.guestReadingState.lastArticleID)
        } else if !cachedFeedItems.isEmpty {
            catalogFeedItems = cachedFeedItems
            rebuildCatalogArticles(preserving: snapshot.guestReadingState.lastArticleID)
        } else {
            articles = []
            articleIndex = 0
            sceneIndex = 0
        }
        await persistNow()
    }

    private func restoreHomeRecommendationCache(for accountID: UUID) {
        let key = Self.accountReadingKey(accountID)
        guard hasCurrentPrivacyConsent,
              var cache = snapshot.accountReadingStates[key]?.homeRecommendationCache,
              cache.normalize(),
              !cache.items.isEmpty else {
            homeRecommendationItems = []
            homeRecommendationCursor = nil
            recommendationState = .idle
            return
        }
        snapshot.accountReadingStates[key]?.homeRecommendationCache = cache
        homeRecommendationItems = cache.items
        homeRecommendationCursor = cache.nextCursor
        recommendationState = .loaded
        homePaginationState = cache.nextCursor == nil ? .end : .idle
        rebuildPersonalizedHomeArticles(preserving: snapshot.lastArticleID)
    }

    private func cancelRecommendationWork() {
        recommendationGeneration = nil
        boundaryRecommendationGeneration = nil
        boundaryRecommendationTask?.cancel()
        boundaryRecommendationTask = nil
    }

    private func enqueueCurrentArticleView() {
        guard hasCurrentPrivacyConsent else { return }
        let event = ArticleViewEvent(articleID: article.id)
        guard event.isValid else {
            #if DEBUG
            print("ARTICLE_VIEW_DEBUG skipped=invalid_article_id")
            #endif
            return
        }
        snapshot.pendingArticleViews.append(event)
        Task { [weak self] in
            guard let self else { return }
            await self.persistArticleViewQueue()
            self.scheduleArticleViewSync()
        }
    }

    private func scheduleArticleViewSync() {
        guard hasCurrentPrivacyConsent,
              connectivityState != .offline,
              !snapshot.pendingArticleViews.isEmpty
        else { return }
        if articleViewTask != nil {
            articleViewSyncNeedsRerun = true
            return
        }
        articleViewTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 40_000_000)
            guard !Task.isCancelled, let self else { return }
            await self.synchronizeArticleViews()
            self.finishArticleViewSync()
        }
    }

    private func runArticleViewSyncImmediately() async {
        guard hasCurrentPrivacyConsent,
              connectivityState != .offline,
              !snapshot.pendingArticleViews.isEmpty
        else { return }
        if let task = articleViewTask {
            await task.value
            return
        }
        let task = Task { [weak self] in
            guard let self else { return }
            await self.synchronizeArticleViews()
            self.finishArticleViewSync()
        }
        articleViewTask = task
        await task.value
    }

    private func finishArticleViewSync() {
        articleViewTask = nil
        if articleViewSyncNeedsRerun {
            articleViewSyncNeedsRerun = false
            scheduleArticleViewSync()
        }
    }

    private func synchronizeArticleViews() async {
        guard hasCurrentPrivacyConsent,
              connectivityState != .offline,
              let sent = snapshot.pendingArticleViews.first
        else { return }
        await persistArticleViewQueue()
        do {
            let response = try await articleViewService.record(sent)
            guard response.articleID == sent.articleID,
                  response.viewCount >= 0
            else {
                throw ArticleFeedError.invalidResponse
            }
            await acknowledgeArticleView(sent)
        } catch let ArticleFeedError.server(code, _)
            where code.lowercased() == "article_not_found" {
            await acknowledgeArticleView(sent)
        } catch {
            // Keep the original UUID queued for a later lifecycle retry.
        }
    }

    private func acknowledgeArticleView(_ sent: ArticleViewEvent) async {
        snapshot.pendingArticleViews.removeAll { $0.eventID == sent.eventID }
        if !snapshot.pendingArticleViews.isEmpty {
            articleViewSyncNeedsRerun = true
        }
        await persistArticleViewQueue()
    }

    private func persistArticleViewQueue() async {
        do {
            try await persistence.save(snapshot)
        } catch {
            // View accounting is best-effort and must never surface over Reader UI.
        }
    }

    private func enqueueReadingSync(_ delta: PersistedReadingDelta) {
        guard let accountID = activeReadingAccountID else { return }
        let key = Self.accountReadingKey(accountID)
        var accountState = snapshot.accountReadingStates[key] ?? .empty
        accountState.state = captureReadingState()
        accountState.pending.merge(delta)
        snapshot.accountReadingStates[key] = accountState
        scheduleReadingSync()
    }

    private func enqueueAccountActivity(_ event: AccountActivityEvent?) {
        guard let event, let accountID = activeReadingAccountID else { return }
        let key = Self.accountReadingKey(accountID)
        var accountState = snapshot.accountReadingStates[key] ?? .empty
        accountState.pendingActivityEvents.append(event)
        snapshot.accountReadingStates[key] = accountState
        schedulePersist()
        scheduleReadingSync()
    }

    private func enqueueCurrentArticleActivity(_ type: AccountActivityEventType) {
        enqueueAccountActivity(AccountActivityEvent.article(
            type,
            articleID: article.id,
            sceneIndex: clampedSceneIndex,
            sceneCount: article.scenes.count,
            occurredAt: clock(),
            timeZone: .current
        ))
    }

    private func scheduleReadingSync() {
        guard activeReadingAccountID != nil, connectivityState != .offline else { return }
        if readingSyncTask != nil {
            readingSyncNeedsRerun = true
            return
        }
        let token = readingNamespaceToken
        readingSyncTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 40_000_000)
            guard !Task.isCancelled else { return }
            await self?.synchronizeReadingState(expectedToken: token)
            self?.finishReadingSync(expectedToken: token)
        }
    }

    private func runReadingSyncImmediately() async {
        guard activeReadingAccountID != nil else { return }
        if let task = readingSyncTask {
            readingSyncNeedsRerun = true
            await task.value
            return
        }
        let token = readingNamespaceToken
        let task = Task { [weak self] in
            guard let self else { return }
            await self.synchronizeReadingState(expectedToken: token)
            self.finishReadingSync(expectedToken: token)
        }
        readingSyncTask = task
        await task.value
    }

    private func finishReadingSync(expectedToken: UUID) {
        guard expectedToken == readingNamespaceToken else { return }
        readingSyncTask = nil
        if readingSyncNeedsRerun {
            readingSyncNeedsRerun = false
            scheduleReadingSync()
        }
    }

    private func synchronizeReadingState(expectedToken: UUID? = nil) async {
        guard let accountID = activeReadingAccountID else { return }
        let token = expectedToken ?? readingNamespaceToken
        guard token == readingNamespaceToken else { return }
        let key = Self.accountReadingKey(accountID)

        do {
            let remote = try await accountService.fetchReadingState()
            guard token == readingNamespaceToken,
                  activeReadingAccountID == accountID else { return }

            if var accountState = snapshot.accountReadingStates[key],
               accountState.guestMergeNeedsServerCheck {
                let serverWasEmpty = remote.revision == 0
                    && remote.savedArticles.isEmpty
                    && remote.sceneProgress.isEmpty
                    && remote.lastArticleID == nil
                if serverWasEmpty {
                    accountState.pending.settings = accountState.state.settings
                    accountState.pending.updateLastArticle = true
                    accountState.pending.lastArticleID = accountState.state.lastArticleID
                }
                accountState.guestMergeNeedsServerCheck = false
                snapshot.accountReadingStates[key] = accountState
            }
            applyServerReadingState(remote, accountID: accountID, acknowledging: nil)
            await persistNow()

            if token == readingNamespaceToken,
               activeReadingAccountID == accountID,
               let pending = snapshot.accountReadingStates[key]?.pending,
               !pending.isEmpty {
                let patch = Self.accountReadingPatch(from: pending)
                if patch.isValid, !patch.isEmpty {
                    let patched = try await accountService.patchReadingState(patch)
                    guard token == readingNamespaceToken,
                          activeReadingAccountID == accountID else { return }
                    applyServerReadingState(
                        patched,
                        accountID: accountID,
                        acknowledging: pending
                    )
                    await persistNow()
                }
            }
        } catch let error as AccountAPIError where error == .unauthorized {
            guard token == readingNamespaceToken,
                  activeReadingAccountID == accountID else { return }
            accountStatus = .signedOut
            editorialPushState = .signedOut
            await deactivateReadingNamespace(removingAccountID: nil)
            return
        } catch {
            // Local state and pending deltas remain authoritative until a later retry.
        }

        guard token == readingNamespaceToken,
              activeReadingAccountID == accountID,
              let queued = snapshot.accountReadingStates[key]?.pendingActivityEvents,
              !queued.isEmpty else { return }
        let sent = Array(queued.prefix(100))
        do {
            let response = try await accountService.submitActivityEvents(sent)
            guard response.accepted >= 0,
                  response.duplicates >= 0,
                  response.accepted + response.duplicates == sent.count
            else {
                throw AccountAPIError.invalidResponse
            }
            guard token == readingNamespaceToken,
                  activeReadingAccountID == accountID,
                  var accountState = snapshot.accountReadingStates[key]
            else { return }
            let sentIDs = Set(sent.map(\.eventID))
            accountState.pendingActivityEvents.removeAll {
                sentIDs.contains($0.eventID)
            }
            let hasMore = !accountState.pendingActivityEvents.isEmpty
            snapshot.accountReadingStates[key] = accountState
            if hasMore { readingSyncNeedsRerun = true }
            await persistNow()
        } catch let error as AccountAPIError where error == .unauthorized {
            guard token == readingNamespaceToken,
                  activeReadingAccountID == accountID else { return }
            accountStatus = .signedOut
            editorialPushState = .signedOut
            await deactivateReadingNamespace(removingAccountID: nil)
        } catch {
            // Activity rows remain queued with their original UUIDs for retry.
        }
    }

    private func applyServerReadingState(
        _ remote: AccountReadingState,
        accountID: UUID,
        acknowledging sent: PersistedReadingDelta?
    ) {
        let key = Self.accountReadingKey(accountID)
        guard var accountState = snapshot.accountReadingStates[key] else { return }
        let local = accountState.state
        if let sent { accountState.pending.acknowledge(sent) }
        let pending = accountState.pending
        var merged = Self.persistedReadingState(from: remote)

        for (articleID, change) in pending.savedChanges {
            if change.isSaved {
                let localArticle = local.savedFeed.first { $0.articleID == articleID }
                let remoteArticle = merged.savedFeed.first { $0.articleID == articleID }
                if let article = localArticle ?? remoteArticle {
                    merged.savedFeed.removeAll { $0.articleID == articleID }
                    merged.savedFeed.append(article)
                    merged.savedArticleIDs.insert(articleID)
                    merged.savedAt[articleID] = change.savedAt
                        ?? local.savedAt[articleID]
                        ?? clock()
                }
            } else {
                merged.savedFeed.removeAll { $0.articleID == articleID }
                merged.savedArticleIDs.remove(articleID)
                merged.savedAt.removeValue(forKey: articleID)
            }
        }
        for (articleID, sceneIndex) in pending.progressChanges {
            merged.sceneProgress[articleID] = sceneIndex
        }
        if let settings = pending.settings { merged.settings = settings }
        if pending.updateLastArticle { merged.lastArticleID = pending.lastArticleID }
        merged.normalize()

        accountState.state = merged
        accountState.serverRevision = remote.revision
        snapshot.accountReadingStates[key] = accountState
        if activeReadingAccountID == accountID {
            applyReadingState(merged)
        }
    }

    private static func persistedReadingState(
        from remote: AccountReadingState
    ) -> PersistedReadingState {
        var state = PersistedReadingState(
            savedFeed: remote.savedArticles.map(\.article),
            savedArticleIDs: Set(remote.savedArticles.map(\.article.articleID)),
            savedAt: Dictionary(
                uniqueKeysWithValues: remote.savedArticles.map {
                    ($0.article.articleID, $0.savedAt)
                }
            ),
            sceneProgress: remote.sceneProgress,
            lastArticleID: remote.lastArticleID,
            settings: remote.settings
        )
        state.normalize()
        return state
    }

    private static func accountReadingPatch(
        from pending: PersistedReadingDelta
    ) -> AccountReadingPatch {
        AccountReadingPatch(
            savedChanges: pending.savedChanges.sorted { $0.key < $1.key }.map {
                AccountSavedChange(
                    articleID: $0.key,
                    saved: $0.value.isSaved,
                    savedAt: $0.value.savedAt
                )
            },
            progressChanges: pending.progressChanges.sorted { $0.key < $1.key }.map {
                AccountProgressChange(articleID: $0.key, sceneIndex: $0.value)
            },
            settings: pending.settings.map(AccountReaderSettingsWire.init),
            updateLastArticle: pending.updateLastArticle,
            lastArticleID: pending.lastArticleID
        )
    }

    private nonisolated static func accountReadingKey(_ accountID: UUID) -> String {
        accountID.uuidString.lowercased()
    }

    private func settingsDidChange() {
        snapshot.settings = settings
        enqueueReadingSync(PersistedReadingDelta(settings: settings))
        schedulePersist()
    }

    private func schedulePersist() {
        persistTask?.cancel()
        persistTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 40_000_000)
            guard !Task.isCancelled else { return }
            await self?.persistNow()
        }
    }

    private func persistNow() async {
        snapshot.savedArticleIDs = savedArticleIDs
        snapshot.savedAt = savedAt
        snapshot.savedFeed = savedFeedItems
        snapshot.sceneProgress = sceneProgress
        snapshot.settings = settings
        snapshot.lastCheckedAt = lastCheckedAt
        snapshot.cachedFeed = settings.offlineCache ? cachedFeedItems : []
        snapshot.cachedDiscovery = discoveryResponse.map(catalogOnly)
        let readingState = captureReadingState()
        if let accountID = activeReadingAccountID {
            let key = Self.accountReadingKey(accountID)
            var accountState = snapshot.accountReadingStates[key] ?? .empty
            accountState.state = readingState
            snapshot.accountReadingStates[key] = accountState
        } else {
            snapshot.guestReadingState = readingState
        }
        do {
            try await persistence.save(snapshot)
            await updateCacheStats()
        } catch {
            announce("기기 저장 공간에 변경을 남기지 못했어요")
        }
    }

    private func prefetchCurrentImages() async {
        let urls = cachedFeedItems.flatMap { $0.map.images.map(\.url) }
        await mediaCache.prefetch(urls)
        await updateCacheStats()
    }

    private func scheduleCompletedArticleMediaCleanup(articleID: String) {
        let previousTask = mediaCleanupTask
        let cache = mediaCache
        mediaCleanupTask = Task { [weak self] in
            await previousTask?.value
            guard !Task.isCancelled else { return }
            await cache.releaseThumbnails(for: articleID)
            await self?.updateCacheStats()
        }
    }

    private func scheduleTransientMediaClear() {
        let previousTask = mediaCleanupTask
        let cache = mediaCache
        mediaCleanupTask = Task { [weak self] in
            await previousTask?.value
            guard !Task.isCancelled else { return }
            await cache.clearTransient()
            await self?.updateCacheStats()
        }
    }

    private func updateCacheStats() async {
        let fileBytes = await persistence.storedByteCount()
        let mediaBytes = await mediaCache.diskUsage()
        cacheStats = CacheStats(
            articleCount: snapshot.cachedFeed.count,
            imageCount: snapshot.cachedFeed.flatMap { $0.map.images }.count,
            byteCount: fileBytes + mediaBytes
        )
    }

    private func rawFeedItem(for articleID: String) -> ArticleFeedItem? {
        if selectedDiscoveryFeedItem?.articleID == articleID {
            return selectedDiscoveryFeedItem
        }
        if let discoveryItem = discoveryResponse?.routes.lazy
            .flatMap(\.items)
            .first(where: { $0.article.articleID == articleID })?.article {
            return discoveryItem
        }
        if let item = loader.items.first(where: { $0.articleID == articleID }) {
            return item
        }
        if let item = boundaryRecommendationItems.first(where: {
            $0.articleID == articleID
        }) {
            return item
        }
        if let item = homeRecommendationItems.first(where: {
            $0.articleID == articleID
        }) {
            return item
        }
        if let item = catalogFeedItems.first(where: { $0.articleID == articleID }) {
            return item
        }
        if let item = cachedFeedItems.first(where: { $0.articleID == articleID }) {
            return item
        }
        return savedFeedItems.first { $0.articleID == articleID }
    }

    private func rebuildSavedArticles() {
        savedArticles = projector.project(savedFeedItems).sorted {
            (savedAt[$0.id] ?? .distantPast) > (savedAt[$1.id] ?? .distantPast)
        }
    }

    private func trimSavedFeed() {
        guard savedFeedItems.count > PersistedAppSnapshot.maximumSavedArticles else { return }
        let overflow = savedFeedItems.count - PersistedAppSnapshot.maximumSavedArticles
        let removed = savedFeedItems.prefix(overflow).map(\.articleID)
        savedFeedItems.removeFirst(overflow)
        removed.forEach {
            savedArticleIDs.remove($0)
            savedAt.removeValue(forKey: $0)
            sceneProgress.removeValue(forKey: $0)
        }
    }

    /// Server-side App Attest verification/protocol error codes that do not
    /// contain the substrings below but are nonetheless attestation failures,
    /// not connectivity problems. Without this set they leak to the offline
    /// screen, which hides the real support code from the user and from us.
    private static let attestFailureCodes: Set<String> = [
        "app_attest_not_configured",
        "invalid_or_expired_challenge",
        "attestation_registration_conflict",
        "assertion_replay_or_race",
        "attested_key_not_active",
        "request_time_out_of_range",
        "invalid_client_identity",
        "invalid_challenge_purpose",
        "invalid_device",
        // AppAttestVerificationError codes (see server app_attest.py)
        "invalid_size", "invalid_format", "invalid_cbor",
        "invalid_attestation_statement", "invalid_public_key",
        "nonce_extension_missing", "nonce_mismatch", "key_id_mismatch",
        "invalid_counter", "environment_mismatch", "credential_id_mismatch",
        "counter_replay", "invalid_signature", "rp_id_mismatch",
        "extended_attributes_missing", "validation_category_mismatch",
        "bundle_version_mismatch", "invalid_client_data", "invalid_challenge",
        "invalid_chain", "invalid_authenticator_data", "invalid_key_id",
        "invalid_nonce_extension", "invalid_extended_attributes",
    ]

    private static func isAttestFailure(_ code: String) -> Bool {
        let normalized = code.lowercased()
        if attestFailureCodes.contains(normalized) { return true }
        return normalized.contains("attest")
            || normalized.contains("assertion")
            || normalized.contains("challenge")
            || normalized.contains("registration")
            || normalized == "appattestunsupported"
            || normalized == "attestationunavailable"
    }

    private static func accountMessage(for error: Error) -> String {
        if let error = error as? AccountAPIError { return error.userMessage }
        if let error = error as? AccountAuthenticationError { return error.userMessage }
        return "계정 요청을 완료하지 못했어요. 잠시 뒤 다시 시도해 주세요."
    }

    private static func privacyConsentMessage(for error: Error) -> String {
        if let error = error as? ArticleFeedError {
            switch error {
            case .appAttestUnsupported:
                return "이 기기에서는 안전한 동의 기록을 만들 수 없어요."
            case .server(code: "privacy_policy_version_outdated", retryAfter: _):
                return "개인정보 처리방침이 변경됐어요. 앱을 업데이트한 뒤 다시 시도해 주세요."
            default:
                break
            }
        }
        return "동의 기록을 서버에 남기지 못했어요. 연결을 확인하고 다시 시도해 주세요."
    }

    private static func articleFeedbackMessage(for error: Error) -> String {
        if let error = error as? AccountAuthenticationError, error == .noSession {
            return "로그인 후 의견을 보낼 수 있어요. 설정에서 로그인할 수 있습니다."
        }
        if let error = error as? AccountAPIError, error == .networkUnavailable {
            return "인터넷 연결을 확인한 뒤 다시 시도해 주세요."
        }
        return "의견을 보내지 못했어요. 잠시 뒤 다시 시도해 주세요."
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}

typealias PrototypeStore = TextShortsStore

private extension Array {
    subscript(safe index: Index) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
