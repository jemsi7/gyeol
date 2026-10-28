import AuthenticationServices
import SwiftUI
import UIKit

struct AppleAccountSignInButton: View {
    static let maximumWidth: CGFloat = 375

    @Environment(\.colorScheme) private var colorScheme
    @State private var rawNonce: String?

    let uxID: UXID
    let showAnnotations: Bool
    let isDisabled: Bool
    let onPayload: (AppleAuthorizationPayload) -> Void
    let onError: (Error) -> Void
    let onCancel: () -> Void

    var body: some View {
        SignInWithAppleButton(.signIn) { request in
            do {
                let nonce = try AppleSignInNonce.make()
                rawNonce = nonce.raw
                request.nonce = nonce.hashed
                request.requestedScopes = [.fullName, .email]
            } catch {
                rawNonce = nil
                DispatchQueue.main.async { onError(error) }
            }
        } onCompletion: { result in
            defer { rawNonce = nil }
            switch result {
            case let .success(authorization):
                guard let credential = authorization.credential
                    as? ASAuthorizationAppleIDCredential,
                      let rawNonce else {
                    onError(AccountAuthenticationError.invalidAppleCredential)
                    return
                }
                do {
                    onPayload(try AppleAuthorizationPayload(
                        credential: credential,
                        rawNonce: rawNonce
                    ))
                } catch {
                    onError(error)
                }
            case let .failure(error):
                if let authorizationError = error as? ASAuthorizationError,
                   authorizationError.code == .canceled {
                    onCancel()
                } else {
                    onError(error)
                }
            }
        }
        .signInWithAppleButtonStyle(colorScheme == .dark ? .white : .black)
        .frame(maxWidth: Self.maximumWidth, minHeight: 52, maxHeight: 52)
        .disabled(isDisabled)
        .opacity(isDisabled ? 0.48 : 1)
        .accessibilityLabel("Apple로 로그인")
        .uxTagged(uxID, show: showAnnotations)
    }
}

struct DestructiveAccountButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: .bold))
            .foregroundColor(.white)
            .frame(maxWidth: .infinity, minHeight: 52)
            .padding(.horizontal, 14)
            .background(configuration.isPressed ? Color.red.opacity(0.78) : Color.red)
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.45)
    }
}

struct ReaderBackNavigationButton: View {
    let palette: AppPalette
    let showAnnotations: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label("뒤로", systemImage: "chevron.left")
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minWidth: 58, minHeight: AppMetrics.touchTarget)
                .contentShape(Rectangle())
                .uxTagged(.ux009, show: showAnnotations)
        }
        .buttonStyle(.plain)
        .fixedSize(horizontal: true, vertical: false)
        .accessibilityLabel("뒤로")
        .accessibilityHint("현재 읽기 위치를 저장하고 이전 화면으로 돌아갑니다")
        .uxTagged(.ux101, show: showAnnotations)
    }
}

struct PrimaryPillButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.appMotionEnabled) private var motionEnabled
    let palette: AppPalette

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .bold))
            .foregroundColor(palette.raised)
            .frame(minHeight: 52)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 16)
            .background(configuration.isPressed ? palette.ink : palette.accentDeep)
            .overlay(Rectangle().stroke(configuration.isPressed ? palette.ink : palette.accentDeep, lineWidth: 1))
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.34)
            .scaleEffect(configuration.isPressed ? 0.99 : 1)
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: configuration.isPressed)
    }
}

struct QuietPillButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.appMotionEnabled) private var motionEnabled
    let palette: AppPalette

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: .semibold))
            .foregroundColor(isEnabled ? palette.ink : palette.muted)
            .frame(minHeight: 52)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 14)
            .background(configuration.isPressed ? palette.deep : Color.clear)
            .overlay(Rectangle().stroke(palette.ruleStrong.opacity(isEnabled ? 1 : 0.48), lineWidth: 1))
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.50)
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: configuration.isPressed)
    }
}

struct CircleIconButtonStyle: ButtonStyle {
    @Environment(\.appMotionEnabled) private var motionEnabled
    let palette: AppPalette

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(width: AppMetrics.touchTarget, height: AppMetrics.touchTarget)
            .background(configuration.isPressed ? palette.deep : Color.clear)
            .contentShape(Rectangle())
            .animation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil, value: configuration.isPressed)
    }
}

struct SwissSwitchToggleStyle: ToggleStyle {
    @Environment(\.appMotionEnabled) private var motionEnabled
    let palette: AppPalette

    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .center, spacing: 18) {
            configuration.label
            Spacer(minLength: 8)
            Button {
                withAnimation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil) {
                    configuration.isOn.toggle()
                }
            } label: {
                ZStack(alignment: configuration.isOn ? .trailing : .leading) {
                    Rectangle()
                        .fill(configuration.isOn ? palette.accentDeep : palette.deep)
                        .frame(width: 46, height: 26)
                    Rectangle()
                        .fill(configuration.isOn ? palette.raised : palette.muted)
                        .frame(width: 18, height: 18)
                        .padding(4)
                }
                .overlay(Rectangle().stroke(configuration.isOn ? palette.accentDeep : palette.ruleStrong, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(configuration.isOn ? "켬" : "끔")
            .accessibilityAddTraits(configuration.isOn ? .isSelected : AccessibilityTraits())
        }
    }
}

