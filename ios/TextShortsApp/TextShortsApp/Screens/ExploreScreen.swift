import ArticleFeedKit
import SwiftUI

struct ExploreScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.appMotionEnabled) private var motionEnabled
    @State private var query = ""
    @State private var timelineInitiallyRevealed = false
    @FocusState private var isSearchFocused: Bool

    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        GeometryReader { proxy in
            let inset: CGFloat = mode == .compact ? 24 : 40
            let contentWidth = max(0, min(proxy.size.width - inset * 2, 840))
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    searchField
                    content
                }
                .frame(width: contentWidth, alignment: .leading)
                .padding(.horizontal, inset)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(keyboardDismissDrag)
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: dismissSearchKeyboard)
        .background(palette.paper)
        .overlay(alignment: .bottom) {
            if let message = store.toastMessage {
                ToastView(message: message, palette: palette)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 14)
            }
        }
        .onAppear { query = store.discoveryQuery }
        .onChange(of: store.discoveryQuery) { query = $0 }
        .screenTagged(.scr11, show: store.showAnnotations)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("SEARCH / EXPLORE")
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .tracking(1.2)
                .foregroundColor(palette.warm)
            HStack(alignment: .lastTextBaseline, spacing: 12) {
                Text("둘러보기")
                    .font(.system(size: mode == .compact ? 30 : 34, weight: .heavy))
                    .tracking(-1.8)
                    .foregroundColor(palette.ink)
                Spacer(minLength: 8)
                Text("관심 있는 기사와\n주제를 찾아봅니다")
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .multilineTextAlignment(.trailing)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 84, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .uxTagged(.ux113, show: store.showAnnotations)
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(palette.accentDeep)
            TextField("기사, 주제, 키워드 검색", text: $query)
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(palette.ink)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .focused($isSearchFocused)
                .onSubmit { submitFocusedSearch(query) }
                .accessibilityIdentifier("DISCOVERY-SEARCH")
            Button {
                query = ""
                store.clearDiscoverySearch()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 18))
                    .foregroundColor(palette.muted)
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain)
            .opacity(query.isEmpty ? 0 : 1)
            .disabled(query.isEmpty)
            .allowsHitTesting(!query.isEmpty)
            .accessibilityHidden(query.isEmpty)
            .accessibilityLabel("검색어 지우기")
        }
        .frame(minHeight: 50)
        .padding(.leading, 14)
        .padding(.trailing, 0)
        .background(palette.raised)
        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
        .padding(.top, 18)
        .uxTagged(.ux114, show: store.showAnnotations)
    }

    private func dismissSearchKeyboard() {
        isSearchFocused = false
    }

    private var keyboardDismissDrag: some Gesture {
        DragGesture(minimumDistance: 8)
            .onChanged { value in
                guard abs(value.translation.height) > abs(value.translation.width) else { return }
                dismissSearchKeyboard()
            }
    }

    @ViewBuilder
    private var content: some View {
        if store.isDiscoverySearchLoading {
            searchLoading
        } else if isSearchFocused {
            focusedSearchSuggestions
        } else if store.discoveryResponse == nil {
            stateSurface
        } else if !store.discoveryQuery.isEmpty {
            searchResults
        } else {
            if case .failed = store.discoveryState {
                staleCatalogNotice
            }
            suggestions
        }
    }

    private var normalizedTypedQuery: String {
        query
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
    }

    private var focusedSearchSuggestions: some View {
        let trending = Array(
            (store.discoveryResponse?.trendingSearchSuggestions ?? []).prefix(3)
        )
        let typedQuery = normalizedTypedQuery
        return VStack(spacing: 0) {
            ForEach(Array(trending.enumerated()), id: \.offset) { index, value in
                focusedSearchSuggestionButton(
                    symbol: "arrow.up",
                    text: value,
                    identifier: "DISCOVERY-TRENDING-CANDIDATE-\(index + 1)",
                    accessibilityLabel: "급상승 검색어, \(value)"
                ) {
                    submitFocusedSearch(value)
                }
                if index < trending.count - 1 {
                    Rectangle()
                        .fill(palette.line)
                        .frame(height: 1)
                }
            }

            if typedQuery.count >= 2 {
                if !trending.isEmpty {
                    Rectangle()
                        .fill(palette.ruleStrong)
                        .frame(height: 1)
                }
                focusedSearchSuggestionButton(
                    symbol: "magnifyingglass",
                    text: "“\(typedQuery)” 검색",
                    identifier: "DISCOVERY-TYPED-CANDIDATE",
                    accessibilityLabel: "\(typedQuery) 검색"
                ) {
                    submitFocusedSearch(typedQuery)
                }
            }
        }
        .padding(.top, 8)
        .overlay(alignment: .bottom) {
            if !trending.isEmpty || typedQuery.count >= 2 {
                Rectangle()
                    .fill(palette.ruleStrong)
                    .frame(height: 1)
            }
        }
    }

    private func focusedSearchSuggestionButton(
        symbol: String,
        text: String,
        identifier: String,
        accessibilityLabel: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 18)
                    .accessibilityHidden(true)
                Text(text)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(palette.ink)
                    .lineLimit(1)
                Spacer(minLength: 8)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(store.isDiscoverySearchLoading)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("이 검색어로 기사를 찾습니다")
    }

    private func submitFocusedSearch(_ value: String) {
        let normalized = value
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        guard normalized.count >= 2 else { return }
        query = normalized
        isSearchFocused = false
        store.searchDiscovery(normalized)
    }

    @ViewBuilder
    private var suggestions: some View {
        let values = (store.discoveryResponse?.searchSuggestions ?? []).filter {
            $0.caseInsensitiveCompare("Editorial") != .orderedSame
        }.filter {
            $0.caseInsensitiveCompare("주간 인사이트") != .orderedSame
        }
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Text("검색어 후보")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(palette.muted)
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        searchSuggestionButton(
                            "Editorial",
                            identifier: "DISCOVERY-CANDIDATE-EDITORIAL"
                        ) {
                            query = "Editorial"
                            store.showEditorialDiscovery()
                        }
                        searchSuggestionButton(
                            "주간 인사이트",
                            identifier: "DISCOVERY-CANDIDATE-WEEKLY-INSIGHT",
                            accessibilityHint: "주간 인사이트로 발행된 글만 모아 봅니다"
                        ) {
                            query = "주간 인사이트"
                            store.showWeeklyInsightDiscovery()
                        }
                        ForEach(Array(values.enumerated()), id: \.offset) { index, value in
                            searchSuggestionButton(
                                value,
                                identifier: "DISCOVERY-CANDIDATE-\(index + 1)"
                            ) {
                                query = value
                                store.searchDiscovery(value)
                            }
                        }
                    }
                }
                .scrollIndicators(.hidden)
            }
            .padding(.top, 14)
            .uxTagged(.ux115, show: store.showAnnotations)

            if !featuredTimelines.isEmpty {
                featuredTimelineLanding
                    .padding(.top, 28)
            }
        }
    }

    private var featuredTimelines: [FeaturedTimeline] {
        (store.discoveryResponse?.featuredTimelines ?? []).sorted {
            if $0.rank != $1.rank { return $0.rank < $1.rank }
            return $0.stableKey < $1.stableKey
        }
    }

    private var selectedFeaturedTimeline: FeaturedTimeline? {
        if let key = store.selectedFeaturedTimelineKey,
           let selected = featuredTimelines.first(where: { $0.stableKey == key }) {
            return selected
        }
        return featuredTimelines.first
    }

    private var featuredTimelineLanding: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("타임라인 구분")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundColor(palette.accentDeep)
                Rectangle()
                    .fill(palette.line)
                    .frame(height: 1)
                Text("중요한 사건부터")
                    .font(.system(size: 9))
                    .foregroundColor(palette.muted)
                    .fixedSize()
            }
            .padding(.bottom, 12)

            LazyVGrid(columns: timelineSelectorColumns, spacing: 7) {
                ForEach(featuredTimelines) { timeline in
                    timelineSelector(timeline)
                }
            }
            .padding(.bottom, 17)

            if let timeline = selectedFeaturedTimeline {
                ZStack(alignment: .topLeading) {
                    FeaturedTimelinePage(
                        timeline: timeline,
                        mode: mode,
                        palette: palette,
                        initiallyRevealed: timelineInitiallyRevealed
                            || store.timelineEntranceRequest?.timelineKey == timeline.stableKey
                            || !motionEnabled,
                        isSubscribed: store.subscribedTimelineKeys.contains(timeline.stableKey),
                        isSubscriptionPending: store.pendingTimelineSubscriptionKeys.contains(
                            timeline.stableKey
                        ),
                        entranceRequest: store.timelineEntranceRequest,
                        onSubscribe: {
                            Task { await store.toggleTimelineSubscription(timeline.stableKey) }
                        },
                        onOpenNode: store.openTimelineArticle,
                        onConsumeEntrance: store.consumeTimelineEntrance
                    )
                    .id(timeline.stableKey)
                    .transition(
                        motionEnabled
                            ? .asymmetric(
                                insertion: .move(edge: .trailing).combined(with: .opacity),
                                removal: .move(edge: .leading).combined(with: .opacity)
                            )
                            : .identity
                    )
                }
                .clipped()
                .animation(
                    motionEnabled ? FeaturedTimelineMotion.pageTransition : nil,
                    value: timeline.stableKey
                )
            }
        }
        .overlay(alignment: .topLeading) {
            Color.clear
                .frame(width: 1, height: 1)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("추천 타임라인")
                .accessibilityIdentifier("FEATURED-TIMELINE-LANDING")
        }
        .onAppear(perform: revealInitialTimeline)
    }

    private var timelineSelectorColumns: [GridItem] {
        let count = mode == .compact ? 2 : min(max(featuredTimelines.count, 1), 6)
        return Array(
            repeating: GridItem(.flexible(), spacing: 7, alignment: .top),
            count: count
        )
    }

    private func timelineSelector(_ timeline: FeaturedTimeline) -> some View {
        let selected = timeline.stableKey == selectedFeaturedTimeline?.stableKey
        return Button {
            withAnimation(motionEnabled ? FeaturedTimelineMotion.pageTransition : nil) {
                store.selectFeaturedTimeline(timeline.stableKey)
            }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                Text(String(format: "%02d", timeline.rank))
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundColor(selected ? palette.accentSoft : palette.muted)
                Text(timeline.topic)
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(selected ? palette.raised : palette.ink)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, minHeight: mode == .compact ? 48 : 62, alignment: .topLeading)
            .background(selected ? palette.accentDeep : Color.clear)
            .overlay(Rectangle().stroke(selected ? palette.accentDeep : palette.line, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("FEATURED-TIMELINE-SELECT-\(timeline.stableKey)")
        .accessibilityLabel("\(timeline.topic), 타임라인 \(timeline.rank)")
        .accessibilityValue(selected ? "선택됨" : "선택 안 됨")
    }

    private func revealInitialTimeline() {
        guard !timelineInitiallyRevealed else { return }
        guard motionEnabled else {
            timelineInitiallyRevealed = true
            return
        }
        DispatchQueue.main.async {
            timelineInitiallyRevealed = true
        }
    }

    private func searchSuggestionButton(
        _ value: String,
        identifier: String,
        accessibilityHint: String = "이 검색어로 기사를 찾습니다",
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Text(value)
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(palette.ink)
                .padding(.horizontal, 13)
                .frame(minHeight: 38)
                .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(identifier)
        .accessibilityHint(accessibilityHint)
    }

    private var searchResults: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let timeline = selectedTimelineSearchResult {
                timelineSearchDetail(timeline)
            } else {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("‘\(store.discoveryQuery)’ 검색 결과")
                        .font(.system(size: 20, weight: .bold))
                        .foregroundColor(palette.ink)
                    Text(searchResultCountText)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(palette.muted)
                }
                Spacer()
                Button {
                    query = ""
                    store.clearDiscoverySearch()
                } label: {
                    Text("둘러보기로 돌아가기")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(palette.accentDeep)
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
            }
            .padding(.top, 24)
            .padding(.bottom, 12)

            if store.quickDiscoveryResults.isEmpty
                && store.deepDiscoveryResults.isEmpty
                && store.ragDiscoveryResults.isEmpty
                && store.timelineDiscoveryResults.isEmpty {
                noSearchResults
            } else {
                timelineResultGroup
                resultGroup("빠르게 읽기", detail: "핵심 흐름부터", results: store.quickDiscoveryResults)
                resultGroup("깊게 읽기", detail: "맥락과 근거까지", results: store.deepDiscoveryResults)
                resultGroup(
                    "관련 기사",
                    detail: "검색어와 맥락이 가까운 순서",
                    results: store.ragDiscoveryResults
                )
                publicationPaginationFooter
            }
            }
        }
        .uxTagged(.ux119, show: store.showAnnotations)
    }

    private var searchResultCountText: String {
        let existing = "빠르게 \(store.quickDiscoveryResults.count) · 깊게 \(store.deepDiscoveryResults.count)"
        var parts = [existing]
        if !store.timelineDiscoveryResults.isEmpty {
            parts.append("타임라인 \(store.timelineDiscoveryResults.count)")
        }
        if !store.ragDiscoveryResults.isEmpty {
            parts.append("관련 기사 \(store.ragDiscoveryResults.count)")
        }
        return parts.joined(separator: " · ")
    }

    private var selectedTimelineSearchResult: FeaturedTimeline? {
        guard let key = store.selectedTimelineSearchKey else { return nil }
        return store.timelineDiscoveryResults.first { $0.stableKey == key }
    }

    @ViewBuilder
    private var timelineResultGroup: some View {
        if !store.timelineDiscoveryResults.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("타임라인")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundColor(palette.warm)
                    Text("과거에 엄선된 사건 흐름까지")
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                    Spacer()
                }
                .padding(.top, 14)
                .padding(.bottom, 4)
                ForEach(store.timelineDiscoveryResults) { timeline in
                    Button {
                        store.selectTimelineSearchResult(timeline.stableKey)
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("\(Self.shortTimelineDate(timeline.startedAt)) · \(timeline.nodes.count) EVENTS")
                                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                                    .foregroundColor(palette.warm)
                                Text(timeline.title)
                                    .font(.system(size: 17, weight: .bold))
                                    .tracking(-0.55)
                                    .foregroundColor(palette.ink)
                                    .lineLimit(2)
                                Text(timeline.summary)
                                    .font(.system(size: 10))
                                    .foregroundColor(palette.muted)
                                    .lineLimit(2)
                            }
                            Spacer(minLength: 8)
                            Image(systemName: "arrow.right")
                                .font(.system(size: 12, weight: .bold))
                                .foregroundColor(palette.accentDeep)
                        }
                        .frame(maxWidth: .infinity, minHeight: 94, alignment: .leading)
                        .padding(.vertical, 13)
                        .contentShape(Rectangle())
                        .overlay(alignment: .bottom) {
                            Rectangle().fill(palette.line).frame(height: 1)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("DISCOVERY-TIMELINE-RESULT-\(timeline.stableKey)")
                    .accessibilityHint("전체 타임라인을 엽니다")
                }
            }
        }
    }

    private func timelineSearchDetail(_ timeline: FeaturedTimeline) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.clearTimelineSearchSelection() } label: {
                HStack(spacing: 7) {
                    Image(systemName: "chevron.left")
                    Text("검색 결과")
                }
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minHeight: 52)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("검색 결과로 돌아가기")

            FeaturedTimelinePage(
                timeline: timeline,
                mode: mode,
                palette: palette,
                initiallyRevealed: true,
                isSubscribed: store.subscribedTimelineKeys.contains(timeline.stableKey),
                isSubscriptionPending: store.pendingTimelineSubscriptionKeys.contains(
                    timeline.stableKey
                ),
                entranceRequest: nil,
                onSubscribe: {
                    Task { await store.toggleTimelineSubscription(timeline.stableKey) }
                },
                onOpenNode: store.openTimelineArticle,
                onConsumeEntrance: { _ in }
            )
            .padding(.top, 8)
        }
    }

    private static func shortTimelineDate(_ date: Date) -> String {
        TimelineDatePresentation.text(date)
    }

    private func resultGroup(
        _ title: String,
        detail: String,
        results: [DiscoverySearchResult]
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if !results.isEmpty {
                HStack {
                    Text(title)
                        .font(.system(size: 11, weight: .bold))
                        .foregroundColor(palette.warm)
                    Text(detail)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                    Spacer()
                }
                .padding(.top, 14)
                .padding(.bottom, 4)
                ForEach(results) { result in
                    searchResultRow(result)
                }
            }
        }
    }

    private func searchResultRow(_ result: DiscoverySearchResult) -> some View {
        let article = ArticleProjector().project(result.article)
        return Button { store.openDiscoverySearchResult(result.article) } label: {
            HStack(alignment: .top, spacing: 12) {
                searchResultThumbnail(for: article, articleID: result.article.articleID)
                VStack(alignment: .leading, spacing: 6) {
                    Text(result.article.domain ?? "출처 미상")
                        .font(.system(size: 9))
                        .foregroundColor(palette.muted)
                        .lineLimit(1)
                    Text(result.article.map.headline)
                        .font(.system(size: 17, weight: .bold))
                        .tracking(-0.55)
                        .foregroundColor(palette.ink)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(result.article.map.summary)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .lineLimit(2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "arrow.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(palette.accentDeep)
            }
            .frame(maxWidth: .infinity, minHeight: mode == .compact ? 94 : 110, alignment: .leading)
            .padding(.vertical, 13)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("DISCOVERY-RESULT-\(result.article.articleID)")
        .accessibilityHint("Reader에서 기사를 엽니다")
        .onAppear {
            store.loadMorePublicationResultsIfNeeded(visibleResultID: result.id)
        }
    }

    @ViewBuilder
    private var publicationPaginationFooter: some View {
        if store.isPublicationDiscovery,
           store.hasMorePublicationResults,
           store.discoveryState == .loading {
            ProgressView()
                .tint(palette.accentDeep)
                .frame(maxWidth: .infinity, minHeight: 56)
                .accessibilityLabel(
                    store.isWeeklyInsightDiscovery
                        ? "주간 인사이트 더 불러오는 중"
                        : "Editorial 더 불러오는 중"
                )
                .accessibilityIdentifier(
                    store.isWeeklyInsightDiscovery
                        ? "DISCOVERY-WEEKLY-INSIGHT-PAGINATION"
                        : "DISCOVERY-EDITORIAL-PAGINATION"
                )
        }
    }

    private func searchResultThumbnail(
        for article: PrototypeArticle,
        articleID: String
    ) -> some View {
        let scene = article.scenes.first
        let label = scene?.artworkLabel.isEmpty == false
            ? scene!.artworkLabel
            : article.homeHeadline
        return ArticleMediaView(
            url: scene?.remoteImageURL,
            fallback: scene?.artwork ?? .tidal,
            accessibilityLabel: label,
            mediaSaver: store.settings.mediaSaver,
            offline: store.isOffline,
            palette: palette,
            thumbnailArticleID: articleID,
            cache: store.mediaCache
        )
        .frame(
            width: mode == .compact ? 96 : 120,
            height: mode == .compact ? 68 : 84
        )
        .clipped()
        .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
        .overlay {
            Color.clear
                .contentShape(Rectangle())
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(label)
                .prototypeTagged("DISCOVERY-RESULT-THUMBNAIL-\(articleID)")
        }
    }

    private var noSearchResults: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("맞는 기사를 찾지 못했어요")
                .font(.system(size: 23, weight: .bold))
                .tracking(-1)
                .foregroundColor(palette.ink)
            Text("검색어를 조금 넓히거나 다른 검색어 후보를 골라보세요.")
                .font(.system(size: 12))
                .foregroundColor(palette.muted)
                .lineSpacing(4)
        }
        .frame(maxWidth: .infinity, minHeight: 240, alignment: .topLeading)
        .padding(.top, 24)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    private var searchLoading: some View {
        VStack(alignment: .leading, spacing: 12) {
            ProgressView()
                .tint(palette.accentDeep)
            Text("검색 결과를 찾는 중")
                .font(.system(size: 23, weight: .bold))
                .tracking(-1)
                .foregroundColor(palette.ink)
            Text("관련 기사와 읽기 깊이를 함께 살펴보고 있어요.")
                .font(.system(size: 12))
                .foregroundColor(palette.muted)
        }
        .frame(maxWidth: .infinity, minHeight: 280, alignment: .topLeading)
        .padding(.top, 28)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("검색 결과를 찾는 중")
        .accessibilityIdentifier("DISCOVERY-SEARCH-LOADING")
    }

    @ViewBuilder
    private var stateSurface: some View {
        switch store.discoveryState {
        case .loading, .idle:
            VStack(alignment: .leading, spacing: 12) {
                Text("오래 남을 기사를 고르는 중")
                    .font(.system(size: 23, weight: .bold))
                    .foregroundColor(palette.ink)
                ForEach(0..<4, id: \.self) { index in
                    Rectangle()
                        .fill(palette.line.opacity(index == 0 ? 0.9 : 0.55))
                        .frame(width: index == 0 ? 240 : nil, height: 8)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 320, alignment: .topLeading)
            .padding(.top, 28)
        case .empty:
            VStack(alignment: .leading, spacing: 9) {
                Text("지금 보여드릴 검색어 후보가 없어요")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(palette.ink)
                Text("검색할 수 있는 기사가 준비되면 여기에 나타납니다.")
                    .font(.system(size: 12))
                    .foregroundColor(palette.muted)
                    .lineSpacing(4)
                retryButton
                    .padding(.top, 14)
            }
            .frame(maxWidth: .infinity, minHeight: 320, alignment: .topLeading)
            .padding(.top, 28)
        case .failed:
            VStack(alignment: .leading, spacing: 9) {
                Text("둘러보기를 불러오지 못했어요")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(palette.ink)
                Text("연결을 확인한 뒤 다시 시도해 주세요.")
                    .font(.system(size: 12))
                    .foregroundColor(palette.muted)
                retryButton
                    .padding(.top, 14)
            }
            .frame(maxWidth: .infinity, minHeight: 320, alignment: .topLeading)
            .padding(.top, 28)
        case .loaded:
            EmptyView()
        }
        Color.clear
            .frame(width: 1, height: 1)
            .uxTagged(.ux120, show: store.showAnnotations)
    }

    private var retryButton: some View {
        Button(action: store.retryDiscovery) {
            HStack {
                Text("다시 시도")
                Spacer()
                Image(systemName: "arrow.clockwise")
            }
        }
        .buttonStyle(PrimaryPillButtonStyle(palette: palette))
        .accessibilityIdentifier("DISCOVERY-RETRY")
    }

    private var staleCatalogNotice: some View {
        HStack(spacing: 8) {
            Image(systemName: "wifi.exclamationmark")
            Text("저장된 둘러보기 정보를 보여드리고 있어요")
            Spacer()
            Button("새로고침", action: store.retryDiscovery)
                .font(.system(size: 10, weight: .bold))
                .frame(minHeight: 44)
        }
        .font(.system(size: 10))
        .foregroundColor(palette.muted)
        .padding(.top, 8)
    }
}

struct TimelineRailMotionPlan: Equatable {
    enum TargetInsertion: Equatable {
        case none
        case gap(index: Int, nodeID: UUID)
        case trailing(nodeID: UUID)
    }

    enum ScrollAnchor: Equatable {
        case center(UUID)
        case trailing(UUID)

        var nodeID: UUID {
            switch self {
            case let .center(nodeID), let .trailing(nodeID): nodeID
            }
        }
    }

    struct NodeMotion: Equatable {
        let nodeID: UUID
        let opacity: Double
        let xOffset: CGFloat
        let yOffset: CGFloat
        let delay: TimeInterval
    }

    let initialVisibleIDs: Set<UUID>
    let targetInsertion: TargetInsertion
    let nodeMotions: [NodeMotion]
    let scrollAnchor: ScrollAnchor?
    let trackInitialScaleX: CGFloat
    let insertionDelay: TimeInterval
    let highlightDuration: TimeInterval
    let animationsEnabled: Bool

    func nodeMotion(for nodeID: UUID) -> NodeMotion? {
        nodeMotions.first { $0.nodeID == nodeID }
    }

    static func make(
        nodes: [FeaturedTimelineNode],
        entrance: TimelineEntrance,
        motionEnabled: Bool
    ) -> TimelineRailMotionPlan {
        let orderedNodes = Array(nodes.prefix(64))
        let allIDs = Set(orderedNodes.map(\.id))
        let targetID: UUID?
        switch entrance {
        case let .inserted(nodeID), let .occurred(nodeID): targetID = nodeID
        case .initial, .selection, .none: targetID = nil
        }
        let validTargetID = targetID.flatMap { allIDs.contains($0) ? $0 : nil }

        var initialVisibleIDs = allIDs
        if motionEnabled, let validTargetID {
            initialVisibleIDs.remove(validTargetID)
        }

        let isInitialEntrance = entrance == .initial
        let nodeMotions = orderedNodes.enumerated().map { index, node in
            guard motionEnabled, isInitialEntrance else {
                return NodeMotion(
                    nodeID: node.id,
                    opacity: 1,
                    xOffset: 0,
                    yOffset: 0,
                    delay: 0
                )
            }
            let yOffset: CGFloat
            if node.lane == .branch {
                yOffset = 18
            } else {
                yOffset = index.isMultiple(of: 2) ? 10 : -10
            }
            return NodeMotion(
                nodeID: node.id,
                opacity: 0,
                xOffset: 18,
                yOffset: yOffset,
                delay: Double(index) * 0.045
            )
        }

        let targetInsertion: TargetInsertion
        let scrollAnchor: ScrollAnchor?
        switch entrance {
        case let .inserted(nodeID) where validTargetID == nodeID:
            let index = orderedNodes.firstIndex(where: { $0.id == nodeID }) ?? 0
            targetInsertion = motionEnabled ? .gap(index: index, nodeID: nodeID) : .none
            scrollAnchor = .center(nodeID)
        case let .occurred(nodeID) where validTargetID == nodeID:
            targetInsertion = motionEnabled ? .trailing(nodeID: nodeID) : .none
            scrollAnchor = .trailing(nodeID)
        case .initial, .selection, .none, .inserted, .occurred:
            targetInsertion = .none
            scrollAnchor = nil
        }

        return TimelineRailMotionPlan(
            initialVisibleIDs: initialVisibleIDs,
            targetInsertion: targetInsertion,
            nodeMotions: nodeMotions,
            scrollAnchor: scrollAnchor,
            trackInitialScaleX: motionEnabled && isInitialEntrance ? 0.001 : 1,
            insertionDelay: motionEnabled && validTargetID != nil ? 0.12 : 0,
            highlightDuration: 0.95,
            animationsEnabled: motionEnabled
        )
    }
}

private enum FeaturedTimelineMotion {
    static let pageTransition = Animation.spring(
        response: 0.48,
        dampingFraction: 0.86,
        blendDuration: 0.10
    )
    static let updateInsertion = Animation.spring(
        response: 0.52,
        dampingFraction: 0.82,
        blendDuration: 0.10
    )

    static func initialReveal(delay: TimeInterval) -> Animation {
        .timingCurve(0.20, 0.80, 0.20, 1.00, duration: 0.52)
            .delay(delay)
    }
}

enum FeaturedTimelineRailGeometry {
    static let columnWidth: CGFloat = 132
    static let columnGap: CGFloat = 12
    static let nodeCenterX: CGFloat = 18
    static let trackCenterY: CGFloat = 28
    static let mainCardY: CGFloat = 46
    static let branchNodeCenterY: CGFloat = 88
    static let branchCardY: CGFloat = 108
    static let cardWidth: CGFloat = 130
    static let cardHeight: CGFloat = 120
    static let cardPadding: CGFloat = 8
    static let columnHeight: CGFloat = 236
    static let railHeight: CGFloat = 246

    static func trackWidth(nodeCount: Int) -> CGFloat {
        CGFloat(max(nodeCount - 1, 0)) * (columnWidth + columnGap)
    }

    static func railWidth(nodeCount: Int) -> CGFloat {
        let gaps = CGFloat(max(nodeCount - 1, 0)) * columnGap
        return max(720, CGFloat(max(nodeCount, 1)) * columnWidth + gaps)
    }
}

private struct FeaturedTimelinePage: View {
    let timeline: FeaturedTimeline
    let mode: LayoutMode
    let palette: AppPalette
    let initiallyRevealed: Bool
    let isSubscribed: Bool
    let isSubscriptionPending: Bool
    let entranceRequest: TimelineEntranceRequest?
    let onSubscribe: () -> Void
    let onOpenNode: (FeaturedTimelineNode) -> Void
    let onConsumeEntrance: (UUID) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            legend
            FeaturedTimelineRail(
                timeline: timeline,
                palette: palette,
                initiallyRevealed: initiallyRevealed,
                entranceRequest: relevantEntranceRequest,
                onOpenNode: onOpenNode,
                onConsumeEntrance: onConsumeEntrance
            )
            .id("\(timeline.stableKey):\(relevantEntranceRequest?.id.uuidString ?? "idle")")
            Text("왼쪽은 앞선 사건 · 오른쪽은 가장 새 사건입니다.")
                .font(.system(size: 9))
                .foregroundColor(palette.muted)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var relevantEntranceRequest: TimelineEntranceRequest? {
        guard entranceRequest?.timelineKey == timeline.stableKey else { return nil }
        return entranceRequest
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 0) {
                Text("\(periodText) · \(timeline.nodes.count) EVENTS")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .tracking(0.4)
                    .foregroundColor(palette.warm)
                Text(timeline.title)
                    .font(.system(size: mode == .compact ? 24 : 31, weight: .bold))
                    .tracking(mode == .compact ? -1.3 : -1.7)
                    .foregroundColor(palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 9)
                Text(timeline.summary)
                    .font(.system(size: 11))
                    .foregroundColor(palette.muted)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 7)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: onSubscribe) {
                HStack(spacing: 6) {
                    if isSubscriptionPending {
                        ProgressView()
                            .controlSize(.small)
                            .tint(palette.accentDeep)
                            .accessibilityHidden(true)
                    }
                    Text(isSubscribed ? "구독 중" : "구독")
                        .font(.system(size: 10, weight: .bold))
                }
                .foregroundColor(palette.accentDeep)
                .padding(.horizontal, 10)
                .frame(minWidth: 68, minHeight: 44)
                .background(isSubscribed ? palette.accentSoft : Color.clear)
                .overlay(Rectangle().stroke(palette.accentDeep, lineWidth: 1))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isSubscriptionPending)
            .accessibilityIdentifier("FEATURED-TIMELINE-SUBSCRIBE-\(timeline.stableKey)")
            .accessibilityLabel(isSubscribed ? "구독 중" : "구독")
            .accessibilityHint(
                isSubscribed
                    ? "이 타임라인 업데이트 알림을 해제합니다"
                    : "이 타임라인 업데이트 알림을 받습니다"
            )
        }
        .padding(.bottom, 15)
    }

    private var legend: some View {
        HStack(spacing: 8) {
            Text("메인 줄기")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(palette.accentDeep)
            FeaturedTimelineBadge(text: "갱신", color: palette.accentDeep)
            FeaturedTimelineBadge(text: "추가됨", color: palette.warm)
            Spacer(minLength: 4)
            Text("가로로 넘겨 전체 보기")
                .font(.system(size: 9))
                .foregroundColor(palette.muted)
                .lineLimit(1)
        }
        .frame(minHeight: 44)
        .overlay(alignment: .top) {
            Rectangle().fill(palette.ruleStrong).frame(height: 1)
        }
    }

    private var periodText: String {
        "\(Self.shortDate(timeline.startedAt)) — \(Self.shortDate(timeline.endedAt))"
    }

    private static func shortDate(_ date: Date) -> String {
        TimelineDatePresentation.text(date)
    }
}

