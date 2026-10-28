import Foundation
import SwiftUI

struct OnboardingReleaseHighlight: Equatable, Sendable {
    let number: String
    let symbol: String
    let title: String
    let detail: String
}

enum OnboardingReleaseContent {
    static let highlights = [
        OnboardingReleaseHighlight(
            number: "01",
            symbol: "clock.arrow.circlepath",
            title: "달라진 수치 대조",
            detail: "블록체인·크립토 기사의 수치가 크게 달라졌으면 지금 값을 함께 보여 줍니다."
        ),
        OnboardingReleaseHighlight(
            number: "02",
            symbol: "newspaper",
            title: "발행 맥락 한 줄",
            detail: "발행 주체가 확인된 기사는 어디서 발행됐는지 한 줄로 함께 표시합니다."
        ),
        OnboardingReleaseHighlight(
            number: "03",
            symbol: "doc.text",
            title: "1차 문서 읽기",
            detail: "거버넌스 제안, 감사 보고서 같은 1차 문서를 기사처럼 이어 읽을 수 있습니다."
        ),
    ]

    static func versionKicker(bundleShortVersion: String?) -> String {
        let version = bundleShortVersion?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return version.isEmpty ? "VERSION" : "VERSION \(version)"
    }
}

struct OnboardingScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @AccessibilityFocusState private var headingFocused: Bool

    let mode: LayoutMode
    let palette: AppPalette

    private var page: OnboardingPage { store.currentOnboardingPage ?? .introReader }
    private var motionEnabled: Bool { store.settings.motionEnabled && !reduceMotion }
    private var isAccountBusy: Bool {
        store.accountStatus == .signingIn || store.accountStatus == .deleting
    }

    var body: some View {
        GeometryReader { proxy in
            if page == .signIn {
                loginStandalone(minHeight: proxy.size.height)
            } else if page == .privacyConsent {
                privacyConsentStandalone(minHeight: proxy.size.height)
            } else {
                ScrollViewReader { scrollProxy in
                    ScrollView {
                        VStack(spacing: 0) {
                            topLine.id("onboarding-top")
                            editorialPage(minHeight: proxy.size.height - 72)
                        }
                        .frame(minHeight: proxy.size.height, alignment: .top)
                        .frame(maxWidth: .infinity)
                    }
                    .scrollIndicators(.hidden)
                    .onChange(of: store.onboardingIndex) { _ in
                        DispatchQueue.main.async {
                            if motionEnabled {
                                withAnimation(SwissMotion.easeOut()) {
                                    scrollProxy.scrollTo("onboarding-top", anchor: .top)
                                }
                            } else {
                                scrollProxy.scrollTo("onboarding-top", anchor: .top)
                            }
                            if voiceOverEnabled { headingFocused = true }
                        }
                    }
                }
            }
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .background(palette.paper)
        .screenTagged(.scr01, show: store.showAnnotations)
    }

    private var topLine: some View {
        HStack(spacing: 12) {
            Text("결")
                .font(.system(size: 38, weight: .heavy))
                .tracking(-5.5)
                .foregroundColor(palette.accentDeep)
                .accessibilityLabel("결")
                .uxTagged(.ux001, show: store.showAnnotations)
            Spacer()
            Text(String(format: "%02d / %02d", store.onboardingIndex + 1, store.onboardingPages.count))
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .tracking(1.2)
                .foregroundColor(palette.muted)
                .accessibilityLabel(
                    "안내 \(store.onboardingIndex + 1)단계, 전체 \(store.onboardingPages.count)단계"
                )
                .uxTagged(.ux091, show: store.showAnnotations)
            Button("건너뛰기", action: store.skipOnboardingIntro)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minWidth: 68, minHeight: AppMetrics.touchTarget)
                .buttonStyle(.plain)
                .uxTagged(.ux093, show: store.showAnnotations)
        }
        .frame(height: 72)
        .overlay(alignment: .bottom) {
            Rectangle().fill(palette.ruleStrong).frame(height: 1)
        }
    }

    private func editorialPage(minHeight: CGFloat) -> some View {
        VStack(spacing: 0) {
            if mode == .compact {
                VStack(alignment: .leading, spacing: 24) {
                    heading
                    pageVisual
                }
                .padding(.top, 30)
            } else {
                HStack(alignment: .center, spacing: mode == .wide ? 84 : 48) {
                    heading.frame(maxWidth: 480, alignment: .leading)
                    pageVisual.frame(maxWidth: 480)
                }
                .frame(maxWidth: 980, minHeight: max(470, minHeight - 132))
                .padding(.top, 26)
            }
            actionBlock
                .frame(maxWidth: 980)
                .padding(.top, mode == .compact ? 26 : 8)
        }
        .frame(maxWidth: .infinity)
        .frame(minHeight: max(minHeight, mode == .compact ? 690 : 560), alignment: .top)
        .id("onboarding-page-\(page.rawValue)")
        .transition(motionEnabled ? .opacity.combined(with: .offset(y: 6)) : .identity)
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(pageKicker)
                .font(.system(size: 12, weight: .bold))
                .tracking(0.4)
                .foregroundColor(palette.warm)
            Text(pageTitle)
                .font(.system(size: mode == .wide ? 66 : mode == .split ? 58 : 47, weight: .heavy))
                .tracking(mode == .wide ? -4.8 : -3.6)
                .lineSpacing(-7)
                .foregroundColor(palette.ink)
                .lineLimit(3)
                .minimumScaleFactor(0.78)
                .accessibilityFocused($headingFocused)
                .modifier(IntroValuePromiseTag(page: page, store: store))
        }
        .frame(maxWidth: 520, alignment: .leading)
    }

    @ViewBuilder
    private var pageVisual: some View {
        switch page {
        case .introReader:
            GesturePreviewArtwork(palette: palette, motionEnabled: motionEnabled)
                .frame(maxWidth: .infinity)
                .frame(height: mode == .compact ? 276 : 360)
                .uxTagged(.ux004, show: store.showAnnotations)
                .accessibilityLabel(
                    "Reader 미리보기. 기사는 여러 장면으로 나뉘고 현재 장면의 역할과 진행을 보여줍니다."
                )
        case .introNavigation:
            orientationBlock
        case .whatsNew:
            releaseHighlights
        case .privacyConsent:
            EmptyView()
        case .signIn:
            EmptyView()
        }
    }

    private var orientationBlock: some View {
        VStack(spacing: 0) {
            orientationRow(
                number: "01",
                symbol: "arrow.left.and.right",
                title: "장면 이동",
                detail: "좌우로 넘기거나 아래 버튼을 누르세요."
            )
            orientationRow(
                number: "02",
                symbol: "list.bullet",
                title: "기사 목차",
                detail: "상단 출처 이름을 누르면 전체 장면이 열립니다."
            )
            orientationRow(
                number: "03",
                symbol: "house",
                title: "읽기 홈",
                detail: "홈 버튼을 누르면 기사·보관함·설정을 다시 찾습니다."
            )
        }
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .uxTagged(.ux094, show: store.showAnnotations)
        .accessibilityElement(children: .contain)
    }

    private var releaseHighlights: some View {
        VStack(spacing: 0) {
            ForEach(OnboardingReleaseContent.highlights, id: \.number) { highlight in
                releaseRow(
                    number: highlight.number,
                    symbol: highlight.symbol,
                    title: highlight.title,
                    detail: highlight.detail
                )
            }
        }
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .uxTagged(.ux104, show: store.showAnnotations)
        .accessibilityElement(children: .contain)
    }

    private func orientationRow(
        number: String,
        symbol: String,
        title: String,
        detail: String
    ) -> some View {
        informationRow(
            number: number,
            symbol: symbol,
            title: title,
            detail: detail,
            symbolColor: palette.accentDeep
        )
    }

    private func releaseRow(
        number: String,
        symbol: String,
        title: String,
        detail: String
    ) -> some View {
        informationRow(
            number: number,
            symbol: symbol,
            title: title,
            detail: detail,
            symbolColor: palette.warm
        )
    }

    private func informationRow(
        number: String,
        symbol: String,
        title: String,
        detail: String,
        symbolColor: Color
    ) -> some View {
        HStack(alignment: .center, spacing: 14) {
            Text(number)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(palette.warm)
                .frame(width: 24, alignment: .leading)
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .medium))
                .foregroundColor(symbolColor)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.system(size: 15, weight: .bold))
                    .foregroundColor(palette.ink)
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundColor(palette.muted)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, minHeight: 84, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(detail)")
    }

    private var actionBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            Rectangle().fill(palette.ruleStrong).frame(height: 1)
            HStack(spacing: 12) {
                if store.onboardingIndex > 0 {
                    Button(action: store.retreatOnboarding) {
                        Label("이전", systemImage: "arrow.left")
                    }
                    .buttonStyle(QuietPillButtonStyle(palette: palette))
                    .uxTagged(.ux092, show: store.showAnnotations)
                }
                if store.onboardingIndex < store.onboardingPages.count - 1 {
                    Button(action: store.advanceOnboarding) {
                        HStack {
                            Text(page == .whatsNew ? "계속" : "다음")
                            Spacer()
                            Image(systemName: "arrow.right")
                        }
                    }
                    .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                    .uxTagged(.ux092, show: store.showAnnotations)
                } else {
                    Button(action: store.advanceOnboarding) {
                        HStack {
                            Text("읽기 홈 보기")
                            Spacer()
                            Image(systemName: "arrow.right")
                        }
                    }
                    .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                    .uxTagged(.ux006, show: store.showAnnotations)
                }
            }
            Text("버전별 한 번 · 언제든 건너뛸 수 있습니다.")
                .font(.system(size: 11))
                .foregroundColor(palette.muted)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .uxTagged(.ux007, show: store.showAnnotations)
        }
        .padding(.bottom, 20)
    }

    private func loginStandalone(minHeight: CGFloat) -> some View {
        ScrollView {
            VStack(spacing: 0) {
                Spacer(minLength: mode == .compact ? 54 : 88)
                Text("결")
                    .font(.system(size: mode == .wide ? 78 : 64, weight: .heavy))
                    .tracking(-9)
                    .foregroundColor(palette.accentDeep)
                    .accessibilityLabel("결")
                Text("계정으로\n읽기를 이어가세요.")
                    .font(.system(size: mode == .wide ? 52 : 42, weight: .heavy))
                    .tracking(-2.8)
                    .multilineTextAlignment(.center)
                    .foregroundColor(palette.ink)
                    .padding(.top, 28)
                Text("Apple로 안전하게 로그인할 수 있습니다.\n로그인하지 않아도 모든 읽기 기능을 사용할 수 있어요.")
                    .font(.system(size: 13))
                    .lineSpacing(5)
                    .multilineTextAlignment(.center)
                    .foregroundColor(palette.muted)
                    .padding(.top, 18)
                Rectangle()
                    .fill(palette.ruleStrong)
                    .frame(height: 1)
                    .padding(.top, 38)
                disclaimerAcknowledgement
                    .padding(.top, 20)
                AppleAccountSignInButton(
                    uxID: .ux105,
                    showAnnotations: store.showAnnotations,
                    isDisabled: isAccountBusy || !store.hasAcknowledgedNewsDisclaimer,
                    onPayload: { payload in
                        Task { await store.completeAppleSignIn(payload: payload) }
                    },
                    onError: store.handleAppleSignInError,
                    onCancel: store.cancelAppleSignIn
                )
                .padding(.top, 18)
                Button("로그인 없이 계속", action: store.continueWithoutAccount)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(
                        store.hasAcknowledgedNewsDisclaimer ? palette.accentDeep : palette.muted
                    )
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .buttonStyle(.plain)
                    .disabled(isAccountBusy || !store.hasAcknowledgedNewsDisclaimer)
                    .uxTagged(.ux106, show: store.showAnnotations)
                if case let .error(message, _) = store.accountStatus {
                    Text(message)
                        .font(.system(size: 11))
                        .foregroundColor(Color.red)
                        .multilineTextAlignment(.center)
                        .padding(.top, 10)
                        .uxTagged(.ux112, show: store.showAnnotations)
                }
                Spacer(minLength: 40)
            }
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
            .frame(minHeight: minHeight, alignment: .center)
        }
        .scrollIndicators(.hidden)
        .accessibilityElement(children: .contain)
    }

    private func privacyConsentStandalone(minHeight: CGFloat) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("결")
                    .font(.system(size: 38, weight: .heavy))
                    .tracking(-5.5)
                    .foregroundColor(palette.accentDeep)
                    .accessibilityLabel("결")
                Text("PRIVACY · VERSION \(PrivacyPolicy.currentVersion)")
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .tracking(0.8)
                    .foregroundColor(palette.warm)
                    .padding(.top, 36)
                Text("읽기 전에\n확인해 주세요.")
                    .font(.system(size: mode == .wide ? 58 : 46, weight: .heavy))
                    .tracking(-3.4)
                    .lineSpacing(-5)
                    .foregroundColor(palette.ink)
                    .padding(.top, 12)
                Text("결은 아래 정보를 이용해 동의 사실을 안전하게 기록합니다. Apple 로그인은 선택입니다.")
                    .font(.system(size: 13))
                    .lineSpacing(5)
                    .foregroundColor(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 18)

                VStack(spacing: 0) {
                    privacySummaryRow(
                        number: "01",
                        title: "기기 확인",
                        detail: "설치 ID와 App Attest 키로 정식 앱 요청인지 확인합니다."
                    )
                    privacySummaryRow(
                        number: "02",
                        title: "선택 로그인",
                        detail: "Apple 로그인 없이도 모든 읽기 기능을 사용할 수 있습니다."
                    )
                    privacySummaryRow(
                        number: "03",
                        title: "동의 기록",
                        detail: "정책 버전, 앱·빌드 버전과 동의 시각을 서버에 남깁니다."
                    )
                }
                .padding(.top, 30)
                .overlay(alignment: .top) {
                    Rectangle()
                        .fill(palette.ruleStrong)
                        .frame(height: 1)
                        .offset(y: 40)
                }
                .overlay(alignment: .bottom) {
                    Rectangle().fill(palette.ruleStrong).frame(height: 1)
                }
                .uxTagged(.ux127, show: store.showAnnotations)

                Link(destination: PrivacyPolicy.url) {
                    HStack(spacing: 8) {
                        Text("개인정보 처리방침 전문 보기")
                        Image(systemName: "arrow.up.right")
                    }
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(palette.accentDeep)
                    .frame(minHeight: AppMetrics.touchTarget)
                }
                .uxTagged(.ux128, show: store.showAnnotations)
                .padding(.top, 12)

                Button {
                    store.setPrivacyConsentSelected(!store.isPrivacyConsentSelected)
                } label: {
                    HStack(spacing: 11) {
                        Image(
                            systemName: store.isPrivacyConsentSelected
                                ? "checkmark.square.fill"
                                : "square"
                        )
                        .font(.system(size: 22, weight: .medium))
                        .foregroundColor(
                            store.isPrivacyConsentSelected ? palette.accentDeep : palette.muted
                        )
                        Text("개인정보 처리방침에 동의합니다.")
                            .font(.system(size: 13, weight: .bold))
                            .foregroundColor(palette.ink)
                        Spacer(minLength: 0)
                    }
                    .frame(
                        maxWidth: .infinity,
                        minHeight: AppMetrics.touchTarget,
                        alignment: .leading
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(privacyConsentIsSaving)
                .accessibilityValue(store.isPrivacyConsentSelected ? "선택됨" : "선택 안 됨")
                .accessibilityHint("선택한 뒤 동의하고 계속 버튼을 누르세요.")
                .accessibilityAddTraits(store.isPrivacyConsentSelected ? .isSelected : [])
                .uxTagged(.ux129, show: store.showAnnotations)
                .padding(.top, 10)

                Button {
                    Task { await store.acceptPrivacyPolicy() }
                } label: {
                    HStack {
                        Text(privacyConsentIsSaving ? "동의 기록 중…" : "동의하고 계속")
                        Spacer()
                        if privacyConsentIsSaving {
                            ProgressView().tint(palette.paper)
                        } else {
                            Image(systemName: "arrow.right")
                        }
                    }
                }
                .buttonStyle(PrimaryPillButtonStyle(palette: palette))
                .disabled(!store.isPrivacyConsentSelected || privacyConsentIsSaving)
                .opacity(!store.isPrivacyConsentSelected || privacyConsentIsSaving ? 0.48 : 1)
                .uxTagged(.ux130, show: store.showAnnotations)
                .padding(.top, 16)

                if case let .failed(message) = store.privacyConsentState {
                    Text(message)
                        .font(.system(size: 11))
                        .lineSpacing(3)
                        .foregroundColor(.red)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("PRIVACY-CONSENT-ERROR")
                        .padding(.top, 12)
                }
            }
            .frame(maxWidth: 600, alignment: .leading)
            .frame(maxWidth: .infinity, minHeight: minHeight, alignment: .center)
            .padding(.vertical, mode == .compact ? 36 : 64)
        }
        .scrollIndicators(.hidden)
    }

    private var privacyConsentIsSaving: Bool {
        store.privacyConsentState == .saving
    }

    private func privacySummaryRow(
        number: String,
        title: String,
        detail: String
    ) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Text(number)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(palette.warm)
                .frame(width: 24, alignment: .leading)
            VStack(alignment: .leading, spacing: 5) {
                Text(title)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(palette.ink)
                Text(detail)
                    .font(.system(size: 11))
                    .lineSpacing(3)
                    .foregroundColor(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .accessibilityElement(children: .combine)
    }

    private var disclaimerAcknowledgement: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("본 서비스에서 제공하는 소식은 오직 참고용이며 사실과 다를 수 있습니다. 중요한 의사결정에 사용하지 마십시오.")
                .font(.system(size: 12))
                .lineSpacing(4)
                .foregroundColor(palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                store.setNewsDisclaimerAcknowledged(!store.hasAcknowledgedNewsDisclaimer)
            } label: {
                HStack(spacing: 10) {
                    Image(
                        systemName: store.hasAcknowledgedNewsDisclaimer
                            ? "checkmark.square.fill"
                            : "square"
                    )
                    .font(.system(size: 21, weight: .medium))
                    .foregroundColor(
                        store.hasAcknowledgedNewsDisclaimer ? palette.accentDeep : palette.muted
                    )
                    Text("이해했습니다.")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(palette.ink)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, minHeight: AppMetrics.touchTarget, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isAccountBusy)
            .accessibilityLabel("이해했습니다.")
            .accessibilityValue(store.hasAcknowledgedNewsDisclaimer ? "선택됨" : "선택 안 됨")
            .accessibilityHint("선택해야 로그인하거나 로그인 없이 계속할 수 있습니다.")
            .accessibilityAddTraits(store.hasAcknowledgedNewsDisclaimer ? .isSelected : [])
            .accessibilityIdentifier("ONBOARDING-DISCLAIMER-ACKNOWLEDGEMENT")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var pageKicker: String {
        switch page {
        case .introReader: return "EDITORIAL READER"
        case .introNavigation: return "THREE PLACES"
        case .whatsNew:
            let shortVersion = Bundle.main.object(
                forInfoDictionaryKey: "CFBundleShortVersionString"
            ) as? String
            return OnboardingReleaseContent.versionKicker(bundleShortVersion: shortVersion)
        case .privacyConsent: return "PRIVACY"
        case .signIn: return "ACCOUNT"
        }
    }

    private var pageTitle: String {
        switch page {
        case .introReader: return "긴 기사를\n장면으로\n읽습니다."
        case .introNavigation: return "읽는 길은\n세 곳만\n기억하세요."
        case .whatsNew: return "이번 버전에서\n달라진\n세 가지."
        case .privacyConsent: return "읽기 전에\n확인해\n주세요."
        case .signIn: return "계정으로\n읽기를\n이어가세요."
        }
    }
}

private struct IntroValuePromiseTag: ViewModifier {
    let page: OnboardingPage
    @ObservedObject var store: PrototypeStore

    func body(content: Content) -> some View {
        if page == .introReader {
            content
                .accessibilityLabel("긴 기사를 장면으로 읽습니다")
                .uxTagged(.ux002, show: store.showAnnotations)
        } else {
            content
        }
    }
}