struct DockCheckboxToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button { configuration.isOn.toggle() } label: {
            HStack(spacing: 6) {
                Image(systemName: configuration.isOn ? "checkmark.square.fill" : "square")
                    .foregroundColor(configuration.isOn ? Color(red: 0.39, green: 0.72, blue: 0.69) : .secondary)
                configuration.label
            }
        }
        .buttonStyle(.plain)
    }
}

struct SimpleHeader<Leading: View, Trailing: View>: View {
    let title: String
    let titleID: UXID
    let showAnnotations: Bool
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing
    let palette: AppPalette

    init(
        title: String,
        titleID: UXID,
        showAnnotations: Bool,
        @ViewBuilder leading: () -> Leading,
        @ViewBuilder trailing: () -> Trailing,
        palette: AppPalette
    ) {
        self.title = title
        self.titleID = titleID
        self.showAnnotations = showAnnotations
        self.leading = leading()
        self.trailing = trailing()
        self.palette = palette
    }

    private var eyebrow: String {
        switch title {
        case "보관함": return "ARCHIVE"
        case "읽기 설정": return "READING SYSTEM"
        default: return "EDITORIAL READER"
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            leading.frame(maxWidth: .infinity, alignment: .leading)
            VStack(spacing: 2) {
                Text(eyebrow)
                    .font(.system(size: 8, weight: .medium))
                    .tracking(1.1)
                    .foregroundColor(palette.muted)
                    .accessibilityHidden(true)
                Text(title)
                    .font(.system(size: 20, weight: .bold))
                    .tracking(-0.7)
                    .uxTagged(titleID, show: showAnnotations)
            }
            .fixedSize()
            trailing.frame(maxWidth: .infinity, alignment: .trailing)
        }
        .frame(height: AppMetrics.headerHeight)
        .padding(.horizontal, 16)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .background(palette.paper)
    }
}

struct HubHeader: View {
    let kicker: String
    let title: String
    let detail: String
    let titleID: UXID
    let showAnnotations: Bool
    let palette: AppPalette

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(kicker)
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .tracking(1.2)
                .foregroundColor(palette.warm)
            HStack(alignment: .lastTextBaseline, spacing: 12) {
                Text(title)
                    .font(.system(size: 28, weight: .heavy))
                    .tracking(-1.7)
                    .foregroundColor(palette.ink)
                    .uxTagged(titleID, show: showAnnotations)
                Spacer(minLength: 10)
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .lineLimit(2)
                    .multilineTextAlignment(.trailing)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 84, alignment: .leading)
        .padding(.horizontal, 24)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .background(palette.paper)
    }
}

struct PrimaryBottomNavigation: View {
    let selected: PrimaryAppTab
    let showAnnotations: Bool
    let palette: AppPalette
    let onSelect: (PrimaryAppTab) -> Void

    var body: some View {
        HStack(spacing: 0) {
            ForEach(PrimaryAppTab.allCases) { tab in
                taggedButton(tab)
            }
        }
        .frame(height: AppMetrics.primaryNavigationHeight)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .background(palette.paper.ignoresSafeArea(edges: .bottom))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("주요 화면")
    }

    @ViewBuilder
    private func taggedButton(_ tab: PrimaryAppTab) -> some View {
        switch tab {
        case .home:
            tabButton(tab).uxTagged(.ux101, show: showAnnotations)
        case .reader:
            tabButton(tab).uxTagged(.ux102, show: showAnnotations)
        case .library:
            tabButton(tab).uxTagged(.ux099, show: showAnnotations)
        case .settings:
            tabButton(tab).uxTagged(.ux100, show: showAnnotations)
        }
    }

