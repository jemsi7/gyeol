import SwiftUI

struct LoadingScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        VStack(spacing: 0) {
            EditorialTopline(code: "SYNC / 01", palette: palette) { EmptyView() }
            VStack(alignment: .leading, spacing: 0) {
                Text("새 기사를 준비하는 중")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(palette.accentDeep)
                    .padding(.bottom, 26)
                    .accessibilityAddTraits(.updatesFrequently)
                    .uxTagged(.ux068, show: store.showAnnotations)
                VStack(alignment: .leading, spacing: 10) {
                    SkeletonBlock(palette: palette, reduceMotion: reduceMotion).frame(height: 31)
                    SkeletonBlock(palette: palette, reduceMotion: reduceMotion).frame(maxWidth: 0.74 * 620).frame(height: 31)
                }
                .uxTagged(.ux069, show: store.showAnnotations)
                SkeletonBlock(palette: palette, reduceMotion: reduceMotion)
                    .frame(height: mode == .compact ? 248 : 320)
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                    .padding(.vertical, 28)
                    .uxTagged(.ux070, show: store.showAnnotations)
                VStack(alignment: .leading, spacing: 10) {
                    skeletonLine(1.0)
                    skeletonLine(0.92)
                    skeletonLine(0.78)
                    skeletonLine(0.88)
                }
                .uxTagged(.ux071, show: store.showAnnotations)
                Spacer(minLength: 24)
            }
            .frame(maxWidth: 720)
            .padding(.top, 34)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .background(palette.paper)
        .screenTagged(.scr06, show: store.showAnnotations)
    }

    private func skeletonLine(_ fraction: CGFloat) -> some View {
        GeometryReader { proxy in
            SkeletonBlock(palette: palette, reduceMotion: reduceMotion)
                .frame(width: proxy.size.width * fraction, height: 13)
        }
        .frame(height: 13)
    }
}

struct EmptyFeedScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        SwissStateScreen(
            code: "FEED / 00",
            index: "07 / EMPTY",
            symbol: "newspaper",
            symbolID: .ux072,
            title: "지금은 새 기사가 없습니다.",
            titleID: .ux073,
            message: "마지막 확인 · \(store.lastCheckedText)",
            bodyID: .ux074,
            mode: mode,
            palette: palette,
            showAnnotations: store.showAnnotations,
            trailing: { EmptyView() }
        ) {
            Button(action: store.retryLoad) {
                HStack { Text("다시 확인"); Spacer(); Image(systemName: "arrow.right") }
            }
            .buttonStyle(PrimaryPillButtonStyle(palette: palette))
            .uxTagged(.ux075, show: store.showAnnotations)
            Button { store.go(to: .library) } label: {
                Label("보관함 읽기", systemImage: "books.vertical")
            }
            .buttonStyle(QuietPillButtonStyle(palette: palette))
            .uxTagged(.ux076, show: store.showAnnotations)
        }
        .screenTagged(.scr07, show: store.showAnnotations)
    }
}

struct OfflineScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        SwissStateScreen(
            code: "",
            index: "08 / OFFLINE",
            symbol: "icloud.slash",
            symbolID: .ux077,
            title: store.offlineTitle,
            titleID: .ux078,
            message: store.offlineMessage,
            bodyID: .ux079,
            mode: mode,
            palette: palette,
            showAnnotations: store.showAnnotations,
            trailing: {
                Button(action: store.dismissOffline) {
                    Image(systemName: "arrow.left").frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("오프라인 안내 닫기")
                .uxTagged(.ux082, show: store.showAnnotations)
            }
        ) {
            if store.canResumeOffline {
                Button(action: store.resumeOffline) {
                    HStack { Text("저장 기사 읽기"); Spacer(); Image(systemName: "arrow.right") }
                }
                .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                .uxTagged(.ux080, show: store.showAnnotations)
            } else {
                Color.clear.frame(height: 0).uxTagged(.ux080, show: false).accessibilityHidden(true)
            }
            Button("연결 다시 확인", action: store.retryLoad)
                .buttonStyle(QuietPillButtonStyle(palette: palette))
                .uxTagged(.ux081, show: store.showAnnotations)
        }
        .screenTagged(.scr08, show: store.showAnnotations)
    }
}

