import SwiftUI

@main
struct TextShortsApp: App {
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var pushAppDelegate
    @StateObject private var store = PrototypeStore()

    var body: some Scene {
        WindowGroup {
            RootPrototypeView()
                .environmentObject(store)
                .preferredColorScheme(store.settings.theme.colorScheme)
        }
    }
}

struct RootPrototypeView: View {
    @EnvironmentObject private var store: PrototypeStore
    @ObservedObject private var pushBridge = PushNotificationBridge.shared
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        GeometryReader { proxy in
            let mode = LayoutMode(width: proxy.size.width)
            let palette = AppPalette(colorScheme)
            let motionEnabled = store.settings.motionEnabled && !reduceMotion
            ZStack {
                palette.paper.ignoresSafeArea()
                VStack(spacing: 0) {
                    screen(mode: mode, palette: palette)
                        .id(store.currentScreen)
                        .transition(motionEnabled ? .opacity.combined(with: .offset(y: 6)) : .identity)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .clipped()
                        .safeAreaInset(edge: .bottom, spacing: 0) {
                            if let selectedTab = store.currentScreen.primaryTab {
                                PrimaryBottomNavigation(
                                    selected: selectedTab,
                                    showAnnotations: store.showAnnotations,
                                    palette: palette,
                                    onSelect: store.selectPrimaryTab
                                )
                            }
                        }
                    if store.showPrototypeTools {
                        PrototypeDock(mode: mode, width: proxy.size.width, palette: palette)
                            .frame(height: AppMetrics.dockHeight)
                    }
                }
                .animation(motionEnabled ? SwissMotion.easeOut() : nil, value: store.currentScreen)
                .environment(\.appMotionEnabled, motionEnabled)
            }
        }
        .ignoresSafeArea(.keyboard)
        .task {
            await store.bootstrap()
            await handleInitialPushEvents()
        }
        .task {
            for await state in store.connectivityUpdates() {
                await store.handleConnectivity(state)
            }
        }
        .onChange(of: scenePhase, perform: handleScenePhaseChange)
        .onChange(of: store.pendingExternalURL, perform: openPendingExternalURL)
        .onChange(of: pushBridge.deviceTokenRevision) { _ in
            guard let token = pushBridge.latestDeviceToken else { return }
            Task { await store.handlePushDeviceToken(token) }
        }
        .onChange(of: pushBridge.pendingDestination) { _ in
            guard let destination = pushBridge.consumeDestination() else { return }
            Task { await handlePushDestination(destination) }
        }
        .sheet(item: $store.sharePayload) { payload in
            ActivityShareSheet(items: [payload.headline, payload.url])
        }
    }

    private func handleScenePhaseChange(_ phase: ScenePhase) {
        Task { await store.handleScenePhase(phase) }
    }

    private func openPendingExternalURL(_ url: URL?) {
        guard let url else { return }
        store.consumeExternalURL()
        openURL(url) { accepted in
            store.handleOpenURLResult(wasDiscarded: !accepted)
        }
    }

    private func handleInitialPushEvents() async {
        if let token = pushBridge.latestDeviceToken {
            await store.handlePushDeviceToken(token)
        }
        if let destination = pushBridge.consumeDestination() {
            await handlePushDestination(destination)
        }
    }

    private func handlePushDestination(_ destination: PendingPushDestination) async {
        switch destination {
        case let .article(pending):
            await store.handleEditorialNotification(
                articleID: pending.articleID,
                collection: pending.collection
            )
        case let .timeline(pending):
            await store.handleTimelineNotification(pending)
        }
    }

    @ViewBuilder
    private func screen(mode: LayoutMode, palette: AppPalette) -> some View {
        switch store.currentScreen {
        case .onboarding: OnboardingScreen(mode: mode, palette: palette)
        case .home: HomeScreen(mode: mode, palette: palette)
        case .explore: ExploreScreen(mode: mode, palette: palette)
        case .reader: ReaderScreen(mode: mode, palette: palette)
        case .boundary: ArticleBoundaryScreen(mode: mode, palette: palette)
        case .library: LibraryScreen(mode: mode, palette: palette)
        case .settings: SettingsScreen(mode: mode, palette: palette)
        case .loading: LoadingScreen(mode: mode, palette: palette)
        case .empty: EmptyFeedScreen(mode: mode, palette: palette)
        case .offline: OfflineScreen(mode: mode, palette: palette)
        case .attestError: AttestErrorScreen(mode: mode, palette: palette)
        }
    }
}
