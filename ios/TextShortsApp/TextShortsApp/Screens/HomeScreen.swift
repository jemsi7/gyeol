import ArticleFeedKit
import SwiftUI

struct HomeCatalogSections: Equatable {
    let lead: PrototypeArticle?
    let recent: [PrototypeArticle]
    let recommendations: [PrototypeArticle]

    init(
        articles: [PrototypeArticle],
        latestArticles: [PrototypeArticle]
    ) {
        let selectedLead = articles.first
        lead = selectedLead
        var recentIDs = Set<String>()
        recent = Array(
            latestArticles
                .filter {
                    $0.id != selectedLead?.id && recentIDs.insert($0.id).inserted
                }
                .prefix(HomeCatalogPolicy.recentArticleLimit)
        )
        let excludedIDs = Set(recent.map(\.id))
        recommendations = articles.dropFirst().filter {
            !excludedIDs.contains($0.id)
        }
    }
}

struct HomeCurationOutline: Equatable {
    let visibleItems: [DiscoveryRouteItem]
    let remainingCount: Int

    init(route: DiscoveryRoute, visibleLimit: Int = 3) {
        let boundedLimit = max(visibleLimit, 0)
        visibleItems = Array(route.items.prefix(boundedLimit))
        remainingCount = max(route.items.count - visibleItems.count, 0)
    }
}

struct HomeCurationCatalog: Equatable {
    let routes: [DiscoveryRoute]

    init(routes: [DiscoveryRoute]) {
        self.routes = routes.filter { !$0.items.isEmpty }
    }
}

