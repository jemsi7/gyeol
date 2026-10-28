import SwiftUI

struct PrototypeDock: View {
    @EnvironmentObject private var store: PrototypeStore
    let mode: LayoutMode
    let width: CGFloat
    let palette: AppPalette

    var body: some View {
        HStack(spacing: 12) {
            HStack(spacing: 7) {
                Text("화면").font(.system(size: 12)).fixedSize()
                Menu {
                    ForEach(PrototypeScreen.allCases) { screen in
                        Button(screen.dockLabel) { store.go(to: screen) }
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(store.currentScreen.dockLabel)
                            .lineLimit(1)
                            .minimumScaleFactor(0.82)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.up.chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .padding(.horizontal, 9)
                    .frame(width: mode == .compact ? 104 : 180, height: 38)
                    .background(Color(red: 0.12, green: 0.21, blue: 0.22))
                    .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color(red: 0.35, green: 0.43, blue: 0.43), lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: 9))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("시안 화면 선택")
            }
            .prototypeTagged("PX-01")
            .layoutPriority(2)

            Spacer(minLength: 2)

            Toggle("번호 표시", isOn: $store.showAnnotations)
                .toggleStyle(DockCheckboxToggleStyle())
                .font(.system(size: 12))
                .prototypeTagged("PX-02")
                .fixedSize()
                .layoutPriority(1)

            Text("\(mode.rawValue) · \(Int(width))pt")
                .font(.system(size: 10, weight: .medium))
                .foregroundColor(Color(red: 0.53, green: 0.76, blue: 0.72))
                .lineLimit(1)
                .prototypeTagged("PX-03")
        }
        .padding(.horizontal, 12)
        .foregroundColor(Color(red: 0.92, green: 0.91, blue: 0.87))
        .background(Color(red: 0.08, green: 0.15, blue: 0.16))
    }
}
