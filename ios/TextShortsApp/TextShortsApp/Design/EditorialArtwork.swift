import SwiftUI

struct EditorialArtwork: View {
    let kind: ArtworkKind

    var body: some View {
        Canvas { context, size in
            switch kind {
            case .tidal: drawTidal(context: &context, size: size)
            case .canopy: drawCanopy(context: &context, size: size)
            case .nightGrid: drawNightGrid(context: &context, size: size)
            }
        }
        .accessibilityHidden(true)
    }

    private func drawTidal(context: inout GraphicsContext, size: CGSize) {
        let horizon = size.height * 0.56
        context.fill(Path(CGRect(x: 0, y: 0, width: size.width, height: horizon)), with: .linearGradient(Gradient(colors: [Color(red: 0.76, green: 0.84, blue: 0.81), Color(red: 0.91, green: 0.89, blue: 0.83)]), startPoint: .zero, endPoint: CGPoint(x: 0, y: horizon)))
        context.fill(Path(CGRect(x: 0, y: horizon, width: size.width, height: size.height - horizon)), with: .linearGradient(Gradient(colors: [Color(red: 0.10, green: 0.40, blue: 0.39), Color(red: 0.07, green: 0.25, blue: 0.29)]), startPoint: CGPoint(x: 0, y: horizon), endPoint: CGPoint(x: size.width, y: size.height)))

        var island = Path()
        island.move(to: CGPoint(x: 0, y: horizon))
        island.addLine(to: CGPoint(x: size.width * 0.28, y: horizon - size.height * 0.08))
        island.addLine(to: CGPoint(x: size.width * 0.48, y: horizon))
        island.closeSubpath()
        context.fill(island, with: .color(Color(red: 0.28, green: 0.40, blue: 0.38).opacity(0.62)))

        for offset in [0.18, 0.43, 0.72] {
            var wave = Path()
            wave.move(to: CGPoint(x: 0, y: horizon + size.height * offset))
            wave.addCurve(to: CGPoint(x: size.width, y: horizon + size.height * offset + 2), control1: CGPoint(x: size.width * 0.28, y: horizon + size.height * offset - 12), control2: CGPoint(x: size.width * 0.68, y: horizon + size.height * offset + 12))
            context.stroke(wave, with: .color(Color(red: 0.62, green: 0.75, blue: 0.70).opacity(0.5)), lineWidth: 2)
        }

        let cx = size.width * 0.69
        let baseY = size.height * 0.79
        context.fill(Path(ellipseIn: CGRect(x: cx - 34, y: baseY + 25, width: 68, height: 10)), with: .color(.black.opacity(0.18)))
        var buoy = Path()
        buoy.move(to: CGPoint(x: cx - 18, y: baseY))
        buoy.addLine(to: CGPoint(x: cx + 18, y: baseY))
        buoy.addLine(to: CGPoint(x: cx + 11, y: baseY + 31))
        buoy.addQuadCurve(to: CGPoint(x: cx - 11, y: baseY + 31), control: CGPoint(x: cx, y: baseY + 39))
        buoy.closeSubpath()
        context.fill(buoy, with: .color(Color(red: 0.84, green: 0.35, blue: 0.22)))
        context.fill(Path(roundedRect: CGRect(x: cx - 3, y: baseY - 41, width: 6, height: 43), cornerRadius: 3), with: .color(Color(red: 0.94, green: 0.91, blue: 0.83)))
        context.fill(Path(ellipseIn: CGRect(x: cx - 10, y: baseY - 54, width: 20, height: 20)), with: .color(Color(red: 0.82, green: 0.31, blue: 0.19)))
        context.fill(Path(ellipseIn: CGRect(x: cx - 3, y: baseY - 47, width: 6, height: 6)), with: .color(Color(red: 0.94, green: 0.91, blue: 0.83)))
        var antenna = Path()
        antenna.move(to: CGPoint(x: cx, y: baseY - 54))
        antenna.addLine(to: CGPoint(x: cx, y: baseY - 76))
        antenna.addLine(to: CGPoint(x: cx - 9, y: baseY - 65))
        antenna.move(to: CGPoint(x: cx, y: baseY - 76))
        antenna.addLine(to: CGPoint(x: cx + 9, y: baseY - 65))
        context.stroke(antenna, with: .color(Color(red: 0.07, green: 0.24, blue: 0.27)), lineWidth: 3)
    }

    private func drawCanopy(context: inout GraphicsContext, size: CGSize) {
        context.fill(Path(CGRect(origin: .zero, size: size)), with: .linearGradient(Gradient(colors: [Color(red: 0.80, green: 0.83, blue: 0.77), Color(red: 0.36, green: 0.47, blue: 0.41)]), startPoint: CGPoint(x: size.width * 0.55, y: 0), endPoint: CGPoint(x: 0, y: size.height)))
        let colors = [
            Color(red: 0.08, green: 0.28, blue: 0.22), Color(red: 0.16, green: 0.36, blue: 0.29),
            Color(red: 0.22, green: 0.40, blue: 0.33), Color(red: 0.11, green: 0.25, blue: 0.21)
        ]
        let radius = min(size.width, size.height) * 0.20
        for row in 0..<4 {
            for column in 0..<7 {
                let x = CGFloat(column) * size.width / 6.0 + (row.isMultiple(of: 2) ? 0 : 18)
                let y = CGFloat(row) * size.height / 3.0
                let rect = CGRect(x: x - radius, y: y - radius, width: radius * 2.2, height: radius * 2.2)
                context.fill(Path(ellipseIn: rect), with: .color(colors[(row + column) % colors.count].opacity(0.96)))
            }
        }
        var path = Path()
        path.move(to: CGPoint(x: -20, y: size.height * 0.72))
        path.addCurve(to: CGPoint(x: size.width + 20, y: size.height * 0.53), control1: CGPoint(x: size.width * 0.28, y: size.height * 0.50), control2: CGPoint(x: size.width * 0.58, y: size.height * 0.74))
        context.stroke(path, with: .color(Color(red: 0.89, green: 0.88, blue: 0.82).opacity(0.55)), lineWidth: 18)
        context.stroke(path, with: .color(Color(red: 0.95, green: 0.93, blue: 0.87).opacity(0.75)), lineWidth: 3)
    }

