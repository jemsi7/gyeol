import ArticleFeedKit
import SwiftUI
import UIKit

struct AppPalette {
    let paper: Color
    let raised: Color
    let deep: Color
    let ink: Color
    let inkSoft: Color
    let muted: Color
    let line: Color
    let ruleStrong: Color
    let accent: Color
    let accentDeep: Color
    let accentSoft: Color
    let warm: Color
    let danger: Color
    let focus: Color

    var chartColors: [Color] {
        [accent, warm, inkSoft, muted]
    }

    init(_ scheme: ColorScheme) {
        if scheme == .dark {
            let darkInk = Color(red: 0.953, green: 0.937, blue: 0.894)
            paper = Color(red: 0.067, green: 0.090, blue: 0.086)
            raised = Color(red: 0.090, green: 0.118, blue: 0.110)
            deep = Color(red: 0.137, green: 0.169, blue: 0.157)
            ink = darkInk
            inkSoft = Color(red: 0.833, green: 0.820, blue: 0.780)
            muted = Color(red: 0.639, green: 0.667, blue: 0.647)
            line = darkInk.opacity(0.24)
            ruleStrong = darkInk.opacity(0.52)
            accent = Color(red: 0.263, green: 0.718, blue: 0.690)
            accentDeep = Color(red: 0.553, green: 0.855, blue: 0.824)
            accentSoft = Color(red: 0.090, green: 0.239, blue: 0.227)
            warm = Color(red: 0.949, green: 0.467, blue: 0.294)
            danger = Color(red: 0.949, green: 0.467, blue: 0.294)
            focus = Color(red: 0.263, green: 0.718, blue: 0.690)
        } else {
            let lightInk = Color(red: 0.067, green: 0.090, blue: 0.086)
            paper = Color(red: 0.953, green: 0.937, blue: 0.894)
            raised = Color(red: 0.980, green: 0.969, blue: 0.937)
            deep = Color(red: 0.906, green: 0.882, blue: 0.824)
            ink = lightInk
            inkSoft = Color(red: 0.318, green: 0.349, blue: 0.333)
            muted = Color(red: 0.400, green: 0.427, blue: 0.412)
            line = lightInk.opacity(0.24)
            ruleStrong = lightInk.opacity(0.52)
            accent = Color(red: 0.000, green: 0.545, blue: 0.525)
            accentDeep = Color(red: 0.000, green: 0.412, blue: 0.396)
            accentSoft = Color(red: 0.855, green: 0.914, blue: 0.890)
            warm = Color(red: 0.937, green: 0.353, blue: 0.165)
            danger = Color(red: 0.714, green: 0.204, blue: 0.137)
            focus = Color(red: 0.000, green: 0.659, blue: 0.631)
        }
    }
}

enum AppMetrics {
    static let dockHeight: CGFloat = 62
    static let compactInset: CGFloat = 30
    static let splitInset: CGFloat = 40
    static let wideRail: CGFloat = 280
    static let wideReadingColumn: CGFloat = 720
    static let wideContent: CGFloat = 660
    static let headerHeight: CGFloat = 78
    static let controlsHeight: CGFloat = 76
    static let primaryNavigationHeight: CGFloat = 54
    static let touchTarget: CGFloat = 44
    static let cornerRadius: CGFloat = 2
}

enum SwissMotion {
    static let fast: Double = 0.16
    static let medium: Double = 0.22
    static let completion: Double = 0.52

    static func easeOut(_ duration: Double = medium) -> Animation {
        .timingCurve(0.20, 0.80, 0.20, 1.00, duration: duration)
    }
}

private struct AppMotionEnabledKey: EnvironmentKey {
    static let defaultValue = true
}

extension EnvironmentValues {
    var appMotionEnabled: Bool {
        get { self[AppMotionEnabledKey.self] }
        set { self[AppMotionEnabledKey.self] = newValue }
    }
}

struct SceneRoleToken {
    let short: String
    let label: String
    let tone: Tone

    enum Tone { case teal, orange, ink, muted }

    init(role: ArticleSceneRole?, fallbackLabel: String) {
        switch role {
        case .lead:
            short = "01"; label = fallbackLabel.isEmpty ? "시작" : fallbackLabel; tone = .teal
        case .context:
            short = "02"; label = fallbackLabel.isEmpty ? "맥락" : fallbackLabel; tone = .orange
        case .evidence:
            short = "03"; label = fallbackLabel.isEmpty ? "근거" : fallbackLabel; tone = .teal
        case .impact:
            short = "04"; label = fallbackLabel.isEmpty ? "영향" : fallbackLabel; tone = .ink
        case .close:
            short = "05"; label = fallbackLabel.isEmpty ? "결론" : fallbackLabel; tone = .teal
        case .unsupported, .none:
            short = "—"; label = fallbackLabel.isEmpty ? "기록" : fallbackLabel; tone = .muted
        }
    }

    func color(_ palette: AppPalette) -> Color {
        switch tone {
        case .teal: return palette.accent
        case .orange: return palette.warm
        case .ink: return palette.ink
        case .muted: return palette.muted
        }
    }
}

/// 리더 본문 서체. 기본값은 번들된 명조(Noto Serif KR)이며, iOS 시스템에는 한글 세리프가
/// 없으므로 번들 등록이 실패한 경우에만 시스템 세리프(라틴만 New York)로 폴백한다.
enum ReaderTypeface {
    static let bodySerifName = "NotoSerifKR-Regular"

    /// 명조 본문 기준 크기. Noto Serif KR의 글립은 SF/Apple SD Gothic Neo보다 em 대비 작게
    /// 그려져 같은 pt에서 작아 보이므로 0.5pt 보정한다.
    static let bodySerifSize: CGFloat = 17.5

    /// 명조는 자폭이 넉넉해 SF용 음수 트래킹(-0.35)을 그대로 쓰면 조여 보인다.
    static let bodyTracking: CGFloat = isBodySerifAvailable ? -0.1 : -0.35

    static let isBodySerifAvailable: Bool = UIFont(name: bodySerifName, size: 17) != nil
}

extension Font {
    static func readerBody(scale: CGFloat = 1) -> Font {
        guard ReaderTypeface.isBodySerifAvailable else {
            return .system(size: 17 * scale, weight: .regular, design: .serif)
        }
        return .custom(ReaderTypeface.bodySerifName, fixedSize: ReaderTypeface.bodySerifSize * scale)
    }

    static func readerHeadline(mode: LayoutMode) -> Font {
        switch mode {
        case .compact: return .system(size: 36, weight: .bold)
        case .split: return .system(size: 42, weight: .bold)
        case .wide: return .system(size: 48, weight: .bold)
        }
    }
}