private struct FeaturedTimelineBadge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 9, weight: .bold, design: .monospaced))
            .foregroundColor(color)
            .padding(.horizontal, 7)
            .frame(minHeight: 20)
            .overlay(Rectangle().stroke(color, lineWidth: 1))
            .fixedSize()
    }
}

private struct FeaturedTimelineRail: View {
    @Environment(\.appMotionEnabled) private var motionEnabled

    let timeline: FeaturedTimeline
    let palette: AppPalette
    let initiallyRevealed: Bool
    let entranceRequest: TimelineEntranceRequest?
    let onOpenNode: (FeaturedTimelineNode) -> Void
    let onConsumeEntrance: (UUID) -> Void

    @State private var renderedNodeIDs: Set<UUID>?
    @State private var highlightedNodeID: UUID?
    @State private var handledEntranceID: UUID?

    init(
        timeline: FeaturedTimeline,
        palette: AppPalette,
        initiallyRevealed: Bool,
        entranceRequest: TimelineEntranceRequest?,
        onOpenNode: @escaping (FeaturedTimelineNode) -> Void,
        onConsumeEntrance: @escaping (UUID) -> Void
    ) {
        self.timeline = timeline
        self.palette = palette
        self.initiallyRevealed = initiallyRevealed
        self.entranceRequest = entranceRequest
        self.onOpenNode = onOpenNode
        self.onConsumeEntrance = onConsumeEntrance

        _renderedNodeIDs = State(initialValue: nil)
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: true) {
                ZStack(alignment: .topLeading) {
                    Rectangle()
                        .fill(palette.ruleStrong)
                        .frame(
                            width: FeaturedTimelineRailGeometry.trackWidth(
                                nodeCount: chronologicalNodes.count
                            ),
                            height: 2
                        )
                        .offset(
                            x: FeaturedTimelineRailGeometry.nodeCenterX,
                            y: FeaturedTimelineRailGeometry.trackCenterY - 1
                        )
                        .scaleEffect(x: motionPlan.trackInitialScaleX, y: 1, anchor: .leading)
                        .animation(
                            motionEnabled ? SwissMotion.easeOut(0.56) : nil,
                            value: initiallyRevealed
                        )

                    HStack(alignment: .top, spacing: FeaturedTimelineRailGeometry.columnGap) {
                        ForEach(renderedNodes) { node in
                            let index = chronologicalNodes.firstIndex(where: {
                                $0.id == node.id
                            }) ?? 0
                            let nodeMotion = motionPlan.nodeMotion(for: node.id)
                            FeaturedTimelineNodeColumn(
                                node: node,
                                index: index,
                                showsDate: beginsDateGroup(at: index),
                                isRightmost: node.id == chronologicalNodes.last?.id,
                                isHighlighted: node.id == highlightedNodeID,
                                palette: palette,
                                onOpen: { onOpenNode(node) }
                            )
                            .id(node.id)
                            .opacity(nodeMotion?.opacity ?? 1)
                            .offset(
                                x: nodeMotion?.xOffset ?? 0,
                                y: nodeMotion?.yOffset ?? 0
                            )
                            .animation(
                                motionPlan.animationsEnabled
                                    ? FeaturedTimelineMotion.initialReveal(
                                        delay: nodeMotion?.delay ?? 0
                                    )
                                    : nil,
                                value: initiallyRevealed
                            )
                            .transition(insertionTransition(for: node))
                        }
                    }
                }
                .frame(
                    width: railWidth,
                    height: FeaturedTimelineRailGeometry.railHeight,
                    alignment: .topLeading
                )
            }
            .accessibilityIdentifier("FEATURED-TIMELINE-RAIL")
            .task(id: entranceRequest?.id) {
                await performEntrance(using: proxy)
            }
        }
    }

    private var chronologicalNodes: [FeaturedTimelineNode] {
        timeline.chronologicalNodes
    }

    private var motionPlan: TimelineRailMotionPlan {
        TimelineRailMotionPlan.make(
            nodes: chronologicalNodes,
            entrance: entranceRequest?.entrance ?? (initiallyRevealed ? .none : .initial),
            motionEnabled: motionEnabled
        )
    }

    private var renderedNodes: [FeaturedTimelineNode] {
        let visibleIDs = renderedNodeIDs ?? motionPlan.initialVisibleIDs
        return chronologicalNodes.filter { visibleIDs.contains($0.id) }
    }

    private var railWidth: CGFloat {
        FeaturedTimelineRailGeometry.railWidth(nodeCount: chronologicalNodes.count)
    }

    private func beginsDateGroup(at index: Int) -> Bool {
        guard index > 0 else { return true }
        return !TimelineDatePresentation.isSameDay(
            chronologicalNodes[index - 1].publishedAt,
            chronologicalNodes[index].publishedAt
        )
    }

    private func insertionTransition(for node: FeaturedTimelineNode) -> AnyTransition {
        switch motionPlan.targetInsertion {
        case let .gap(_, nodeID) where nodeID == node.id:
            return .asymmetric(
                insertion: .scale(scale: 0.72).combined(with: .opacity),
                removal: .identity
            )
        case let .trailing(nodeID) where nodeID == node.id:
            return .asymmetric(
                insertion: .offset(x: 62).combined(with: .opacity),
                removal: .identity
            )
        case .none, .gap, .trailing:
            return .identity
        }
    }

    @MainActor
    private func performEntrance(using proxy: ScrollViewProxy) async {
        guard let request = entranceRequest,
              request.timelineKey == timeline.stableKey,
              chronologicalNodes.contains(where: { $0.id == request.nodeID }),
              handledEntranceID != request.id else { return }
        handledEntranceID = request.id
        let plan = TimelineRailMotionPlan.make(
            nodes: chronologicalNodes,
            entrance: request.entrance,
            motionEnabled: motionEnabled
        )

        if plan.animationsEnabled {
            try? await Task.sleep(
                nanoseconds: UInt64(plan.insertionDelay * 1_000_000_000)
            )
            withAnimation(FeaturedTimelineMotion.updateInsertion) {
                var visibleIDs = renderedNodeIDs ?? plan.initialVisibleIDs
                visibleIDs.insert(request.nodeID)
                renderedNodeIDs = visibleIDs
                highlightedNodeID = request.nodeID
            }
        } else {
            var visibleIDs = renderedNodeIDs ?? plan.initialVisibleIDs
            visibleIDs.insert(request.nodeID)
            renderedNodeIDs = visibleIDs
            highlightedNodeID = request.nodeID
        }

        await Task.yield()
        if let scrollAnchor = plan.scrollAnchor {
            let anchor: UnitPoint
            switch scrollAnchor {
            case .center: anchor = .center
            case .trailing: anchor = .trailing
            }
            if plan.animationsEnabled {
                withAnimation(FeaturedTimelineMotion.updateInsertion) {
                    proxy.scrollTo(scrollAnchor.nodeID, anchor: anchor)
                }
            } else {
                proxy.scrollTo(scrollAnchor.nodeID, anchor: anchor)
            }
        }

        try? await Task.sleep(
            nanoseconds: UInt64(plan.highlightDuration * 1_000_000_000)
        )
        highlightedNodeID = nil
        onConsumeEntrance(request.id)
    }
}