struct HomeScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @State private var expandedCurationRouteID: UUID?

    let mode: LayoutMode
    let palette: AppPalette

    private var leadScene: PrototypeScene { store.article.scenes.first ?? store.scene }
    private var headline: String {
        store.article.headline.isEmpty ? leadScene.title : store.article.headline
    }
    private var progressFraction: CGFloat {
        CGFloat(store.clampedSceneIndex + 1) / CGFloat(max(store.article.scenes.count, 1))
    }

    var body: some View {
        GeometryReader { proxy in
            let contentWidth = max(
                0,
                min(proxy.size.width - horizontalInset * 2, 840)
            )
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    hubHeader
                    catalog(availableWidth: contentWidth)
                }
                .frame(width: contentWidth, alignment: .leading)
                .padding(.horizontal, horizontalInset)
                .frame(maxWidth: .infinity)
                .frame(minHeight: proxy.size.height, alignment: .top)
            }
            .scrollIndicators(.hidden)
            .accessibilityIdentifier("HOME-FEED")
        }
        .background(palette.paper)
        .onAppear { store.loadHomeCurationIfNeeded() }
        .onChange(of: store.selectedCurationRoute?.id) { routeID in
            guard expandedCurationRouteID != routeID else { return }
            expandedCurationRouteID = nil
        }
        .overlay(alignment: .bottom) {
            if let message = store.toastMessage {
                ToastView(message: message, palette: palette)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 14)
            }
        }
        .screenTagged(.scr10, show: store.showAnnotations)
    }

    private var horizontalInset: CGFloat { mode == .compact ? 24 : 40 }

    private var hubHeader: some View {
        HStack(spacing: 16) {
            Text("결")
                .font(.system(size: 38, weight: .heavy))
                .tracking(-5)
                .foregroundColor(palette.accentDeep)
                .accessibilityLabel("결 홈")
                .uxTagged(.ux095, show: store.showAnnotations)
            Spacer(minLength: 10)
            Button { store.selectPrimaryTab(.library) } label: {
                HStack(spacing: 6) {
                    Image(systemName: "bookmark")
                    Text("보관함")
                }
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(palette.ink)
                .frame(minHeight: AppMetrics.touchTarget)
            }
            .buttonStyle(.plain)
            .uxTagged(.ux099, show: store.showAnnotations)
            Button { store.selectPrimaryTab(.settings) } label: {
                HStack(spacing: 6) {
                    Image(systemName: "slider.horizontal.3")
                    Text("설정")
                }
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(palette.ink)
                .frame(minHeight: AppMetrics.touchTarget)
            }
            .buttonStyle(.plain)
            .uxTagged(.ux100, show: store.showAnnotations)
        }
        .frame(height: 60)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    @ViewBuilder
    private func catalog(availableWidth: CGFloat) -> some View {
        let sections = HomeCatalogSections(
            articles: store.homeArticles,
            latestArticles: store.homeLatestArticles
        )
        if let lead = sections.lead {
            LazyVStack(alignment: .leading, spacing: 0) {
                Text("오늘의 선택")
                    .font(.system(size: mode == .compact ? 19 : 22, weight: .bold))
                    .tracking(-0.7)
                    .foregroundColor(palette.warm)
                    .padding(.top, 30)
                    .padding(.bottom, 12)
                    .uxTagged(.ux103, show: store.showAnnotations)
                leadArticle(lead, availableWidth: availableWidth)
                if !sections.recent.isEmpty {
                    sectionHeader(
                        "최근 올라온 기사",
                        subtitle: "새 기사부터 빠르게 훑어보기",
                        trailing: "최신순"
                    )
                    .accessibilityIdentifier("HOME-RECENT")
                    ForEach(sections.recent) { article in
                        catalogRow(
                            article,
                            availableWidth: availableWidth,
                            allowsPagination: false
                        )
                    }
                }
                curationSection(availableWidth: availableWidth)
                if !sections.recommendations.isEmpty {
                    sectionHeader("기사 읽기 계속")
                        .accessibilityIdentifier("HOME-CONTINUE")
                    ForEach(sections.recommendations) { article in
                        catalogRow(
                            article,
                            availableWidth: availableWidth,
                            allowsPagination: true
                        )
                    }
                }
                homePaginationFooter
            }
        } else {
            emptyCatalog
                .padding(.top, 22)
                .uxTagged(.ux103, show: store.showAnnotations)
        }
    }

    private func sectionHeader(
        _ title: String,
        subtitle: String? = nil,
        trailing: String? = nil,
        titleColor: Color? = nil
    ) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(title)
                    .font(.system(size: mode == .compact ? 19 : 22, weight: .bold))
                    .tracking(-0.7)
                    .foregroundColor(titleColor ?? palette.ink)
                Spacer(minLength: 8)
                if let trailing {
                    Text(trailing)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(palette.muted)
                }
            }
            if let subtitle {
                Text(subtitle)
                    .font(.system(size: 11))
                    .foregroundColor(palette.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 28)
        .padding(.bottom, 12)
        .overlay(alignment: .bottom) {
            Rectangle().fill(palette.ruleStrong).frame(height: 1)
        }
    }

    private func leadArticle(_ article: PrototypeArticle, availableWidth: CGFloat) -> some View {
        return Button { store.openArticleFromHome(article) } label: {
            VStack(alignment: .leading, spacing: 0) {
                articleMedia(for: article)
                    .frame(
                        width: availableWidth,
                        height: mode == .compact ? 172 : 260
                    )
                    .clipped()
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                Text(article.homeMetadataText)
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .padding(.top, 9)
                if !article.entryNote.isEmpty {
                    // One line at the point of entry, in the existing muted
                    // caption style: a correction is a sentence, not a badge.
                    Text(article.entryNote)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 4)
                        .accessibilityLabel("사건 진행, \(article.entryNote)")
                }
                if !article.publisherContext.isEmpty {
                    Text(article.publisherContext)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 4)
                }
                Text(article.homeHeadline)
                    .font(.system(size: mode == .compact ? 25 : 31, weight: .bold))
                    .tracking(-1.2)
                    .foregroundColor(palette.ink)
                    .lineLimit(2)
                    .minimumScaleFactor(0.84)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 6)
                Text(article.homeSummary)
                    .font(.system(size: 12))
                    .foregroundColor(palette.muted)
                    .lineSpacing(4)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: mode == .compact ? 300 : nil, alignment: .leading)
                    .padding(.top, 7)
                Color.clear.frame(height: 14)
            }
            .frame(width: availableWidth, alignment: .leading)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(article.homeHeadline), \(article.homeMetadataText)")
            .accessibilityHint("기사를 읽습니다")
        }
        .buttonStyle(.plain)
        .uxTagged(.ux096, show: store.showAnnotations)
        .onAppear { store.homeArticleDidAppear(article.id) }
    }

    private func catalogRow(
        _ article: PrototypeArticle,
        availableWidth: CGFloat,
        allowsPagination: Bool
    ) -> some View {
        let thumbnailWidth = mode == .compact ? min(154, max(108, availableWidth * 0.45)) : 176
        return Button { store.openArticleFromHome(article) } label: {
            HStack(alignment: .top, spacing: 16) {
                articleMedia(for: article)
                    .frame(width: thumbnailWidth, height: mode == .compact ? 112 : 126)
                    .clipped()
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                VStack(alignment: .leading, spacing: 0) {
                    Text(article.homeMetadataText)
                        .font(.system(size: 9))
                        .foregroundColor(palette.muted)
                        .lineLimit(1)
                    Text(article.homeHeadline)
                        .font(.system(size: mode == .compact ? 16 : 18, weight: .bold))
                        .tracking(-0.65)
                        .foregroundColor(palette.ink)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                    Text(article.homeSummary)
                        .font(.system(size: 9.5))
                        .foregroundColor(palette.muted)
                        .lineSpacing(2)
                        .lineLimit(2)
                        .padding(.top, 6)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, minHeight: 112, alignment: .leading)
            .padding(.vertical, 12)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(article.homeHeadline), \(article.homeMetadataText)")
            .accessibilityHint("기사를 읽습니다")
        }
        .buttonStyle(.plain)
        .uxTagged(.ux097, show: store.showAnnotations)
        .onAppear {
            guard allowsPagination else { return }
            store.homeArticleDidAppear(article.id)
        }
    }

    @ViewBuilder
    private func curationSection(availableWidth: CGFloat) -> some View {
        let catalog = HomeCurationCatalog(
            routes: store.discoveryResponse?.routes ?? []
        )
        if !catalog.routes.isEmpty {
            sectionHeader(
                "오늘의 큐레이션",
                subtitle: "한 주제를 여러 관점으로 묶어 읽기",
                titleColor: palette.accentDeep
            )
            ScrollView(.horizontal) {
                HStack(alignment: .top, spacing: 12) {
                    ForEach(catalog.routes) { route in
                        curationCard(
                            route,
                            cardWidth: curationCardWidth(
                                availableWidth: availableWidth
                            )
                        )
                    }
                }
            }
            .frame(width: availableWidth, alignment: .leading)
            .scrollIndicators(.hidden)
            .accessibilityIdentifier("HOME-CURATION-RAIL")
        }
    }

    private func curationCardWidth(availableWidth: CGFloat) -> CGFloat {
        if mode == .compact {
            return max(availableWidth - 36, 1)
        }
        return min(max(availableWidth * 0.72, 1), 560)
    }

    private func curationCard(
        _ route: DiscoveryRoute,
        cardWidth: CGFloat
    ) -> some View {
        let isExpanded = expandedCurationRouteID == route.id
        return VStack(alignment: .leading, spacing: 0) {
            curationHeader(
                route,
                cardWidth: cardWidth,
                isExpanded: isExpanded
            )
            if isExpanded {
                curationOutline(route)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .frame(width: cardWidth, alignment: .leading)
        .background(palette.accentSoft.opacity(0.72))
        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
        .clipShape(Rectangle())
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("HOME-CURATION-CARD-\(route.id.uuidString)")
    }

    private func curationHeader(
        _ route: DiscoveryRoute,
        cardWidth: CGFloat,
        isExpanded: Bool
    ) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) {
                expandedCurationRouteID = isExpanded ? nil : route.id
            }
        } label: {
            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 7) {
                        Text("CURATION · \(route.items.count)개 기사")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .tracking(0.8)
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .bold))
                    }
                    .foregroundColor(palette.accentDeep)
                    Spacer(minLength: 12)
                    Text(route.title)
                        .font(.system(size: mode == .compact ? 21 : 25, weight: .bold))
                        .tracking(-0.9)
                        .foregroundColor(palette.ink)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(route.summary)
                        .font(.system(size: 10.5))
                        .foregroundColor(palette.muted)
                        .lineSpacing(2)
                        .lineLimit(3)
                        .padding(.top, 8)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .padding(14)
                curationArtwork(route)
                    .frame(
                        width: mode == .compact ? 92 : 132,
                        height: mode == .compact ? 146 : 168
                    )
                    .clipped()
                    .accessibilityHidden(true)
                    .overlay {
                        Color.clear
                            .contentShape(Rectangle())
                            .accessibilityElement()
                            .accessibilityLabel("큐레이션 기사 이미지")
                            .accessibilityIdentifier(
                                "HOME-CURATION-ARTWORK-\(route.id.uuidString)"
                            )
                    }
                    .padding(.trailing, 14)
            }
            .frame(
                width: cardWidth,
                height: mode == .compact ? 174 : 196,
                alignment: .leading
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("HOME-CURATION-\(route.id.uuidString)")
        .accessibilityLabel("\(route.title), \(route.items.count)개 기사 큐레이션, \(isExpanded ? "목차 펼침" : "목차 접힘")")
        .accessibilityHint(isExpanded ? "목차를 닫습니다" : "목차를 엽니다")
    }

    private func curationOutline(_ route: DiscoveryRoute) -> some View {
        let outline = HomeCurationOutline(route: route)
        return VStack(alignment: .leading, spacing: 0) {
            Rectangle()
                .fill(palette.line)
                .frame(height: 1)
            Text("읽는 순서")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .padding(.top, 12)
                .padding(.bottom, 5)
            ForEach(outline.visibleItems) { item in
                curationOutlineRow(item, in: route)
            }
            if outline.remainingCount > 0 {
                HStack(spacing: 0) {
                    Rectangle()
                        .fill(palette.muted)
                        .frame(width: 18, height: 1)
                    Text("외 \(outline.remainingCount)개 기사")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(palette.muted)
                        .padding(.leading, 8)
                }
                .frame(minHeight: 30)
            }
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 10)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("HOME-CURATION-TOC-\(route.id.uuidString)")
    }

    private func curationOutlineRow(
        _ item: DiscoveryRouteItem,
        in route: DiscoveryRoute
    ) -> some View {
        let projected = ArticleProjector().projectDisplayable(item.article)
        let itemHeadline = projected?.homeHeadline ?? item.article.map.headline
        let itemReadTime = projected?.readTime.replacingOccurrences(of: " 읽기", with: "") ?? ""
        return Button {
            store.openRouteArticle(item, in: route)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(String(format: "%02d", item.position))
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 24, alignment: .leading)
                Text(itemHeadline)
                    .font(.system(size: mode == .compact ? 13 : 15, weight: .semibold))
                    .tracking(-0.35)
                    .foregroundColor(palette.ink)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(itemReadTime)
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .fixedSize()
            }
            .frame(maxWidth: .infinity, minHeight: mode == .compact ? 42 : 48, alignment: .leading)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) {
                Rectangle().fill(palette.line).frame(height: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(
            "HOME-CURATION-ITEM-\(route.id.uuidString)-\(item.position)"
        )
        .accessibilityLabel("\(item.position)번째, \(itemHeadline), \(itemReadTime)")
        .accessibilityHint("이 기사를 읽습니다")
    }

    private func curationArtwork(_ route: DiscoveryRoute) -> some View {
        HStack(spacing: 7) {
            ForEach(Array(route.items.prefix(2))) { item in
                if let article = ArticleProjector().projectDisplayable(item.article) {
                    articleMedia(for: article)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .clipped()
                        .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                }
            }
        }
    }

    private func articleMedia(for article: PrototypeArticle) -> some View {
        let scene = article.scenes.first
        return ArticleMediaView(
            url: scene?.remoteImageURL,
            fallback: scene?.artwork ?? .tidal,
            accessibilityLabel: scene?.artworkLabel.isEmpty == false ? scene!.artworkLabel : article.homeHeadline,
            mediaSaver: store.settings.mediaSaver,
            offline: store.isOffline,
            palette: palette,
            thumbnailArticleID: article.id,
            cache: store.mediaCache
        )
    }

    private var emptyCatalog: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("읽을 기사를 준비하고 있어요")
                .font(.system(size: 25, weight: .bold))
                .tracking(-1.1)
                .foregroundColor(palette.ink)
            Text("연결을 확인하고 다시 시도해 주세요. 저장된 기사가 있으면 먼저 보여드립니다.")
                .font(.system(size: 12))
                .foregroundColor(palette.muted)
                .lineSpacing(4)
            Button(action: store.retryHomeRandom) {
                HStack { Text("다시 시도"); Spacer(); Image(systemName: "arrow.clockwise") }
            }
            .buttonStyle(PrimaryPillButtonStyle(palette: palette))
            .disabled(store.isLoadingFeed)
            .padding(.top, 12)
        }
        .frame(maxWidth: .infinity, minHeight: 360, alignment: .topLeading)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    @ViewBuilder
    private var homePaginationFooter: some View {
        switch store.homePaginationState {
        case .idle:
            Button(action: store.downloadNewArticlesFromHome) {
                Text("최근 기사 더 보기")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(palette.ink)
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .contentShape(Rectangle())
                    .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .disabled(store.isLoadingNewArticle)
            .accessibilityIdentifier("HOME-LOAD-MORE")
            .accessibilityHint("추천 기사를 더 불러옵니다")
            .padding(.top, 16)
            .padding(.bottom, 24)
                .uxTagged(.ux098, show: store.showAnnotations)
        case .loading:
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 7) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(palette.line)
                        .frame(width: 112, height: 5)
                    RoundedRectangle(cornerRadius: 2)
                        .fill(palette.line.opacity(0.7))
                        .frame(width: 72, height: 5)
                }
                Text("다음 기사를 불러오는 중")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(palette.muted)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("다음 기사를 불러오는 중")
            .accessibilityAddTraits(.updatesFrequently)
            .uxTagged(.ux098, show: store.showAnnotations)
        case .failed:
            Button(action: store.retryHomePagination) {
                HStack {
                    Text("다시 시도")
                    Spacer()
                    Image(systemName: "arrow.clockwise")
                }
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(maxWidth: .infinity, minHeight: 52)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("기사 목록을 이어서 불러옵니다")
            .uxTagged(.ux098, show: store.showAnnotations)
        case .end:
            Text("모든 기사를 살펴봤어요")
                .font(.system(size: 10, weight: .medium))
                .foregroundColor(palette.muted)
                .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
                .accessibilityAddTraits(.isStaticText)
                .uxTagged(.ux098, show: store.showAnnotations)
        }
    }

