import SwiftUI

public struct ArticleMapView: View {
    private let item: ArticleFeedItem
    private let blocks: [ArticleContentBlock]
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @ScaledMetric(relativeTo: .body) private var compactInset = 20.0

    public init(item: ArticleFeedItem, parser: ArticleMapParser = .init()) {
        self.item = item
        blocks = parser.parse(item.map)
    }

    public var body: some View {
        GeometryReader { geometry in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    articleHeader
                    ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                        content(block)
                    }
                    sourceFooter
                }
                .frame(
                    maxWidth: readingColumnWidth(for: geometry.size.width),
                    alignment: .leading
                )
                .padding(.horizontal, horizontalInset(for: geometry.size.width))
                .padding(.top, geometry.size.width >= 760 ? 52 : 28)
                .padding(.bottom, 72)
                .frame(maxWidth: .infinity)
            }
            .background(articleBackground)
        }
    }

    private var articleHeader: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let domain = item.domain, !domain.isEmpty {
                Text(domain.uppercased())
                    .font(.caption.weight(.semibold))
                    .tracking(0.6)
                    .foregroundStyle(.secondary)
                    .padding(.bottom, 14)
            }
            Text(item.map.headline)
                .font(.largeTitle.weight(.bold))
                .accessibilityAddTraits(.isHeader)
                .fixedSize(horizontal: false, vertical: true)
            if let subheadline = item.map.subheadline, !subheadline.isEmpty {
                Text(subheadline)
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .padding(.top, 12)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(item.map.summary)
                .font(.body.weight(.medium))
                .foregroundStyle(.primary.opacity(0.82))
                .lineSpacing(5)
                .padding(.top, 24)
                .padding(.bottom, 34)
                .accessibilityLabel("기사 요약. \(item.map.summary)")
        }
    }

    @ViewBuilder
    private func content(_ block: ArticleContentBlock) -> some View {
        switch block {
        case .markdown(let markdown):
            Text(attributed(markdown))
                .font(.body)
                .lineSpacing(6)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 26)
        case .image(let image):
            articleImage(image)
                .padding(.vertical, 8)
                .padding(.bottom, 26)
        case .visualization(let visualization):
            visualizationFallback(visualization)
                .padding(.vertical, 8)
                .padding(.bottom, 26)
        case .unsupported:
            Text("이 콘텐츠는 현재 버전에서 표시할 수 없습니다.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.vertical, 12)
                .accessibilityLabel("지원하지 않는 기사 콘텐츠")
        }
    }

    private func visualizationFallback(_ visualization: ArticleVisualization) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(visualization.title)
                .font(.headline)
                .accessibilityAddTraits(.isHeader)
            Text(visualization.altText)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            ForEach(Array(visualization.series.enumerated()), id: \.offset) { _, series in
                VStack(alignment: .leading, spacing: 6) {
                    Text(series.name)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(Array(series.points.enumerated()), id: \.offset) { _, point in
                        HStack(alignment: .firstTextBaseline) {
                            Text(point.label)
                            Spacer(minLength: 16)
                            Text(point.rawValue)
                                .fontWeight(.semibold)
                        }
                        .font(.callout)
                    }
                }
            }
        }
        .padding(16)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(visualization.title)
        .accessibilityValue(visualization.altText)
    }

    private func articleImage(_ image: ArticleImage) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            AsyncImage(url: image.url, transaction: Transaction(animation: nil)) { phase in
                switch phase {
                case .success(let loaded):
                    loaded
                        .resizable()
                        .scaledToFill()
                case .failure:
                    ZStack {
                        Rectangle().fill(Color.secondary.opacity(0.10))
                        Image(systemName: "photo")
                            .font(.title2)
                            .foregroundStyle(.secondary)
                    }
                case .empty:
                    Rectangle().fill(Color.secondary.opacity(0.10))
                @unknown default:
                    Rectangle().fill(Color.secondary.opacity(0.10))
                }
            }
            .aspectRatio(16 / 9, contentMode: .fill)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .accessibilityLabel(image.caption?.isEmpty == false ? image.caption! : "기사 사진")
            if let caption = image.caption, !caption.isEmpty {
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var sourceFooter: some View {
        Group {
            if let sourceURL = item.sourceURL {
                Link(destination: sourceURL) {
                    Label("원문 보기", systemImage: "arrow.up.right")
                        .font(.callout.weight(.semibold))
                }
                .accessibilityHint("브라우저에서 기사 원문을 엽니다")
                .padding(.top, 20)
            }
        }
    }

    private func readingColumnWidth(for width: CGFloat) -> CGFloat {
        if width >= 1_000 { return 720 }
        if horizontalSizeClass == .regular { return min(680, width - 96) }
        return .infinity
    }

    private func horizontalInset(for width: CGFloat) -> CGFloat {
        width >= 760 ? 32 : compactInset
    }

    private func attributed(_ markdown: String) -> AttributedString {
        (try? AttributedString(markdown: markdown)) ?? AttributedString(markdown)
    }

    private var articleBackground: Color {
        #if os(iOS)
        Color(uiColor: .systemBackground)
        #elseif os(macOS)
        Color(nsColor: .textBackgroundColor)
        #else
        Color.clear
        #endif
    }
}