struct AttestErrorScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        VStack(spacing: 0) {
            EditorialTopline(code: "VERIFY / 02", palette: palette) { EmptyView() }
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Image(systemName: "exclamationmark.shield")
                        .font(.system(size: 54, weight: .light))
                        .foregroundColor(palette.accentDeep)
                        .uxTagged(.ux083, show: store.showAnnotations)
                    Text("09 / INTEGRITY")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .tracking(1.2)
                        .foregroundColor(palette.warm)
                        .padding(.top, 22)
                    Text("앱을 다시 확인해 주세요.")
                        .font(.system(size: mode == .wide ? 44 : 34, weight: .bold))
                        .tracking(-2)
                        .lineSpacing(-2)
                        .padding(.top, 10)
                        .uxTagged(.ux084, show: store.showAnnotations)
                    Text("기사와 저장 항목은 지워지지 않았습니다. 연결을 확인한 뒤 앱 확인을 다시 시도해 주세요.")
                        .font(.system(size: 13))
                        .foregroundColor(palette.muted)
                        .lineSpacing(5)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 18)
                        .uxTagged(.ux085, show: store.showAnnotations)
                    Button(action: store.retryAttest) {
                        HStack {
                            Text(store.isAttestRetrying ? "확인하는 중" : "앱 다시 확인")
                            Spacer()
                            Image(systemName: "arrow.right")
                        }
                    }
                    .disabled(store.isAttestRetrying)
                    .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                    .padding(.top, 28)
                    .uxTagged(.ux086, show: store.showAnnotations)
                    technicalDetails
                    Button("읽기 설정") { store.go(to: .settings) }
                        .buttonStyle(QuietPillButtonStyle(palette: palette))
                        .padding(.top, 10)
                        .uxTagged(.ux089, show: store.showAnnotations)
                }
                .frame(maxWidth: 680, alignment: .leading)
                .padding(.top, mode == .compact ? 156 : 120)
                .padding(.bottom, 36)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .background(palette.paper)
        .screenTagged(.scr09, show: store.showAnnotations)
    }

    private var technicalDetails: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.showTechnicalDetails.toggle() } label: {
                HStack {
                    Image(systemName: store.showTechnicalDetails ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9))
                    Text("기술 상세")
                    Spacer()
                }
                .font(.system(size: 11))
                .foregroundColor(palette.muted)
                .frame(minHeight: 44)
            }
            .buttonStyle(.plain)
            if store.showTechnicalDetails {
                Text(store.supportCode)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(palette.warm)
                    .textSelection(.enabled)
                    .padding(.bottom, 12)
                    .uxTagged(.ux088, show: store.showAnnotations)
            } else {
                Color.clear.frame(height: 0).uxTagged(.ux088, show: false).accessibilityHidden(true)
            }
        }
        .overlay(alignment: .top) { Rectangle().fill(palette.line).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .padding(.top, 12)
        .uxTagged(.ux087, show: store.showAnnotations)
    }
}

private struct SwissStateScreen<Trailing: View, Actions: View>: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.appMotionEnabled) private var motionEnabled
    let code: String
    let index: String
    let symbol: String
    let symbolID: UXID
    let title: String
    let titleID: UXID
    let message: String
    let bodyID: UXID
    let mode: LayoutMode
    let palette: AppPalette
    let showAnnotations: Bool
    @ViewBuilder let trailing: Trailing
    @ViewBuilder let actions: Actions
    @State private var visible = false

    var body: some View {
        VStack(spacing: 0) {
            EditorialTopline(code: code, palette: palette) { trailing }
            VStack(alignment: .leading, spacing: 0) {
                Image(systemName: symbol)
                    .font(.system(size: 54, weight: .light))
                    .foregroundColor(palette.accentDeep)
                    .uxTagged(symbolID, show: showAnnotations)
                Text(index)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .tracking(1.2)
                    .foregroundColor(palette.warm)
                    .padding(.top, 22)
                Text(title)
                    .font(.system(size: mode == .wide ? 44 : 34, weight: .bold))
                    .tracking(-2)
                    .lineSpacing(-2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 10)
                    .uxTagged(titleID, show: showAnnotations)
                Text(message)
                    .font(.system(size: 13))
                    .foregroundColor(palette.muted)
                    .lineSpacing(5)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 18)
                    .uxTagged(bodyID, show: showAnnotations)
                VStack(spacing: 8) { actions }
                    .padding(.top, 30)
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(.top, mode == .compact ? 152 : 110)
            .opacity(visible || !motionEnabled || reduceMotion ? 1 : 0)
            .offset(y: visible || !motionEnabled || reduceMotion ? 0 : 6)
            .onAppear {
                if reduceMotion || !motionEnabled { visible = true }
                else { withAnimation(SwissMotion.easeOut()) { visible = true } }
            }
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .background(palette.paper)
    }
}