    private func tabButton(_ tab: PrimaryAppTab) -> some View {
        let isSelected = selected == tab
        return Button { onSelect(tab) } label: {
            VStack(spacing: 5) {
                Image(systemName: tab.symbol)
                    .font(.system(size: 21, weight: isSelected ? .semibold : .regular))
                Text(tab.label)
                    .font(.system(size: 10, weight: isSelected ? .bold : .medium))
            }
            .foregroundColor(isSelected ? palette.accentDeep : palette.inkSoft)
            .frame(maxWidth: .infinity, minHeight: AppMetrics.primaryNavigationHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(tab.label)
        .accessibilityAddTraits(isSelected ? .isSelected : AccessibilityTraits())
    }
}

struct SegmentedControl<Option: Hashable & Identifiable>: View where Option.ID: Hashable {
    @Environment(\.appMotionEnabled) private var motionEnabled
    let options: [Option]
    let label: (Option) -> String
    @Binding var selection: Option
    let palette: AppPalette

    var body: some View {
        HStack(spacing: -1) {
            ForEach(options) { option in
                Button {
                    withAnimation(motionEnabled ? SwissMotion.easeOut(SwissMotion.fast) : nil) { selection = option }
                } label: {
                    Text(label(option))
                        .font(.system(size: 12, weight: selection == option ? .semibold : .regular))
                        .foregroundColor(selection == option ? palette.raised : palette.muted)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(selection == option ? palette.accentDeep : Color.clear)
                        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selection == option ? .isSelected : AccessibilityTraits())
            }
        }
    }
}

struct RoleMarker: View {
    let token: SceneRoleToken
    let palette: AppPalette

    var body: some View {
        HStack(spacing: 9) {
            Rectangle().fill(token.color(palette)).frame(width: 4, height: 23)
            Text(token.short)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .foregroundColor(palette.muted)
            Text(token.label)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(palette.ink)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("장면 역할, \(token.label)")
    }
}

struct EditorialTopline<Trailing: View>: View {
    let code: String
    let palette: AppPalette
    @ViewBuilder var trailing: Trailing

    init(code: String, palette: AppPalette, @ViewBuilder trailing: () -> Trailing) {
        self.code = code
        self.palette = palette
        self.trailing = trailing()
    }

    var body: some View {
        HStack(spacing: 0) {
            Text("결")
                .font(.system(size: 30, weight: .heavy))
                .tracking(-4)
                .foregroundColor(palette.accentDeep)
                .accessibilityLabel("결")
            Spacer()
            if code.isEmpty {
                trailing
            } else {
                Text(code)
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .tracking(1.2)
                trailing
            }
        }
        .frame(height: 60)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }
}

struct ToastView: View {
    let message: String
    let palette: AppPalette

    var body: some View {
        Text(message)
            .font(.system(size: 12, weight: .semibold))
            .foregroundColor(palette.raised)
            .padding(.horizontal, 14)
            .frame(minHeight: 42)
            .frame(maxWidth: .infinity)
            .background(palette.ink)
            .accessibilityAddTraits(.isStaticText)
    }
}

struct ActivityShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

struct SkeletonBlock: View {
    let palette: AppPalette
    let reduceMotion: Bool
    @State private var illuminated = false

    var body: some View {
        palette.deep
            .opacity(reduceMotion ? 0.68 : (illuminated ? 0.88 : 0.44))
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) {
                    illuminated = true
                }
            }
    }
}

struct KeyCommandReader: UIViewControllerRepresentable {
    let onPrevious: () -> Void
    let onNext: () -> Void
    let onSave: () -> Void
    let onEscape: () -> Void

    func makeUIViewController(context: Context) -> KeyCommandController {
        let controller = KeyCommandController()
        controller.onPrevious = onPrevious
        controller.onNext = onNext
        controller.onSave = onSave
        controller.onEscape = onEscape
        return controller
    }

    func updateUIViewController(_ controller: KeyCommandController, context: Context) {
        controller.onPrevious = onPrevious
        controller.onNext = onNext
        controller.onSave = onSave
        controller.onEscape = onEscape
        DispatchQueue.main.async { controller.becomeFirstResponder() }
    }
}

final class KeyCommandController: UIViewController {
    var onPrevious: (() -> Void)?
    var onNext: (() -> Void)?
    var onSave: (() -> Void)?
    var onEscape: (() -> Void)?
    override var canBecomeFirstResponder: Bool { true }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        becomeFirstResponder()
    }

    override var keyCommands: [UIKeyCommand]? {
        [
            UIKeyCommand(input: UIKeyCommand.inputLeftArrow, modifierFlags: [], action: #selector(previous)),
            UIKeyCommand(input: UIKeyCommand.inputRightArrow, modifierFlags: [], action: #selector(nextScene)),
            UIKeyCommand(input: UIKeyCommand.inputUpArrow, modifierFlags: [], action: #selector(previous)),
            UIKeyCommand(input: UIKeyCommand.inputDownArrow, modifierFlags: [], action: #selector(nextScene)),
            UIKeyCommand(input: " ", modifierFlags: [], action: #selector(nextScene)),
            UIKeyCommand(input: "s", modifierFlags: [], action: #selector(save)),
            UIKeyCommand(input: UIKeyCommand.inputEscape, modifierFlags: [], action: #selector(escape)),
        ]
    }

    @objc private func previous() { onPrevious?() }
    @objc private func nextScene() { onNext?() }
    @objc private func save() { onSave?() }
    @objc private func escape() { onEscape?() }
}
