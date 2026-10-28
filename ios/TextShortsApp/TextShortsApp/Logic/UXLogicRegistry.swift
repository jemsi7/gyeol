import Foundation

enum UXLogicEffect: String, Hashable {
    case presentation
    case navigation
    case persistence
    case network
    case media
    case platform
    case accessibility
}

struct UXLogicContract: Identifiable, Equatable {
    let id: UXID
    let owner: String
    let key: String
    let effects: Set<UXLogicEffect>
}

enum UXLogicRegistry {
    static let all: [UXLogicContract] = [
        c(.ux001, "OnboardingScreen", "app.identity", [.presentation]),
        c(.ux002, "OnboardingScreen", "onboarding.valuePromise", [.presentation, .accessibility]),
        c(.ux004, "OnboardingScreen", "onboarding.gestureModel", [.presentation, .accessibility]),
        c(.ux006, "TextShortsStore", "onboarding.complete", [.network, .persistence, .navigation]),
        c(.ux007, "OnboardingScreen", "accessibility.navigationPolicy", [.accessibility]),
        c(.ux091, "OnboardingScreen", "onboarding.stepProgress", [.presentation, .accessibility]),
        c(.ux092, "OnboardingScreen", "onboarding.stepNavigation", [.navigation, .presentation, .accessibility]),
        c(.ux093, "TextShortsStore", "onboarding.skip", [.network, .persistence, .navigation]),
        c(.ux094, "OnboardingScreen", "onboarding.coreConcepts", [.presentation, .accessibility]),
        c(.ux104, "OnboardingScreen", "onboarding.releaseHighlights", [.presentation, .accessibility]),
        c(.ux105, "TextShortsStore", "account.signInFromOnboarding", [.platform, .network, .persistence, .navigation]),
        c(.ux106, "TextShortsStore", "onboarding.continueAsGuest", [.network, .persistence, .navigation]),
        c(.ux127, "OnboardingScreen", "privacy.summary", [.presentation, .accessibility]),
        c(.ux128, "OnboardingScreen", "privacy.openPolicy", [.platform, .accessibility]),
        c(.ux129, "TextShortsStore", "privacy.selectConsent", [.presentation, .accessibility]),
        c(.ux130, "TextShortsStore", "privacy.recordConsent", [.network, .persistence, .navigation, .accessibility]),

        c(.ux095, "HomeScreen", "home.identity", [.presentation, .accessibility]),
        c(.ux096, "HomeScreen", "home.leadArticle", [.presentation, .media, .navigation]),
        c(.ux097, "TextShortsStore", "home.articleRows", [.presentation, .navigation, .persistence]),
        c(.ux098, "TextShortsStore", "home.continuousPagination", [.network, .persistence, .presentation]),
        c(.ux099, "TextShortsStore", "navigation.libraryTab", [.navigation]),
        c(.ux100, "TextShortsStore", "navigation.settingsTab", [.navigation]),
        c(.ux101, "TextShortsStore", "navigation.homeTab", [.navigation, .persistence]),
        c(.ux124, "TextShortsStore", "navigation.readerOriginFromBoundary", [.navigation, .persistence]),
        c(.ux102, "TextShortsStore", "navigation.readerTab", [.navigation, .persistence]),
        c(.ux103, "HomeScreen", "home.articleCatalog", [.presentation, .accessibility]),

        c(.ux113, "ExploreScreen", "discovery.identity", [.presentation, .accessibility]),
        c(.ux114, "TextShortsStore", "discovery.search", [.network, .presentation, .accessibility]),
        c(.ux115, "ExploreScreen", "discovery.suggestions", [.presentation, .accessibility]),
        c(.ux119, "ExploreScreen", "discovery.searchResults", [.presentation, .accessibility]),
        c(.ux120, "TextShortsStore", "discovery.stateAndRetry", [.network, .presentation]),

        c(.ux008, "ReaderScreen", "reader.focusContent", [.accessibility]),
        c(.ux009, "ReaderScreen", "reader.homeAffordance", [.presentation, .accessibility]),
        c(.ux010, "TextShortsStore", "reader.toggleContext", [.presentation]),
        c(.ux011, "TextShortsStore", "reader.sceneProgress", [.presentation, .persistence]),
        c(.ux012, "TextShortsStore", "library.toggleBookmark", [.persistence]),
        c(.ux013, "TextShortsStore", "reader.toggleOverflow", [.presentation]),
        c(.ux014, "ReaderScreen", "reader.contextPresentation", [.presentation]),
        c(.ux015, "ArticleProjector", "article.sourceMetadata", [.presentation, .network]),
        c(.ux016, "TextShortsStore", "reader.jumpScene", [.navigation, .persistence]),
        c(.ux017, "TextShortsStore", "platform.openOriginalFromRail", [.platform]),
        c(.ux018, "TextShortsStore", "navigation.libraryFromRail", [.navigation]),
        c(.ux019, "TextShortsStore", "navigation.settingsFromRail", [.navigation]),
        c(.ux020, "ArticleProjector", "article.sceneHeadline", [.presentation, .network]),
        c(.ux021, "ArticleProjector", "article.sceneSummary", [.presentation, .network]),
        c(.ux022, "ArticleProjector", "article.metadata", [.presentation, .network]),
        c(.ux023, "ArticleMediaCache", "media.loadImage", [.media, .network, .persistence]),
        c(.ux024, "ArticleProjector", "media.imageCaption", [.presentation, .media]),
        c(.ux025, "ArticleProjector", "article.sceneBody", [.presentation, .network]),
        c(.ux026, "ArticleProjector", "article.sceneQuote", [.presentation, .network]),
        c(.ux027, "TextShortsStore", "reader.previousScene", [.navigation, .persistence]),
        c(.ux028, "TextShortsStore", "reader.nextScene", [.navigation, .persistence]),
        c(.ux029, "ReaderScreen", "reader.gestureNavigation", [.navigation, .accessibility]),
        c(.ux030, "TextShortsStore", "feed.offlineIndicator", [.presentation, .persistence]),
        c(.ux031, "TextShortsStore", "reader.overflowPresentation", [.presentation]),
        c(.ux032, "TextShortsStore", "platform.shareArticle", [.platform]),
        c(.ux033, "TextShortsStore", "platform.openOriginal", [.platform]),
        c(.ux034, "TextShortsStore", "navigation.libraryFromMenu", [.navigation]),
        c(.ux035, "TextShortsStore", "navigation.settingsFromMenu", [.navigation]),
        c(.ux036, "TextShortsStore", "feedback.toast", [.presentation, .accessibility]),
        c(.ux090, "TextShortsStore", "feed.requestUnreadOrRefresh", [.network, .navigation, .persistence, .presentation]),

        c(.ux037, "TextShortsStore", "boundary.completionState", [.presentation, .persistence]),
        c(.ux038, "TextShortsStore", "boundary.currentHeadline", [.presentation]),
        c(.ux039, "TextShortsStore", "boundary.fullProgress", [.presentation]),
        c(.ux040, "ArticleProjector", "boundary.keywords", [.presentation]),
        c(.ux041, "ArticleMediaCache", "boundary.firstNextMedia", [.media, .persistence]),
        c(.ux042, "TextShortsStore", "boundary.firstNextSource", [.presentation]),
        c(.ux043, "TextShortsStore", "boundary.firstNextHeadline", [.presentation]),
        c(.ux044, "TextShortsStore", "boundary.openNextArticle", [.navigation, .persistence]),
        c(.ux045, "TextShortsStore", "boundary.returnLastScene", [.navigation, .persistence]),
        c(.ux046, "TextShortsStore", "boundary.toggleBookmark", [.persistence]),

        c(.ux047, "TextShortsStore", "navigation.homeFromLibrary", [.navigation, .persistence]),
        c(.ux048, "TextShortsStore", "library.screenState", [.presentation]),
        c(.ux049, "TextShortsStore", "library.savedCount", [.presentation, .persistence]),
        c(.ux050, "TextShortsStore", "library.savedArticles", [.presentation, .persistence]),
        c(.ux051, "ArticleMediaCache", "library.thumbnail", [.media, .persistence]),
        c(.ux052, "TextShortsStore", "library.savedMetadata", [.presentation, .persistence]),
        c(.ux053, "TextShortsStore", "library.savedHeadline", [.presentation]),
        c(.ux054, "TextShortsStore", "library.openSaved", [.navigation, .persistence]),
        c(.ux055, "TextShortsStore", "library.removeSaved", [.persistence]),
        c(.ux056, "TextShortsStore", "library.emptyState", [.presentation, .navigation]),
        c(.ux057, "TextShortsStore", "navigation.settingsFromLibrary", [.navigation]),

        c(.ux058, "TextShortsStore", "navigation.settingsBack", [.navigation]),
        c(.ux059, "TextShortsStore", "settings.screenState", [.presentation, .persistence]),
        c(.ux060, "TextShortsStore", "settings.typeSize", [.persistence, .presentation]),
        c(.ux061, "TextShortsStore", "settings.leading", [.persistence, .presentation]),
        c(.ux062, "TextShortsStore", "settings.theme", [.persistence, .presentation]),
        c(.ux063, "TextShortsStore", "settings.mediaSaver", [.persistence, .media]),
        c(.ux064, "TextShortsStore", "settings.offlineCache", [.persistence, .media]),
        c(.ux065, "TextShortsStore", "settings.cacheUsage", [.presentation, .persistence]),
        c(.ux066, "TextShortsStore", "settings.reset", [.persistence, .media]),
        c(.ux067, "TextShortsStore", "settings.done", [.navigation, .persistence]),
        c(.ux107, "TextShortsStore", "settings.accountState", [.presentation, .network, .persistence]),
        c(.ux108, "TextShortsStore", "account.signInFromSettings", [.platform, .network, .persistence]),
        c(.ux109, "TextShortsStore", "account.signOut", [.network, .persistence, .presentation]),
        c(.ux110, "SettingsScreen", "account.deleteDisclosure", [.presentation, .accessibility]),
        c(.ux111, "TextShortsStore", "account.delete", [.network, .persistence, .presentation]),
        c(.ux112, "TextShortsStore", "account.feedback", [.presentation, .accessibility]),
        c(.ux121, "SettingsScreen", "settings.openReadingPreferences", [.navigation, .presentation, .accessibility]),
        c(.ux122, "SettingsScreen", "settings.closeReadingPreferences", [.navigation, .presentation, .accessibility]),
        c(.ux123, "TextShortsStore", "settings.editorialPushSwitch", [.platform, .network, .persistence, .presentation, .accessibility]),
        c(.ux131, "TextShortsStore", "settings.weeklyInsightPushSwitch", [.platform, .network, .persistence, .presentation, .accessibility]),
        c(.ux132, "TextShortsStore", "settings.pushRegistrationRepair", [.platform, .network, .presentation, .accessibility]),
        c(.ux125, "SettingsScreen", "settings.openNotificationPreferences", [.navigation, .presentation, .accessibility]),
        c(.ux126, "SettingsScreen", "settings.closeNotificationPreferences", [.navigation, .presentation, .accessibility]),
        c(.ux133, "SettingsScreen", "settings.openTimelinePreferences", [.navigation, .presentation, .accessibility]),
        c(.ux134, "SettingsScreen", "settings.closeTimelinePreferences", [.navigation, .presentation, .accessibility]),
        c(.ux135, "TextShortsStore", "settings.timelineRefreshPushSwitch", [.platform, .network, .persistence, .presentation, .accessibility]),
        c(.ux136, "TextShortsStore", "settings.openTimelineSubscriptions", [.navigation, .network, .presentation, .accessibility]),
        c(.ux137, "TextShortsStore", "settings.openTimelineHistory", [.navigation, .network, .presentation, .accessibility]),
        c(.ux138, "SettingsScreen", "settings.closeTimelineManagement", [.navigation, .presentation, .accessibility]),
        c(.ux139, "TextShortsStore", "settings.timelineAdditionPushSwitch", [.platform, .network, .persistence, .presentation, .accessibility]),

        c(.ux068, "ArticleFeedLoader", "feed.loadingState", [.network, .presentation]),
        c(.ux069, "LoadingScreen", "feed.loadingHeadlineShape", [.presentation]),
        c(.ux070, "LoadingScreen", "feed.loadingMediaShape", [.presentation, .media]),
        c(.ux071, "LoadingScreen", "feed.loadingBodyShape", [.presentation, .accessibility]),
        c(.ux072, "TextShortsStore", "feed.emptySymbol", [.presentation, .network]),
        c(.ux073, "TextShortsStore", "feed.emptyTitle", [.presentation, .network]),
        c(.ux074, "TextShortsStore", "feed.lastCheckedAt", [.presentation, .persistence]),
        c(.ux075, "TextShortsStore", "feed.retryEmpty", [.network, .navigation]),
        c(.ux076, "TextShortsStore", "navigation.libraryFromEmpty", [.navigation, .persistence]),
        c(.ux077, "TextShortsStore", "feed.offlineSymbol", [.presentation, .persistence]),
        c(.ux078, "TextShortsStore", "feed.offlineTitle", [.presentation, .persistence]),
        c(.ux079, "TextShortsStore", "feed.cachedArticleCount", [.presentation, .persistence]),
        c(.ux080, "TextShortsStore", "feed.resumeCached", [.navigation, .persistence]),
        c(.ux081, "TextShortsStore", "feed.retryOffline", [.network, .navigation]),
        c(.ux082, "TextShortsStore", "navigation.dismissOffline", [.navigation]),
        c(.ux083, "TextShortsStore", "attest.errorSymbol", [.presentation, .network]),
        c(.ux084, "TextShortsStore", "attest.errorTitle", [.presentation, .network]),
        c(.ux085, "TextShortsStore", "attest.errorMessage", [.presentation, .network]),
        c(.ux086, "TextShortsStore", "attest.retry", [.network, .navigation]),
        c(.ux087, "TextShortsStore", "attest.detailsDisclosure", [.presentation]),
        c(.ux088, "TextShortsStore", "attest.supportCode", [.presentation, .network]),
        c(.ux089, "TextShortsStore", "navigation.settingsFromAttest", [.navigation]),
    ]

    static func contract(for id: UXID) -> UXLogicContract? {
        all.first { $0.id == id }
    }

    private static func c(
        _ id: UXID,
        _ owner: String,
        _ key: String,
        _ effects: Set<UXLogicEffect>
    ) -> UXLogicContract {
        UXLogicContract(id: id, owner: owner, key: key, effects: effects)
    }
}
