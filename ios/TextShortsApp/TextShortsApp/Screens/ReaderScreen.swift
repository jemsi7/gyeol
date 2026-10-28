import SwiftUI

struct ReaderScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AccessibilityFocusState private var articleFocused: Bool
    let mode: LayoutMode
    let palette: AppPalette

    private var motionEnabled: Bool { store.settings.motionEnabled && !reduceMotion }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                VStack(spacing: 0) {
                    if mode != .wide { compactHeader }
                    if mode != .wide {
                        Rectangle().fill(palette.ruleStrong).frame(height: 1).padding(.horizontal, 16)
                    }
#if DEBUG
                    if store.showPrototypeTools && store.showReaderGuide { readerGuide }
#endif
                    readerLayout(size: proxy.size)
                    sceneControls
                }
                .background(palette.paper)

                skipControl

                if store.showContext || store.showOverflow {
                    palette.ink.opacity(0.42)
                        .contentShape(Rectangle())
                        .onTapGesture(perform: store.closeOverlays)
                        .transition(.opacity)
                        .zIndex(5)
                }

                if store.showContext, mode != .wide {
                    ContextPanel(mode: mode, palette: palette)
                        .environmentObject(store)
                        .frame(width: min(332, proxy.size.width - 28), height: proxy.size.height)
                        .transition(motionEnabled ? .move(edge: .leading) : .opacity)
                        .zIndex(6)
                }

                if store.showOverflow, mode != .wide {
                    overflowMenu
                        .padding(.top, 68)
                        .padding(.trailing, 14)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .transition(motionEnabled ? .opacity.combined(with: .offset(y: -8)) : .opacity)
                        .zIndex(7)
                }

                if store.isOffline {
                    Text("오프라인 저장본")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(palette.ink)
                        .padding(.horizontal, 10)
                        .frame(minHeight: 28)
                        .background(palette.accentSoft)
                        .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, mode == .wide ? 16 : AppMetrics.headerHeight + 7)
                        .uxTagged(.ux030, show: store.showAnnotations)
                        .zIndex(3)
                }

                if let message = store.toastMessage {
                    ToastView(message: message, palette: palette)
                        .padding(.horizontal, 24)
                        .padding(.bottom, AppMetrics.controlsHeight + 12)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                        .uxTagged(.ux036, show: store.showAnnotations)
                        .transition(motionEnabled ? .opacity.combined(with: .offset(y: 8)) : .opacity)
                        .zIndex(9)
                }

                KeyCommandReader(
                    onPrevious: store.previousScene,
                    onNext: store.nextScene,
                    onSave: store.toggleSaved,
                    onEscape: store.closeOverlays
                )
                .frame(width: 1, height: 1)
                .opacity(0.001)
                .allowsHitTesting(false)

#if DEBUG
                if LiveMapE2EEnvironment.isEnabled {
                    LiveMapE2ESentinelView(
                        article: store.article,
                        sceneIndex: store.clampedSceneIndex
                    )
                    .zIndex(20)
                }