private struct FeaturedTimelineNodeColumn: View {
    let node: FeaturedTimelineNode
    let index: Int
    let showsDate: Bool
    let isRightmost: Bool
    let isHighlighted: Bool
    let palette: AppPalette
    let onOpen: () -> Void

    var body: some View {
        ZStack(alignment: .topLeading) {
            if node.lane == .branch {
                Rectangle()
                    .fill(palette.accent)
                    .frame(
                        width: 1,
                        height: FeaturedTimelineRailGeometry.branchNodeCenterY
                            - FeaturedTimelineRailGeometry.trackCenterY
                    )
                    .offset(
                        x: FeaturedTimelineRailGeometry.nodeCenterX - 0.5,
                        y: FeaturedTimelineRailGeometry.trackCenterY
                    )
            }

            Rectangle()
                .fill(dotFill)
                .frame(width: dotSize, height: dotSize)
                .overlay(Rectangle().stroke(dotStroke, lineWidth: node.lane == .branch ? 1 : 2))
                .offset(
                    x: FeaturedTimelineRailGeometry.nodeCenterX - dotSize / 2,
                    y: nodeCenterY - dotSize / 2
                )

            Button(action: onOpen) {
                VStack(alignment: .leading, spacing: 0) {
                    if showsDate {
                        Text(Self.shortDate(node.publishedAt))
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundColor(palette.accentDeep)
                    } else {
                        Color.clear.frame(height: 12)
                    }
                    Text(node.article.map.headline)
                        .font(.system(size: 13, weight: .bold))
                        .tracking(-0.35)
                        .foregroundColor(palette.ink)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                    Text(sourceText)
                        .font(.system(size: 9))
                        .foregroundColor(palette.muted)
                        .lineLimit(2)
                        .padding(.top, 4)
                    if let badgeText {
                        FeaturedTimelineBadge(
                            text: badgeText,
                            color: badgeText == "추가됨" ? palette.warm : palette.accentDeep
                        )
                        .padding(.top, 6)
                    }
                    Spacer(minLength: 0)
                }
                .frame(
                    width: FeaturedTimelineRailGeometry.cardWidth
                        - FeaturedTimelineRailGeometry.cardPadding * 2,
                    height: FeaturedTimelineRailGeometry.cardHeight
                        - FeaturedTimelineRailGeometry.cardPadding * 2,
                    alignment: .topLeading
                )
                .padding(FeaturedTimelineRailGeometry.cardPadding)
                .background(isHighlighted ? palette.accentSoft : palette.raised)
                .overlay(
                    Rectangle().stroke(
                        isHighlighted ? palette.warm : palette.line,
                        lineWidth: 1
                    )
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .offset(y: node.lane == .branch
                ? FeaturedTimelineRailGeometry.branchCardY
                : FeaturedTimelineRailGeometry.mainCardY)
            .accessibilityIdentifier("FEATURED-TIMELINE-NODE-\(node.id.uuidString.lowercased())")
            .accessibilityLabel(accessibilityLabel)
            .accessibilityHint("Reader에서 이 사건의 기사를 엽니다")
        }
        .frame(
            width: FeaturedTimelineRailGeometry.columnWidth,
            height: FeaturedTimelineRailGeometry.columnHeight,
            alignment: .topLeading
        )
    }

    private var sourceText: String {
        let source = node.article.domain ?? "출처 미상"
        return node.lane == .branch ? "방계 사건 · \(source)" : source
    }

    private var badgeText: String? {
        if isRightmost || node.updateKind == .occurred { return "추가됨" }
        if node.updateKind == .inserted { return "갱신" }
        return nil
    }

    private var dotSize: CGFloat { node.lane == .branch ? 8 : 12 }
    private var nodeCenterY: CGFloat {
        node.lane == .branch
            ? FeaturedTimelineRailGeometry.branchNodeCenterY
            : FeaturedTimelineRailGeometry.trackCenterY
    }
    private var dotFill: Color {
        if isRightmost || node.updateKind == .occurred { return palette.warm }
        return node.lane == .branch ? palette.accentSoft : palette.paper
    }
    private var dotStroke: Color {
        isRightmost || node.updateKind == .occurred ? palette.warm : palette.accentDeep
    }

    private var accessibilityLabel: String {
        [
            Self.shortDate(node.publishedAt),
            node.article.map.headline,
            node.lane == .branch ? "방계 사건" : "메인 줄기",
            badgeText,
        ].compactMap { $0 }.joined(separator: ", ")
    }

    private static func shortDate(_ date: Date) -> String {
        TimelineDatePresentation.text(date)
    }
}

enum TimelineDatePresentation {
    static let timeZone = TimeZone(identifier: "Asia/Seoul")
        ?? TimeZone(secondsFromGMT: 9 * 60 * 60)!

    static let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "ko_KR")
        calendar.timeZone = timeZone
        return calendar
    }()

    static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.timeZone = timeZone
        formatter.dateFormat = "M/d"
        return formatter
    }()

    static func day(_ date: Date) -> Date {
        calendar.startOfDay(for: date)
    }

    static func isSameDay(_ lhs: Date, _ rhs: Date) -> Bool {
        day(lhs) == day(rhs)
    }

    static func text(_ date: Date) -> String {
        formatter.string(from: date)
    }
}