#if DEBUG
    // Previous current-article Home remains compiled only for forced-screen regression.

    private var homeHeading: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("지금 읽는 기사")
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .tracking(1.1)
                .foregroundColor(palette.warm)
            Text("읽던 흐름으로\n돌아갑니다.")
                .font(.system(size: mode == .wide ? 52 : mode == .split ? 44 : 36, weight: .heavy))
                .tracking(mode == .wide ? -3.8 : -2.6)
                .lineSpacing(-5)
                .foregroundColor(palette.ink)
                .fixedSize(horizontal: false, vertical: true)
                .uxTagged(.ux095, show: store.showAnnotations)
        }
    }

    @ViewBuilder
    private func currentArticleSection(availableWidth: CGFloat) -> some View {
        if mode == .compact {
            VStack(alignment: .leading, spacing: 0) {
                articleMedia
                    .frame(maxWidth: .infinity)
                    .frame(height: min(184, max(152, availableWidth * 0.42)))
                    .clipped()
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                    .uxTagged(.ux096, show: store.showAnnotations)
                articleCopy
                    .padding(.top, 18)
            }
            .sectionRules(palette: palette)
        } else {
            HStack(alignment: .top, spacing: 0) {
                articleMedia
                    .frame(width: min(430, availableWidth * 0.44))
                    .frame(minHeight: 270)
                    .clipped()
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                    .uxTagged(.ux096, show: store.showAnnotations)
                Rectangle().fill(palette.ruleStrong).frame(width: 1)
                articleCopy
                    .padding(.horizontal, mode == .wide ? 34 : 26)
                    .padding(.vertical, 26)
            }
            .sectionRules(palette: palette)
        }
    }

    private var articleMedia: some View {
        ArticleMediaView(
            url: leadScene.remoteImageURL,
            fallback: leadScene.artwork ?? .tidal,
            accessibilityLabel: leadScene.artworkLabel.isEmpty ? headline : leadScene.artworkLabel,
            mediaSaver: store.settings.mediaSaver,
            offline: store.isOffline,
            palette: palette,
            thumbnailArticleID: store.article.id,
            cache: store.mediaCache
        )
    }

    private var articleCopy: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(store.article.source)  ·  \(store.article.readTime)")
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(palette.muted)
            Text(headline)
                .font(.system(size: mode == .wide ? 30 : 25, weight: .bold))
                .tracking(-1.2)
                .foregroundColor(palette.ink)
                .lineLimit(3)
                .minimumScaleFactor(0.82)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 9)

            HStack(spacing: 12) {
                Text(store.homeProgressText)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundColor(palette.muted)
                    .fixedSize()
                GeometryReader { proxy in
                    ZStack(alignment: .leading) {
                        palette.deep
                        palette.accent.frame(width: proxy.size.width * progressFraction)
                    }
                }
                .frame(height: 4)
            }
            .padding(.top, 18)

            Button(action: store.continueReadingFromHome) {
                HStack {
                    Text(store.homePrimaryActionLabel)
                    Spacer()
                    Image(systemName: "arrow.right")
                }
            }
            .buttonStyle(PrimaryPillButtonStyle(palette: palette))
            .padding(.top, 18)
            .uxTagged(.ux097, show: store.showAnnotations)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, mode == .compact ? 20 : 0)
    }

    private var actionDirectory: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .lastTextBaseline) {
                Text("바로가기")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .tracking(1)
                Spacer()
                Text("기사 · 보관함 · 설정")
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
            }
            .padding(.bottom, 10)
            .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }

            directoryAction(
                number: "01",
                title: store.isLoadingNewArticle ? "새 기사 확인 중" : "새 기사",
                detail: "아직 읽지 않은 기사로 이동합니다.",
                symbol: "doc.badge.plus",
                id: .ux098,
                action: store.requestNewArticle
            )
            .disabled(store.isLoadingNewArticle)

            directoryAction(
                number: "02",
                title: "보관함",
                detail: "저장한 기사를 다시 읽습니다.",
                symbol: "books.vertical",
                id: .ux099
            ) { store.go(to: .library) }

            directoryAction(
                number: "03",
                title: "읽기 설정",
                detail: "글자·화면·오프라인 방식을 조정합니다.",
                symbol: "slider.horizontal.3",
                id: .ux100
            ) { store.go(to: .settings) }
        }
    }

    private func directoryAction(
        number: String,
        title: String,
        detail: String,
        symbol: String,
        id: UXID,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Text(number)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundColor(palette.warm)
                    .frame(width: 24, alignment: .leading)
                Image(systemName: symbol)
                    .font(.system(size: 18, weight: .medium))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 26)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(palette.ink)
                    Text(detail)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .lineLimit(2)
                }
                Spacer(minLength: 8)
                Image(systemName: "arrow.right")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(palette.accentDeep)
            }
            .frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityHint(detail)
        .uxTagged(id, show: store.showAnnotations)
    }
#endif
}

private extension View {
    func sectionRules(palette: AppPalette) -> some View {
        overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
            .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }
}