#endif
            }
            .animation(motionEnabled ? SwissMotion.easeOut() : nil, value: store.showContext)
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: store.showOverflow)
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: store.toastMessage)
        }
        .screenTagged(.scr02, show: store.showAnnotations)
        .confirmationDialog(
            "왜 이 기사가 아쉬웠나요?",
            isPresented: $store.showArticleFeedback,
            titleVisibility: .visible
        ) {
            ForEach(ArticleFeedbackReason.allCases) { reason in
                Button(reason.displayName) {
                    Task { await store.submitArticleFeedback(reason) }
                }
            }
            Button("취소", role: .cancel) {}
        } message: {
            Text("추천을 더 잘 맞추는 데 활용할게요.")
        }
    }

    private var skipControl: some View {
        Button("본문 바로가기") { articleFocused = true }
            .font(.system(size: 1))
            .frame(width: 1, height: 1)
            .opacity(store.showAnnotations ? 0.02 : 0.001)
            .accessibilityLabel("본문 바로가기")
            .uxTagged(.ux008, show: store.showAnnotations)
    }

    private var compactHeader: some View {
        HStack(spacing: 3) {
            ReaderBackNavigationButton(
                palette: palette,
                showAnnotations: store.showAnnotations,
                action: store.returnToReaderOrigin
            )

            Button(action: store.toggleContext) {
                HStack(spacing: 5) {
                    Text(store.article.source)
                        .font(.system(size: 12, weight: .bold))
                        .lineLimit(1)
                    Image(systemName: "list.bullet")
                        .font(.system(size: 13, weight: .medium))
                }
                .frame(maxWidth: .infinity, minHeight: AppMetrics.touchTarget, alignment: .leading)
                .padding(.horizontal, 5)
            }
            .buttonStyle(.plain)
            .layoutPriority(1)
            .accessibilityLabel("기사 맥락, \(store.article.source)")
            .accessibilityValue(store.showContext ? "펼침" : "접힘")
            .uxTagged(.ux010, show: store.showAnnotations)

            progressIndicator
                .frame(width: 76)
                .uxTagged(.ux011, show: store.showAnnotations)

            Button(action: store.toggleSaved) {
                Image(systemName: store.isSaved ? "bookmark.fill" : "bookmark")
                    .font(.system(size: 20, weight: .regular))
                    .foregroundColor(palette.accentDeep)
            }
            .buttonStyle(CircleIconButtonStyle(palette: palette))
            .accessibilityLabel(store.isSaved ? "저장 해제" : "저장")
            .accessibilityAddTraits(store.isSaved ? .isSelected : AccessibilityTraits())
            .uxTagged(.ux012, show: store.showAnnotations)

            Button(action: store.presentArticleFeedback) {
                Text("😟")
                    .font(.system(size: 18))
            }
            .buttonStyle(CircleIconButtonStyle(palette: palette))
            .disabled(store.isSubmittingArticleFeedback)
            .accessibilityLabel("이 기사 피드백")
            .accessibilityValue(feedbackAccessibilityValue)
            .accessibilityIdentifier("ARTICLE-FEEDBACK-ACTION")

            Button(action: store.toggleOverflow) {
                Image(systemName: "ellipsis")
                    .font(.system(size: 19, weight: .medium))
            }
            .buttonStyle(CircleIconButtonStyle(palette: palette))
            .accessibilityLabel("기사 더보기")
            .accessibilityValue(store.showOverflow ? "펼침" : "접힘")
            .uxTagged(.ux013, show: store.showAnnotations)
        }
        .frame(height: AppMetrics.headerHeight)
        .padding(.horizontal, 16)
    }

    private var feedbackAccessibilityValue: String {
        if store.isSubmittingArticleFeedback { return "보내는 중" }
        if let reason = store.currentArticleFeedbackReason {
            return "보낸 의견, \(reason.displayName)"
        }
        return "선택 안 함"
    }

    private var progressIndicator: some View {
        let total = max(store.article.scenes.count, 1)
        let fraction = CGFloat(store.sceneIndex + 1) / CGFloat(total)
        return VStack(spacing: 7) {
            Text("\(store.sceneIndex + 1) / \(total)")
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(palette.muted)
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    palette.deep
                    palette.accent.frame(width: proxy.size.width * fraction)
                }
            }
            .frame(height: 4)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("기사 장면 진행")
        .accessibilityValue("\(store.sceneIndex + 1) / \(total)")
    }

    private var readerGuide: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "hand.draw")
                .font(.system(size: 17, weight: .medium))
                .foregroundColor(palette.accentDeep)
                .frame(width: 24)
            Text("좌우로 넘기거나 아래 버튼으로 장면 이동 · 출처에서 목차 · 뒤로 버튼으로 돌아가기")
                .font(.system(size: mode == .compact ? 10 : 11, weight: .medium))
                .foregroundColor(palette.inkSoft)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .uxTagged(.ux102, show: store.showAnnotations)
            Spacer(minLength: 6)
            Button("확인", action: store.dismissReaderGuide)
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minWidth: 48, minHeight: AppMetrics.touchTarget)
                .buttonStyle(.plain)
                .accessibilityHint("이 안내를 다시 표시하지 않습니다")
                .uxTagged(.ux103, show: store.showAnnotations)
        }
        .padding(.horizontal, mode == .wide ? 20 : 16)
        .frame(minHeight: 56)
        .background(palette.accentSoft.opacity(0.72))
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    @ViewBuilder
    private func readerLayout(size: CGSize) -> some View {
        if mode == .wide {
            HStack(spacing: 0) {
                ContextPanel(mode: mode, palette: palette)
                    .environmentObject(store)
                    .frame(width: AppMetrics.wideRail)
                Rectangle().fill(palette.ruleStrong).frame(width: 1)
                ReaderSceneView(mode: mode, availableWidth: min(AppMetrics.wideReadingColumn, size.width - AppMetrics.wideRail), palette: palette)
                    .environmentObject(store)
                    .accessibilityFocused($articleFocused)
                    .frame(maxWidth: AppMetrics.wideReadingColumn)
                Text(String(format: "%02d", store.sceneIndex + 1))
                    .font(.system(size: 118, weight: .heavy))
                    .tracking(-9)
                    .foregroundColor(palette.line)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing)
                    .padding(24)
                    .accessibilityHidden(true)
            }
        } else {
            ReaderSceneView(mode: mode, availableWidth: size.width, palette: palette)
                .environmentObject(store)
                .accessibilityFocused($articleFocused)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var sceneControls: some View {
        HStack(spacing: 0) {
            if mode == .wide { Color.clear.frame(width: AppMetrics.wideRail + 1) }
            VStack(spacing: 0) {
                HStack(spacing: 0) {
                    previousButton
                    Rectangle().fill(palette.line).frame(width: 1)
                    nextButton
                }
                .frame(height: 52)
                Text("좌우로 넘기거나 아래 버튼으로 장면을 이동하세요.")
                    .font(.system(size: 9))
                    .tracking(0.2)
                    .foregroundColor(palette.muted)
                    .frame(height: 24)
                    .uxTagged(.ux029, show: store.showAnnotations)
            }
            .frame(maxWidth: mode == .wide ? AppMetrics.wideReadingColumn : .infinity)
            if mode == .wide { Spacer() }
        }
        .frame(height: AppMetrics.controlsHeight)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .background(palette.paper)
    }

    private var previousButton: some View {
        Button(action: store.previousScene) {
            HStack {
                Image(systemName: "arrow.left")
                Text("이전 장면")
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 17)
            .contentShape(Rectangle())
        }
        .buttonStyle(SceneNavigationButtonStyle(palette: palette))
        .disabled(store.sceneIndex == 0)
        .uxTagged(.ux027, show: store.showAnnotations)
    }

    private var nextButton: some View {
        Button(action: store.nextScene) {
            HStack {
                Text(store.sceneIndex == store.article.scenes.count - 1 ? "기사 마치기" : "다음 장면")
                Spacer(minLength: 0)
                Image(systemName: "arrow.right")
            }
            .padding(.horizontal, 17)
            .contentShape(Rectangle())
        }
        .buttonStyle(SceneNavigationButtonStyle(palette: palette))
        .uxTagged(.ux028, show: store.showAnnotations)
    }

    private var overflowMenu: some View {
        VStack(spacing: 0) {
            menuButton("공유", symbol: "square.and.arrow.up", id: .ux032, action: store.share)
            if !store.isReadingEditorial {
                menuButton("원문 열기", symbol: "arrow.up.right.square", id: .ux033, action: store.openOriginal)
            } else {
                Color.clear.frame(height: 0).uxTagged(.ux033, show: false).accessibilityHidden(true)
            }
#if DEBUG
            if store.showPrototypeTools {
                menuButton(store.isLoadingNewArticle ? "확인하는 중" : "새 기사 불러오기", symbol: "list.bullet", id: .ux090, action: store.requestNewArticle)
                    .disabled(store.isLoadingNewArticle)
                menuButton("보관함", symbol: "books.vertical", id: .ux034) { store.go(to: .library) }
                menuButton("읽기 설정", symbol: "slider.horizontal.3", id: .ux035) { store.go(to: .settings) }
            }
#endif
        }
        .padding(.horizontal, 12)
        .frame(width: 210)
        .background(palette.raised)
        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
        .uxTagged(.ux031, show: store.showAnnotations)
    }

    private func menuButton(_ title: String, symbol: String, id: UXID, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: symbol).frame(width: 20)
                Text(title)
                Spacer()
            }
            .font(.system(size: 12, weight: .semibold))
            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            .contentShape(Rectangle())
            .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        }
        .buttonStyle(.plain)
        .uxTagged(id, show: store.showAnnotations)
    }
}

