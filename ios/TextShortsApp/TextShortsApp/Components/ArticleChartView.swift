import ArticleFeedKit
import Charts
import SwiftUI

struct ArticleChartView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let visualization: ArticleVisualization
    let mode: LayoutMode
    let palette: AppPalette
    let motionEnabled: Bool
    @State private var revealed = false

    private var data: [ArticleChartDatum] {
        ArticleChartSupport.data(for: visualization)
    }

    private var chartHeight: CGFloat {
        switch mode {
        case .compact: return 210
        case .split: return 240
        case .wide: return 280
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(visualization.title)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(palette.ink)
                    .lineLimit(2)
                Spacer()
                Text(visualization.unit.isEmpty ? visualization.kind.rawValue.uppercased() : visualization.unit)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(palette.muted)
            }

            if ArticleChartSupport.canRenderGraph(visualization) {
                if visualization.kind == .pie {
                    pieChart
                } else if visualization.kind == .worldMap || visualization.kind == .regionHeatmap {
                    geoHeatmap
                } else if visualization.kind == .matrixHeatmap {
                    matrixHeatmap
                } else {
                    cartesianChart
                }
                if visualization.kind.isHeatmap {
                    colorLegend
                } else {
                    legend
                }
            }

            valueGrid
        }
        .padding(.vertical, 13)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
        .opacity(revealed || !motionEnabled || reduceMotion ? 1 : 0.2)
        .scaleEffect(y: revealed || !motionEnabled || reduceMotion ? 1 : 0.82, anchor: .bottom)
        .onAppear {
            if motionEnabled && !reduceMotion {
                withAnimation(SwissMotion.easeOut()) { revealed = true }
            } else {
                revealed = true
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(visualization.title)
        .accessibilityValue(visualization.altText)
        .accessibilityIdentifier("article-chart-\(visualization.ref)")
    }

    private var cartesianChart: some View {
        Chart {
            ForEach(data.filter { $0.mark == .bar }) { datum in
                BarMark(
                    x: .value("항목", datum.label),
                    y: .value(visualization.unit, datum.value)
                )
                .position(by: .value("계열", datum.seriesName))
                .foregroundStyle(color(for: datum.seriesIndex))
            }

            ForEach(data.filter { $0.mark == .area }) { datum in
                AreaMark(
                    x: .value("항목", datum.label),
                    y: .value(visualization.unit, datum.value)
                )
                .foregroundStyle(color(for: datum.seriesIndex).opacity(0.22))
                .interpolationMethod(.monotone)
                LineMark(
                    x: .value("항목", datum.label),
                    y: .value(visualization.unit, datum.value)
                )
                .foregroundStyle(color(for: datum.seriesIndex))
                .lineStyle(StrokeStyle(lineWidth: 2))
                .interpolationMethod(.monotone)
            }

            ForEach(data.filter { $0.mark == .line }) { datum in
                LineMark(
                    x: .value("항목", datum.label),
                    y: .value(visualization.unit, datum.value)
                )
                .foregroundStyle(color(for: datum.seriesIndex))
                .lineStyle(StrokeStyle(lineWidth: 2.4))
                .interpolationMethod(.monotone)
                PointMark(
                    x: .value("항목", datum.label),
                    y: .value(visualization.unit, datum.value)
                )
                .foregroundStyle(color(for: datum.seriesIndex))
                .symbolSize(30)
            }
        }
        .chartLegend(.hidden)
        .chartYAxisLabel(visualization.unit)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: mode == .compact ? 4 : 7)) { _ in
                AxisGridLine().foregroundStyle(palette.line.opacity(0.55))
                AxisValueLabel().foregroundStyle(palette.muted)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { _ in
                AxisGridLine().foregroundStyle(palette.line.opacity(0.55))
                AxisValueLabel().foregroundStyle(palette.muted)
            }
        }
        .frame(height: chartHeight)
        .accessibilityHidden(true)
    }

    private var pieChart: some View {
        let fractions = ArticleChartSupport.pieFractions(visualization)
        return Canvas { context, size in
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let radius = min(size.width, size.height) * 0.44
            var start = -Double.pi / 2
            for (index, fraction) in fractions.enumerated() {
                let end = start + fraction * 2 * Double.pi
                var path = Path()
                path.move(to: center)
                path.addArc(
                    center: center,
                    radius: radius,
                    startAngle: .radians(start),
                    endAngle: .radians(end),
                    clockwise: false
                )
                path.closeSubpath()
                context.fill(path, with: .color(color(for: index)))
                start = end
            }
            let hole = Path(ellipseIn: CGRect(
                x: center.x - radius * 0.48,
                y: center.y - radius * 0.48,
                width: radius * 0.96,
                height: radius * 0.96
            ))
            context.fill(hole, with: .color(palette.paper))
        }
        .frame(maxWidth: mode == .wide ? 360 : .infinity)
        .frame(height: chartHeight)
        .frame(maxWidth: .infinity, alignment: .center)
        .accessibilityHidden(true)
    }

    private var geoHeatmap: some View {
        let scope = ArticleGeoScope(rawValue: visualization.geoScope ?? "") ?? .world
        let features = ArticleGeoRepository.shared.features(scope: scope)
        let valuesByCode = data.reduce(into: [String: Double]()) { result, datum in
            if !datum.regionCode.isEmpty { result[datum.regionCode] = datum.value }
        }
        let values = Array(valuesByCode.values)
        return Canvas { context, size in
            let viewport = ArticleGeoViewport.forScope(scope)
            let fittedRect = viewport.fittedRect(in: size)
            for feature in features {
                var path = Path()
                for ring in feature.polygons {
                    var previousLongitude: Double?
                    var segmentOpen = false
                    for coordinate in ring {
                        let localPoint = viewport.project(coordinate, into: fittedRect.size)
                        let projected = CGPoint(
                            x: fittedRect.minX + localPoint.x,
                            y: fittedRect.minY + localPoint.y
                        )
                        if let previousLongitude,
                           abs(coordinate.longitude - previousLongitude) > 180 {
                            segmentOpen = false
                        }
                        if segmentOpen { path.addLine(to: projected) }
                        else {
                            path.move(to: projected)
                            segmentOpen = true
                        }
                        previousLongitude = coordinate.longitude
                    }
                    path.closeSubpath()
                }
                let fill = valuesByCode[feature.code].map {
                    heatColor(value: $0, values: values)
                } ?? palette.deep.opacity(0.38)
                context.fill(path, with: .color(fill), style: FillStyle(eoFill: true))
                context.stroke(path, with: .color(palette.paper.opacity(0.82)), lineWidth: 0.45)
            }
        }
        .frame(height: chartHeight)
        .clipShape(Rectangle())
        .accessibilityHidden(true)
    }

    private var matrixHeatmap: some View {
        let rows = orderedUnique(data.map(\.row).filter { !$0.isEmpty })
        let columns = orderedUnique(data.map(\.column).filter { !$0.isEmpty })
        let values = data.map(\.value)
        let cells = data.reduce(into: [String: Double]()) { result, datum in
            result["\(datum.row)\u{1F}\(datum.column)"] = datum.value
        }
        return Canvas { context, size in
            guard !rows.isEmpty, !columns.isEmpty else { return }
            let showLabels = rows.count <= 8 && columns.count <= 8
            let left: CGFloat = showLabels ? min(64, size.width * 0.22) : 0
            let top: CGFloat = showLabels ? 22 : 0
            let cellWidth = max(1, (size.width - left) / CGFloat(columns.count))
            let cellHeight = max(1, (size.height - top) / CGFloat(rows.count))
            for (rowIndex, row) in rows.enumerated() {
                for (columnIndex, column) in columns.enumerated() {
                    let rect = CGRect(
                        x: left + CGFloat(columnIndex) * cellWidth,
                        y: top + CGFloat(rowIndex) * cellHeight,
                        width: max(0, cellWidth - 2),
                        height: max(0, cellHeight - 2)
                    )
                    let key = "\(row)\u{1F}\(column)"
                    let fill = cells[key].map { heatColor(value: $0, values: values) }
                        ?? palette.deep.opacity(0.38)
                    context.fill(Path(rect), with: .color(fill))
                }
            }
            if showLabels {
                for (index, column) in columns.enumerated() {
                    let text = context.resolve(Text(column).font(.system(size: 8, weight: .medium)))
                    context.draw(
                        text,
                        at: CGPoint(x: left + (CGFloat(index) + 0.5) * cellWidth, y: top * 0.45),
                        anchor: .center
                    )
                }
                for (index, row) in rows.enumerated() {
                    let text = context.resolve(Text(row).font(.system(size: 8, weight: .medium)))
                    context.draw(
                        text,
                        at: CGPoint(x: left * 0.45, y: top + (CGFloat(index) + 0.5) * cellHeight),
                        anchor: .center
                    )
                }
            }
        }
        .frame(height: chartHeight)
        .accessibilityHidden(true)
    }

    private var colorLegend: some View {
        let sorted = data.sorted { $0.value < $1.value }
        let stops = ArticleColorScaleSupport.stops(scale: resolvedColorScale).map(color)
        return VStack(alignment: .leading, spacing: 5) {
            if resolvedColorScale.mode == "quantized" {
                HStack(spacing: 2) {
                    ForEach(Array(stops.enumerated()), id: \.offset) { _, stop in
                        Rectangle().fill(stop).frame(height: 8)
                    }
                }
            } else {
                LinearGradient(colors: stops, startPoint: .leading, endPoint: .trailing)
                    .frame(height: 8)
            }
            HStack {
                Text(sorted.first?.rawValue ?? "")
                Spacer()
                Text(sorted.last?.rawValue ?? "")
            }
            .font(.system(size: 9, weight: .medium))
            .foregroundColor(palette.muted)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("색상 범례")
        .accessibilityValue("\(sorted.first?.rawValue ?? "")부터 \(sorted.last?.rawValue ?? "")")
    }

    @ViewBuilder
    private var legend: some View {
        let columns = [GridItem(.adaptive(minimum: 118), spacing: 8, alignment: .leading)]
        if visualization.kind == .pie {
            LazyVGrid(columns: columns, alignment: .leading, spacing: 7) {
                ForEach(Array(data.enumerated()), id: \.offset) { index, datum in
                    HStack(spacing: 7) {
                        Rectangle()
                            .fill(color(for: index))
                            .frame(width: 8, height: 8)
                        Text(datum.label)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(palette.inkSoft)
                            .lineLimit(2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(datum.label), \(datum.rawValue)")
                }
            }
        } else {
            LazyVGrid(columns: columns, alignment: .leading, spacing: 7) {
                ForEach(Array(visualization.series.enumerated()), id: \.offset) { index, series in
                    HStack(spacing: 7) {
                        Image(systemName: symbol(for: series.mark))
                            .font(.system(size: 10, weight: .bold))
                            .foregroundColor(color(for: index))
                            .frame(width: 14)
                        Text(series.name)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(palette.inkSoft)
                            .lineLimit(2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(series.name), \(series.mark.rawValue) 계열")
                }
            }
        }
    }

    @ViewBuilder
    private var valueGrid: some View {
        if data.count > 6 {
            let shown = data.prefix(12)
            let remaining = max(0, data.count - shown.count)
            Text(
                shown.map { "\($0.label) \($0.rawValue)" }.joined(separator: "  ·  ")
                    + (remaining > 0 ? "  ·  외 \(remaining)개" : "")
            )
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(palette.inkSoft)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(data.map {
                    "\($0.seriesName), \($0.label), \($0.rawValue)"
                }.joined(separator: ". "))
        } else {
            let columns = [GridItem(.adaptive(minimum: mode == .compact ? 110 : 140), spacing: 8)]
            LazyVGrid(columns: columns, alignment: .leading, spacing: 7) {
                ForEach(Array(data.enumerated()), id: \.offset) { index, datum in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if visualization.kind == .pie {
                            Rectangle()
                                .fill(color(for: index))
                                .frame(width: 7, height: 7)
                        }
                        Text(datum.label)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        Text(datum.rawValue)
                            .fontWeight(.bold)
                            .lineLimit(1)
                    }
                    .font(.system(size: 11))
                    .foregroundColor(palette.inkSoft)
                    .padding(.horizontal, 7)
                    .frame(minHeight: 32)
                    .overlay(Rectangle().stroke(palette.line, lineWidth: 1))
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(datum.seriesName), \(datum.label)")
                    .accessibilityValue(datum.rawValue)
                }
            }
        }
    }

    private func color(for index: Int) -> Color {
        if visualization.colorScale != nil {
            let stops = ArticleColorScaleSupport.stops(scale: resolvedColorScale)
            return color(stops[index % stops.count])
        }
        return palette.chartColors[index % palette.chartColors.count]
    }

    private var resolvedColorScale: ArticleColorScale {
        visualization.colorScale ?? ArticleColorScale(scheme: "ocean", mode: "continuous")
    }

    private func heatColor(value: Double, values: [Double]) -> Color {
        color(ArticleColorScaleSupport.components(
            value: value,
            values: values,
            scale: resolvedColorScale
        ))
    }

    private func color(_ components: ArticleColorComponents) -> Color {
        Color(red: components.red, green: components.green, blue: components.blue)
    }

    private func orderedUnique(_ values: [String]) -> [String] {
        values.reduce(into: []) { result, value in
            if !result.contains(value) { result.append(value) }
        }
    }

    private func symbol(for mark: ArticleChartMark) -> String {
        switch mark {
        case .bar: return "rectangle.fill"
        case .line: return "minus"
        case .area: return "triangle.fill"
        case .sector: return "circle.fill"
        case .region: return "map.fill"
        case .cell: return "square.grid.3x3.fill"
        case .unsupported: return "list.bullet"
        }
    }
}

struct ArticleChartDatum: Identifiable, Equatable {
    let id: String
    let seriesIndex: Int
    let seriesName: String
    let mark: ArticleChartMark
    let label: String
    let value: Double
    let rawValue: String
    let regionCode: String
    let row: String
    let column: String
}

enum ArticleChartSupport {
    static func data(for visualization: ArticleVisualization) -> [ArticleChartDatum] {
        visualization.series.enumerated().flatMap { seriesIndex, series in
            series.points.enumerated().map { pointIndex, point in
                ArticleChartDatum(
                    id: "\(seriesIndex)-\(pointIndex)",
                    seriesIndex: seriesIndex,
                    seriesName: series.name,
                    mark: series.mark,
                    label: point.label,
                    value: point.value,
                    rawValue: point.rawValue,
                    regionCode: point.regionCode ?? "",
                    row: point.row ?? "",
                    column: point.column ?? ""
                )
            }
        }
    }

    static func canRenderGraph(_ visualization: ArticleVisualization) -> Bool {
        let points = data(for: visualization)
        guard !points.isEmpty, points.allSatisfy({ $0.value.isFinite }) else { return false }
        switch visualization.kind {
        case .bar:
            return points.count >= 2 && points.count <= 12
                && points.allSatisfy { $0.mark == .bar }
        case .line:
            return points.count >= 2 && points.count <= 12
                && points.allSatisfy { $0.mark == .line }
        case .area:
            return points.count >= 2 && points.count <= 12
                && points.allSatisfy { $0.mark == .area }
        case .pie:
            return points.count >= 2 && points.count <= 12
                && points.allSatisfy { $0.mark == .sector && $0.value >= 0 }
                && points.contains { $0.value > 0 }
        case .mixed:
            let marks = Set(points.map { $0.mark.rawValue })
            return points.count >= 2 && points.count <= 12
                && !marks.contains("sector") && marks.count >= 2
        case .worldMap:
            let codes = Set(points.map(\.regionCode))
            let knownCodes = Set(ArticleGeoRepository.shared.features.map(\.code))
            return points.count <= 200
                && points.allSatisfy { $0.mark == .region && $0.regionCode.count == 2 }
                && codes.count == points.count
                && codes.isSubset(of: knownCodes)
        case .regionHeatmap:
            let scope = ArticleGeoScope(rawValue: visualization.geoScope ?? "")
            let codes = Set(points.map(\.regionCode))
            let knownCodes = scope.map {
                Set(ArticleGeoRepository.shared.features(scope: $0).map(\.code))
            } ?? []
            return points.count >= 2
                && points.count <= 200
                && scope != nil && scope != .world
                && points.allSatisfy { $0.mark == .region && $0.regionCode.count == 2 }
                && codes.count == points.count
                && codes.isSubset(of: knownCodes)
        case .matrixHeatmap:
            let cells = Set(points.map { "\($0.row)\u{1F}\($0.column)" })
            return points.count >= 2
                && points.count <= 100
                && points.allSatisfy { $0.mark == .cell && !$0.row.isEmpty && !$0.column.isEmpty }
                && cells.count == points.count
        case .unsupported:
            return false
        }
    }

    static func pieFractions(_ visualization: ArticleVisualization) -> [Double] {
        let values = data(for: visualization).map { max(0, $0.value) }
        let total = values.reduce(0, +)
        guard total > 0 else { return [] }
        return values.map { $0 / total }
    }
}

private extension ArticleVisualizationKind {
    var isHeatmap: Bool {
        self == .worldMap || self == .regionHeatmap || self == .matrixHeatmap
    }
}
