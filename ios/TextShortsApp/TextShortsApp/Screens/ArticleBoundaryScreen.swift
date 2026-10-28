import SwiftUI

struct ArticleBoundaryScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let mode: LayoutMode
    let palette: AppPalette
    @State private var progressVisible = false

    private var motionEnabled: Bool { store.settings.motionEnabled && !reduceMotion }
    private var totalScenes: Int { max(store.article.scenes.count, 1) }

    var body: some View {
        VStack(spacing: 0) {
            boundaryHeader
            GeometryReader { viewport in
                ScrollView {
                    if mode == .wide {
                        HStack(alignment: .top, spacing: 56) {
                            completedBlock.frame(maxWidth: 420)
                            nextPreview.frame(maxWidth: 520)
                        }
                        .frame(maxWidth: 980)
                        .padding(.vertical, 44)
                        .frame(width: viewport.size.width)
                    } else {
                        VStack(alignment: .leading, spacing: 0) {
                            completedBlock
                            nextPreview
                        }
                        .frame(width: viewport.size.width, alignment: .leading)
                    }
                }
                .scrollIndicators(.hidden)
            }
            actionRow
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .background(palette.paper)
        .onAppear {
            if motionEnabled {
                withAnimation(SwissMotion.easeOut(SwissMotion.completion)) { progressVisible = true }
            } else {
                progressVisible = true
            }
        }
        .screenTagged(.scr03, show: store.showAnnotations)
    }

    private var boundaryHeader: some View {
        HStack(spacing: 12) {
            Text("ARTICLE COMPLETE")
                .font(.system(size: 9, weight: .medium, design: .monospaced))
                .tracking(1.2)
            Spacer()
            Text(String(format: "%02d / %02d", totalScenes, totalScenes))
                .font(.system(size: 9, weight: .medium, design: .monospaced))
                .tracking(1.2)
            readerOriginBackButton
        }
        .frame(height: 60)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    private var readerOriginBackButton: some View {
        Button(action: store.returnToReaderOrigin) {
            Label("돌아가기", systemImage: "chevron.left")
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minWidth: 58, minHeight: AppMetrics.touchTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .fixedSize(horizontal: true, vertical: false)
        .accessibilityLabel("돌아가기")
        .accessibilityHint("Reader에 들어오기 전 화면으로 돌아갑니다")
        .uxTagged(.ux124, show: store.showAnnotations)
    }

    private var completedBlock: some View {
        VStack(alignment: .leading, spacing: 0) {
            Label("기사 읽기 완료", systemImage: "checkmark")
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .uxTagged(.ux037, show: store.showAnnotations)

            Text(store.article.scenes.first?.title ?? store.article.source)
                .font(.system(size: mode == .wide ? 40 : 31, weight: .bold))
                .tracking(mode == .wide ? -2.4 : -1.8)
                .lineSpacing(-2)
                .foregroundColor(palette.ink)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)
                .uxTagged(.ux038, show: store.showAnnotations)

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    palette.deep
                    palette.accent.frame(width: progressVisible ? proxy.size.width : 0)
                }
            }
            .frame(height: 4)
            .padding(.top, 20)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("기사 전체 읽기 완료")
            .accessibilityValue("100퍼센트")
            .uxTagged(.ux039, show: store.showAnnotations)

            LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 14, alignment: .leading)], alignment: .leading, spacing: 8) {
                ForEach(store.article.keywords.prefix(6), id: \.self) { keyword in
                    HStack(spacing: 2) {
                        Text("#").foregroundColor(palette.warm)
                        Text(keyword).foregroundColor(palette.muted)
                    }
                    .font(.system(size: 11))
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.top, 14)
            .uxTagged(.ux040, show: store.showAnnotations)
        }
        .padding(.vertical, 27)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    @ViewBuilder
    private var nextPreview: some View {
        if let routeArticle = store.nextRouteArticle {
            routeContinuationPreview(routeArticle)
        } else if let timelineArticle = store.nextTimelineArticle {
            timelineContinuationPreview(timelineArticle)
        } else if !store.displayedNextArticles.isEmpty {
            let recommendations = store.displayedNextArticles
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    Text("다음 기사")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(palette.warm)
                    Spacer()
                    Text("\(recommendations.count)개")
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundColor(palette.muted)
                }
                .padding(.top, 18)
                .padding(.bottom, 4)

                ForEach(recommendations.indices, id: \.self) { index in
                    let article = recommendations[index]
                    nextArticleRow(
                        article,
                        index: index,
                        accessibilityLabel: index == 0 ? "계속 읽기" : "다음 기사 \(index + 1)"
                    ) {
                        store.continueNextArticle(article)
                    }
                }
            }
            .padding(.bottom, mode == .wide ? 12 : 0)
        } else {
            VStack(alignment: .leading, spacing: 12) {
                Image(systemName: "newspaper")
                    .font(.system(size: 34, weight: .light))
                    .foregroundColor(palette.accentDeep)
                    .uxTagged(.ux041, show: store.showAnnotations)
                Text("새 기사 확인")
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .uxTagged(.ux042, show: store.showAnnotations)
                Text("지금 준비된 기사를 모두 읽었습니다")
                    .font(.system(size: 22, weight: .bold))
                    .uxTagged(.ux043, show: store.showAnnotations)
                Button(action: store.retryLoad) {
                    HStack { Text("새 기사 확인"); Spacer(); Image(systemName: "arrow.right") }
                }
                .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                .uxTagged(.ux044, show: store.showAnnotations)
            }
            .padding(.vertical, 24)
        }
    }

    private func routeContinuationPreview(_ article: PrototypeArticle) -> some View {
        let progress = store.routeContinuationProgressText ?? ""
        let accessibilityLabel = progress.isEmpty
            ? "루트 이어 읽기"
            : "루트 이어 읽기, \(progress)"
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("루트 이어 읽기")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(palette.warm)
                Spacer()
                Text(progress)
                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                    .foregroundColor(palette.muted)
            }
            .padding(.top, 18)
            .padding(.bottom, 4)

            nextArticleRow(
                article,
                index: 0,
                accessibilityLabel: accessibilityLabel
            ) {
                store.continueNextRouteArticle()
            }
        }
        .padding(.bottom, mode == .wide ? 12 : 0)
    }

    private func timelineContinuationPreview(_ article: PrototypeArticle) -> some View {
        let progress = store.timelineContinuationProgressText ?? ""
        let accessibilityLabel = progress.isEmpty
            ? "타임라인 이어 읽기"
            : "타임라인 이어 읽기, \(progress)"
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("타임라인 이어 읽기")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(palette.warm)
                Spacer()
                Text(progress)
                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                    .foregroundColor(palette.muted)
            }
            .padding(.top, 18)
            .padding(.bottom, 4)

            nextArticleRow(
                article,
                index: 0,
                accessibilityLabel: accessibilityLabel
            ) {
                store.continueNextTimelineArticle()
            }
        }
        .padding(.bottom, mode == .wide ? 12 : 0)
    }

    @ViewBuilder
    private func nextArticleRow(
        _ article: PrototypeArticle,
        index: Int,
        accessibilityLabel: String,
        action: @escaping () -> Void
    ) -> some View {
        let headline = article.scenes.first?.title ?? article.source
        let row = Button(action: action) {
            HStack(alignment: .center, spacing: 12) {
                previewArtwork(for: article, index: index)
                VStack(alignment: .leading, spacing: 4) {
                    nextArticleSource(article, index: index)
                    nextArticleHeadline(headline, index: index)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "arrow.right")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(palette.accentDeep)
            }
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }

        if index == 0 {
            row
                .accessibilityLabel(accessibilityLabel)
                .accessibilityValue(headline)
                .uxTagged(.ux044, show: store.showAnnotations)
        } else {
            row
                .accessibilityLabel("다음 기사 \(index + 1)")
                .accessibilityValue(headline)
                .prototypeTagged("BOUNDARY-NEXT-\(index + 1)")
        }
    }

    @ViewBuilder
    private func nextArticleSource(_ article: PrototypeArticle, index: Int) -> some View {
        let source = Text("\(article.source) · \(article.readTime)")
            .font(.system(size: 9))
            .foregroundColor(palette.muted)
            .lineLimit(1)
        if index == 0 {
            source.uxTagged(.ux042, show: store.showAnnotations)
        } else {
            source.prototypeTagged("BOUNDARY-NEXT-SOURCE-\(index + 1)")
        }
    }

    @ViewBuilder
    private func nextArticleHeadline(_ headline: String, index: Int) -> some View {
        let title = Text(headline)
            .font(.system(size: mode == .wide ? 18 : 16, weight: .semibold))
            .tracking(-0.4)
            .lineSpacing(1)
            .foregroundColor(palette.ink)
            .lineLimit(2)
        if index == 0 {
            title.uxTagged(.ux043, show: store.showAnnotations)
        } else {
            title.prototypeTagged("BOUNDARY-NEXT-TITLE-\(index + 1)")
        }
    }

    @ViewBuilder
    private func previewArtwork(for article: PrototypeArticle, index: Int) -> some View {
        let scene = article.scenes.first ?? store.scene
        let label = scene.artworkLabel.isEmpty ? scene.title : scene.artworkLabel
        let artwork = ArticleMediaView(
            url: scene.remoteImageURL,
            fallback: scene.artwork ?? .canopy,
            accessibilityLabel: label,
            mediaSaver: store.settings.mediaSaver,
            offline: store.isOffline,
            palette: palette,
            thumbnailArticleID: article.id,
            cache: store.mediaCache
        )
        .allowsHitTesting(false)
        .accessibilityHidden(true)
        .frame(
            width: mode == .compact ? 96 : 120,
            height: mode == .compact ? 68 : 84
        )
        .clipped()
        .overlay(Rectangle().stroke(palette.line, lineWidth: 1))

        if index == 0 {
            artwork.overlay {
                Color.clear
                    .contentShape(Rectangle())
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(label)
                    .uxTagged(.ux041, show: store.showAnnotations)
            }
        } else {
            artwork.overlay {
                Color.clear
                    .contentShape(Rectangle())
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(label)
                    .prototypeTagged("BOUNDARY-NEXT-MEDIA-\(index + 1)")
            }
        }
    }

    private var actionRow: some View {
        HStack(spacing: 0) {
            Button(action: store.returnToLastScene) {
                HStack { Image(systemName: "arrow.left"); Text("마지막 장면") }
                    .frame(maxWidth: .infinity, minHeight: 68)
            }
            .buttonStyle(.plain)
            .uxTagged(.ux045, show: store.showAnnotations)
            Rectangle().fill(palette.line).frame(width: 1)
            Button(action: store.toggleSaved) {
                HStack {
                    Image(systemName: store.isSaved ? "bookmark.fill" : "bookmark")
                    Text(store.isSaved ? "저장됨" : "기사 저장")
                }
                .frame(maxWidth: .infinity, minHeight: 68)
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(store.isSaved ? .isSelected : AccessibilityTraits())
            .uxTagged(.ux046, show: store.showAnnotations)
        }
        .frame(height: 68)
        .font(.system(size: 12, weight: .bold))
        .foregroundColor(palette.accentDeep)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }
}