private struct ReaderSceneView: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let mode: LayoutMode
    let availableWidth: CGFloat
    let palette: AppPalette

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Color.clear.frame(height: 0).id("scene-top")
                ReaderSceneContent(mode: mode, palette: palette)
                    .environmentObject(store)
                    .id("\(store.article.id)-\(store.sceneIndex)")
            }
            .scrollIndicators(.visible)
            .simultaneousGesture(
                DragGesture(minimumDistance: 28).onEnded { value in
                    let horizontal = value.translation.width
                    let vertical = value.translation.height
                    guard abs(horizontal) > abs(vertical) * 1.25 else { return }
                    if horizontal < -54 { store.nextScene() }
                    else if horizontal > 54 { store.previousScene() }
                }
            )
            .onChange(of: store.sceneIndex) { _ in
                proxy.scrollTo("scene-top", anchor: .top)
            }
        }
        .background(palette.paper)
        .accessibilityAction(named: "이전 장면", store.previousScene)
        .accessibilityAction(named: "다음 장면", store.nextScene)
    }
}

private struct ReaderSceneContent: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let mode: LayoutMode
    let palette: AppPalette
    @State private var revealed = false

    private var motionEnabled: Bool { store.settings.motionEnabled && !reduceMotion }
    private var inset: CGFloat { mode == .compact ? AppMetrics.compactInset : AppMetrics.splitInset }
    private var roleToken: SceneRoleToken { SceneRoleToken(role: store.scene.role, fallbackLabel: store.scene.label) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            RoleMarker(token: roleToken, palette: palette)
                .padding(.bottom, 22)

            Text(store.scene.title)
                .font(.readerHeadline(mode: mode))
                .tracking(mode == .wide ? -2.8 : -2.2)
                .lineSpacing(-2)
                .foregroundColor(palette.ink)
                .fixedSize(horizontal: false, vertical: true)
                .uxTagged(.ux020, show: store.showAnnotations)

            if !store.scene.summary.isEmpty {
                Text(store.scene.summary)
                    .font(.system(size: 18))
                    .tracking(-0.4)
                    .foregroundColor(palette.inkSoft)
                    .lineSpacing(6)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 20)
                    .uxTagged(.ux021, show: store.showAnnotations)
            } else {
                Color.clear.frame(height: 0).uxTagged(.ux021, show: false).accessibilityHidden(true)
            }

            Text("\(store.article.source)  ·  \(store.article.readTime)  ·  \(store.article.date)")
                .font(.system(size: 11))
                .foregroundColor(palette.muted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 20)
                .uxTagged(.ux022, show: store.showAnnotations)

            sceneContent
                .padding(.top, 24)
        }
        .frame(maxWidth: mode == .wide ? AppMetrics.wideContent : .infinity, alignment: .leading)
        .padding(.horizontal, mode == .wide ? 30 : inset)
        .padding(.top, 27)
        .padding(.bottom, 36)
        .frame(maxWidth: .infinity, alignment: .center)
        .opacity(revealed ? 1 : (motionEnabled ? 0 : 1))
        .offset(y: revealed || !motionEnabled ? 0 : 6)
        .onAppear {
            if motionEnabled {
                withAnimation(SwissMotion.easeOut()) { revealed = true }
            } else {
                revealed = true
            }
        }
    }

    @ViewBuilder
    private var sceneContent: some View {
        if let visualization = store.scene.visualization {
            ArticleChartView(
                visualization: visualization,
                mode: mode,
                palette: palette,
                motionEnabled: motionEnabled
            )
        }

        if store.scene.remoteImageURL != nil || store.scene.artwork != nil {
            ArticleMediaView(
                url: store.scene.remoteImageURL,
                fallback: store.scene.artwork ?? .tidal,
                accessibilityLabel: store.scene.artworkLabel.isEmpty ? store.scene.title : store.scene.artworkLabel,
                mediaSaver: store.settings.mediaSaver,
                offline: store.isOffline,
                palette: palette,
                contentMode: .fit,
                preservesNaturalRatio: true,
                cache: store.mediaCache
            )
            .mask(alignment: .top) {
                Rectangle().scaleEffect(y: revealed || !motionEnabled ? 1 : 0.001, anchor: .top)
            }
            .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
            .uxTagged(.ux023, show: store.showAnnotations)

            Text(store.scene.caption)
                .font(.system(size: 10))
                .foregroundColor(palette.muted)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 9)
                .uxTagged(.ux024, show: store.showAnnotations)
        } else {
            Color.clear.frame(height: 0).uxTagged(.ux023, show: false).accessibilityHidden(true)
            Color.clear.frame(height: 0).uxTagged(.ux024, show: false).accessibilityHidden(true)
        }

        if store.scene.visualization == nil,
           store.scene.remoteImageURL == nil,
           store.scene.artwork == nil,
           store.scene.body.isEmpty,
           store.scene.secondary.isEmpty,
           store.scene.quote.isEmpty {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.circle").font(.system(size: 22))
                Text("새로운 형식의 장면입니다. 원문 의미를 잃지 않도록 텍스트 기록으로 표시합니다.")
                    .font(.system(size: 14))
                    .lineSpacing(5)
            }
            .foregroundColor(palette.muted)
            .padding(.vertical, 20)
            .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
            .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
            .accessibilityAddTraits(.isStaticText)
        }

        bodyCopy
        stalenessNote
        pullQuote
    }

    @ViewBuilder
    private var bodyCopy: some View {
        if !store.scene.body.isEmpty || !store.scene.secondary.isEmpty {
            VStack(alignment: .leading, spacing: 15) {
                if !store.scene.body.isEmpty { Text(store.scene.body) }
                if !store.scene.secondary.isEmpty { Text(store.scene.secondary) }
            }
            .font(.readerBody(scale: store.settings.typeSize.scale))
            .tracking(ReaderTypeface.bodyTracking)
            .foregroundColor(palette.ink)
            .lineSpacing(store.settings.leading.spacing)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 22)
            .uxTagged(.ux025, show: store.showAnnotations)
        } else {
            Color.clear.frame(height: 0).uxTagged(.ux025, show: false).accessibilityHidden(true)
        }
    }

    @ViewBuilder
    private var stalenessNote: some View {
        if !store.scene.stalenessNote.isEmpty {
            // Existing caption style, not a badge or a panel: the note is a
            // sentence beside the prose, not an indicator on top of it.
            Text(store.scene.stalenessNote)
                .font(.system(size: 10))
                .foregroundColor(palette.muted)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 9)
                .accessibilityLabel("수치 시효, \(store.scene.stalenessNote)")
        }
    }

    @ViewBuilder
    private var pullQuote: some View {
        if !store.scene.quote.isEmpty {
            Text(store.scene.quote)
                .font(.system(size: 21, weight: .semibold))
                .tracking(-0.8)
                .foregroundColor(palette.accentDeep)
                .lineSpacing(7)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 18)
                .overlay(alignment: .top) { Rectangle().fill(palette.accent).frame(height: 3) }
                .padding(.top, 24)
                .scaleEffect(y: revealed || !motionEnabled ? 1 : 0.82, anchor: .top)
                .opacity(revealed || !motionEnabled ? 1 : 0)
                .uxTagged(.ux026, show: store.showAnnotations)
        } else {
            Color.clear.frame(height: 0).uxTagged(.ux026, show: false).accessibilityHidden(true)
        }
    }
}