    private func drawNightGrid(context: inout GraphicsContext, size: CGSize) {
        context.fill(Path(CGRect(origin: .zero, size: size)), with: .linearGradient(Gradient(colors: [Color(red: 0.07, green: 0.14, blue: 0.18), Color(red: 0.14, green: 0.28, blue: 0.29)]), startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
        context.fill(Path(ellipseIn: CGRect(x: size.width * 0.76, y: size.height * 0.11, width: 44, height: 44)), with: .color(Color(red: 0.91, green: 0.83, blue: 0.62).opacity(0.85)))
        var hill = Path()
        hill.move(to: CGPoint(x: 0, y: size.height * 0.76))
        hill.addCurve(to: CGPoint(x: size.width, y: size.height * 0.74), control1: CGPoint(x: size.width * 0.28, y: size.height * 0.58), control2: CGPoint(x: size.width * 0.58, y: size.height * 0.69))
        hill.addLine(to: CGPoint(x: size.width, y: size.height))
        hill.addLine(to: CGPoint(x: 0, y: size.height))
        hill.closeSubpath()
        context.fill(hill, with: .color(Color(red: 0.05, green: 0.18, blue: 0.17)))
        drawTower(context: &context, center: CGPoint(x: size.width * 0.36, y: size.height * 0.68), height: size.height * 0.45)
        drawTower(context: &context, center: CGPoint(x: size.width * 0.70, y: size.height * 0.69), height: size.height * 0.32)
    }

    private func drawTower(context: inout GraphicsContext, center: CGPoint, height: CGFloat) {
        let top = center.y - height
        var tower = Path()
        tower.move(to: CGPoint(x: center.x - height * 0.12, y: center.y))
        tower.addLine(to: CGPoint(x: center.x, y: top))
        tower.addLine(to: CGPoint(x: center.x + height * 0.12, y: center.y))
        tower.move(to: CGPoint(x: center.x - height * 0.085, y: center.y - height * 0.36))
        tower.addLine(to: CGPoint(x: center.x + height * 0.085, y: center.y - height * 0.36))
        tower.move(to: CGPoint(x: center.x - height * 0.06, y: center.y - height * 0.62))
        tower.addLine(to: CGPoint(x: center.x + height * 0.06, y: center.y - height * 0.62))
        context.stroke(tower, with: .color(Color(red: 0.67, green: 0.73, blue: 0.68)), lineWidth: 3)
    }
}

struct GesturePreviewArtwork: View {
    let palette: AppPalette
    let motionEnabled: Bool
    @State private var floating = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 7) {
                Text("결")
                    .font(.system(size: 20, weight: .heavy))
                    .tracking(-2.5)
                    .foregroundColor(palette.accentDeep)
                Spacer()
                Text("2 / 5").font(.system(size: 10, design: .monospaced))
                Image(systemName: "bookmark").font(.system(size: 13))
                Image(systemName: "ellipsis").font(.system(size: 14))
            }
            .frame(height: 38)
            .padding(.horizontal, 12)
            ZStack(alignment: .leading) {
                palette.deep
                palette.accent.frame(width: 104)
            }
            .frame(height: 4)
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 7) {
                    Rectangle().fill(palette.warm).frame(width: 3, height: 15)
                    Text("맥락").font(.system(size: 11, weight: .bold))
                }
                Text("같은 기사도 역할에 따라\n다르게 읽힙니다.")
                    .font(.system(size: 22, weight: .bold))
                    .tracking(-1.2)
                    .lineSpacing(-1)
                    .padding(.top, 14)
                Text("화면의 위치는 그대로 두고, 장면의 의미만 선명하게 바뀝니다.")
                    .font(.system(size: 11))
                    .foregroundColor(palette.muted)
                    .lineSpacing(3)
                    .padding(.top, 10)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .padding(18)
            HStack {
                Text("다음 장면")
                Spacer()
                Image(systemName: "arrow.up")
            }
            .font(.system(size: 11, weight: .bold))
            .foregroundColor(palette.accentDeep)
            .frame(height: 42)
            .padding(.horizontal, 16)
            .overlay(alignment: .top) { Rectangle().fill(palette.line).frame(height: 1) }
        }
        .frame(width: 260, height: 268)
        .background(palette.raised)
        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
        .rotationEffect(.degrees(floating ? -1 : -2))
        .offset(y: floating ? -7 : 0)
        .onAppear {
            guard motionEnabled else { return }
            withAnimation(.easeInOut(duration: 1.7).repeatForever(autoreverses: true)) {
                floating = true
            }
        }
    }
}
