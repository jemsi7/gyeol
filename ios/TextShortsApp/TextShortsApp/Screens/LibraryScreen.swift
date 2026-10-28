import SwiftUI

struct LibraryScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        VStack(spacing: 0) {
            HubHeader(
                kicker: "ARCHIVE / SAVED",
                title: "보관함",
                detail: "저장한 기사를 다시 읽습니다",
                titleID: .ux048,
                showAnnotations: store.showAnnotations,
                palette: palette
            )
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    savedCount
                    if store.savedArticles.isEmpty { emptyState } else { savedList }
                }
                .frame(maxWidth: 840, alignment: .leading)
                .padding(.horizontal, mode == .compact ? 24 : 40)
                .padding(.top, 24)
                .frame(maxWidth: .infinity)
            }
        }
        .background(palette.paper)
        .overlay(alignment: .bottom) {
            if let message = store.toastMessage {
                ToastView(message: message, palette: palette).padding(.horizontal, 24).padding(.bottom, 14)
            }
        }
        .screenTagged(.scr04, show: store.showAnnotations)
    }

#if DEBUG
    // Production navigation is owned by PrimaryBottomNavigation.
    private var backButton: some View {
        Button(action: store.goHome) {
            Image(systemName: "arrow.left").frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("읽기 홈으로 돌아가기")
        .uxTagged(.ux047, show: store.showAnnotations)
    }

    private var settingsButton: some View {
        Button { store.go(to: .settings) } label: {
            HStack(spacing: 4) {
                Image(systemName: "slider.horizontal.3")
                Text("설정").font(.system(size: 10, weight: .semibold))
            }
            .foregroundColor(palette.accentDeep)
            .frame(minHeight: 44)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("읽기 설정")
        .uxTagged(.ux057, show: store.showAnnotations)
    }
#endif

    private var savedCount: some View {
        HStack(alignment: .lastTextBaseline, spacing: 10) {
            Text(String(format: "%02d", store.savedArticles.count))
                .font(.system(size: 34, weight: .medium, design: .monospaced))
                .foregroundColor(palette.accentDeep)
            Text("저장한 기사")
                .font(.system(size: 11))
                .foregroundColor(palette.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .uxTagged(.ux049, show: store.showAnnotations)
    }

    private var savedList: some View {
        LazyVStack(spacing: 0) {
            ForEach(store.savedArticles) { article in
                let scene = article.scenes.first ?? store.scene
                HStack(alignment: .top, spacing: 12) {
                    ArticleMediaView(
                        url: scene.remoteImageURL,
                        fallback: scene.artwork ?? .tidal,
                        accessibilityLabel: scene.artworkLabel.isEmpty ? scene.title : scene.artworkLabel,
                        mediaSaver: store.settings.mediaSaver,
                        offline: store.isOffline,
                        palette: palette,
                        thumbnailArticleID: article.id,
                        cache: store.mediaCache
                    )
                    .frame(width: mode == .compact ? 88 : 120, height: mode == .compact ? 108 : 124)
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                    .clipped()
                    .uxTagged(.ux051, show: store.showAnnotations)

                    VStack(alignment: .leading, spacing: 0) {
                        Text("\(article.source) · \(store.savedDateText(for: article.id))")
                            .font(.system(size: 10))
                            .foregroundColor(palette.muted)
                            .uxTagged(.ux052, show: store.showAnnotations)
                        Text(scene.title)
                            .font(.system(size: 17, weight: .bold))
                            .tracking(-0.6)
                            .lineLimit(3)
                            .padding(.top, 7)
                            .uxTagged(.ux053, show: store.showAnnotations)
                        Button { store.openSaved(article) } label: {
                            HStack(spacing: 5) {
                                Text("이어서 읽기")
                                Image(systemName: "arrow.right")
                            }
                            .font(.system(size: 11, weight: .bold))
                            .foregroundColor(palette.accentDeep)
                            .frame(minHeight: 44)
                        }
                        .buttonStyle(.plain)
                        .uxTagged(.ux054, show: store.showAnnotations)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    Button { store.removeSaved(articleID: article.id) } label: {
                        Image(systemName: "trash")
                            .font(.system(size: 17))
                            .foregroundColor(palette.muted)
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(scene.title) 저장 제거")
                    .uxTagged(.ux055, show: store.showAnnotations)
                }
                .padding(.vertical, 14)
                .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
            }
        }
        .uxTagged(.ux050, show: store.showAnnotations)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 0) {
            Image(systemName: "doc.plaintext")
                .font(.system(size: 42, weight: .light))
                .foregroundColor(palette.accentDeep)
            Text("아직 저장한 기사가 없습니다.")
                .font(.system(size: 27, weight: .bold))
                .tracking(-1.4)
                .padding(.top, 20)
            Text("Reader 상단의 저장 아이콘을 누르면 여기에 모입니다.")
                .font(.system(size: 13))
                .foregroundColor(palette.muted)
                .lineSpacing(4)
                .padding(.top, 8)
            Button { store.selectPrimaryTab(.reader) } label: {
                HStack { Text("기사 읽기"); Spacer(); Image(systemName: "arrow.right") }
            }
            .buttonStyle(PrimaryPillButtonStyle(palette: palette))
            .padding(.top, 24)
        }
        .frame(maxWidth: .infinity, minHeight: 480, alignment: .leading)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .uxTagged(.ux056, show: store.showAnnotations)
    }
}