private struct ContextPanel: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: mode == .wide ? .center : .top) {
                if mode == .wide {
                    ReaderBackNavigationButton(
                        palette: palette,
                        showAnnotations: store.showAnnotations,
                        action: store.returnToReaderOrigin
                    )
                }
                Text("ARTICLE MAP")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .tracking(1.3)
                    .uxTagged(.ux014, show: store.showAnnotations)
                Spacer()
                if mode != .wide {
                    Button { store.showContext = false } label: {
                        Image(systemName: "xmark").frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("기사 맥락 닫기")
                    .offset(y: -14)
                }
            }
            .frame(height: 44, alignment: .top)
            .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }

            VStack(alignment: .leading, spacing: 4) {
                Text("출처")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundColor(palette.warm)
                Text(store.article.source)
                    .font(.system(size: 22, weight: .bold))
                    .tracking(-0.8)
                Text(store.article.domain)
                Text(store.article.publishedContextText)
            }
            .font(.system(size: 11))
            .foregroundColor(palette.muted)
            .padding(.vertical, 18)
            .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
            .uxTagged(.ux015, show: store.showAnnotations)

            ScrollView {
                VStack(spacing: 0) {
                    ForEach(Array(store.article.scenes.enumerated()), id: \.offset) { index, scene in
                        Button { store.jump(to: index) } label: {
                            HStack(spacing: 6) {
                                Text(String(format: "%02d", index + 1))
                                    .font(.system(size: 10, design: .monospaced))
                                    .frame(width: 28, alignment: .leading)
                                Text(scene.label)
                                    .font(.system(size: 10, weight: .bold))
                                    .frame(width: 48, alignment: .leading)
                                Text(scene.title)
                                    .font(.system(size: 11))
                                    .foregroundColor(index == store.sceneIndex ? palette.accentDeep : palette.muted)
                                    .lineLimit(1)
                                Spacer(minLength: 0)
                            }
                            .foregroundColor(index == store.sceneIndex ? palette.accentDeep : palette.ink)
                            .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
                            .contentShape(Rectangle())
                            .overlay(alignment: .bottom) {
                                Rectangle().fill(index == store.sceneIndex ? palette.accent : palette.line).frame(height: 1)
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(index == store.sceneIndex ? .isSelected : AccessibilityTraits())
                    }
                }
                .uxTagged(.ux016, show: store.showAnnotations)
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 0), GridItem(.flexible(), spacing: 0)], spacing: 0) {
                if mode == .wide {
                    railAction(store.isSaved ? "저장됨" : "기사 저장", symbol: store.isSaved ? "bookmark.fill" : "bookmark", id: .ux012, action: store.toggleSaved)
                    railAction("공유", symbol: "square.and.arrow.up", id: .ux032, action: store.share)
                    feedbackRailAction
                }
                if !store.isReadingEditorial {
                    railAction("원문 열기", symbol: "arrow.up.right.square", id: .ux017, action: store.openOriginal)
                } else {
                    Color.clear.frame(height: 0).uxTagged(.ux017, show: false).accessibilityHidden(true)
                }
#if DEBUG
                if store.showPrototypeTools {
                    railAction(store.isLoadingNewArticle ? "확인 중" : "새 기사", symbol: "list.bullet", id: .ux090, action: store.requestNewArticle)
                        .disabled(store.isLoadingNewArticle)
                    railAction("보관함", symbol: "books.vertical", id: .ux018) { store.go(to: .library) }
                    railAction("읽기 설정", symbol: "slider.horizontal.3", id: .ux019) { store.go(to: .settings) }
                }
#endif
            }
            .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        }
        .padding(.horizontal, mode == .wide ? 20 : 18)
        .padding(.top, mode == .wide ? 24 : 18)
        .padding(.bottom, 14)
        .frame(maxHeight: .infinity)
        .background(mode == .wide ? palette.deep : palette.raised)
        .overlay { if mode != .wide { Rectangle().stroke(palette.ruleStrong, lineWidth: 1) } }
    }

    private func railAction(_ title: String, symbol: String, id: UXID, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol).font(.system(size: 15))
                Text(title).lineLimit(1)
            }
            .font(.system(size: 10, weight: .semibold))
            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
            .overlay(Rectangle().stroke(palette.line, lineWidth: 0.5))
        }
        .buttonStyle(.plain)
        .uxTagged(id, show: store.showAnnotations)
    }

    private var feedbackRailAction: some View {
        Button(action: store.presentArticleFeedback) {
            HStack(spacing: 7) {
                Text("😟")
                    .font(.system(size: 15))
                Text("기사 의견")
                    .lineLimit(1)
            }
            .font(.system(size: 10, weight: .semibold))
            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
            .overlay(Rectangle().stroke(palette.line, lineWidth: 0.5))
        }
        .buttonStyle(.plain)
        .disabled(store.isSubmittingArticleFeedback)
        .accessibilityLabel("이 기사 피드백")
        .accessibilityValue(feedbackAccessibilityValue)
        .accessibilityIdentifier("ARTICLE-FEEDBACK-ACTION")
    }

    private var feedbackAccessibilityValue: String {
        if store.isSubmittingArticleFeedback { return "보내는 중" }
        if let reason = store.currentArticleFeedbackReason {
            return "보낸 의견, \(reason.displayName)"
        }
        return "선택 안 함"
    }
}

private struct SceneNavigationButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.appMotionEnabled) private var motionEnabled
    let palette: AppPalette

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .bold))
            .foregroundColor(isEnabled ? palette.accentDeep : palette.muted)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(configuration.isPressed ? palette.deep : Color.clear)
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.34)
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: configuration.isPressed)
    }
}
