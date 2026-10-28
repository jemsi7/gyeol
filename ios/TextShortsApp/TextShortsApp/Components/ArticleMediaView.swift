import SwiftUI
import UIKit

@MainActor
final class ArticleMediaLoader: ObservableObject {
    enum State { case idle, loading, loaded(UIImage), failed }
    @Published private(set) var state: State = .idle

    func load(
        url: URL,
        allowNetwork: Bool,
        thumbnailArticleID: String?,
        cache: any ArticleMediaCaching
    ) async {
        if case .loading = state { return }
        state = .loading
        do {
            let data = try await cache.data(for: url, allowNetwork: allowNetwork)
            guard let image = UIImage(data: data) else {
                state = .failed
                return
            }
            if let thumbnailArticleID {
                await cache.retainThumbnail(
                    data,
                    for: url,
                    articleID: thumbnailArticleID
                )
            }
            state = .loaded(image)
        } catch is CancellationError {
            state = .idle
        } catch {
            state = .failed
        }
    }
}

struct ArticleMediaView: View {
    let url: URL?
    let fallback: ArtworkKind
    let accessibilityLabel: String
    let mediaSaver: Bool
    let offline: Bool
    let palette: AppPalette
    var thumbnailArticleID: String? = nil
    var contentMode: ContentMode = .fill
    var preservesNaturalRatio = false
    var cache: any ArticleMediaCaching = ArticleMediaCache.shared

    @StateObject private var loader = ArticleMediaLoader()
    @State private var requested = false
    @State private var retryNonce = 0

    @ViewBuilder
    var body: some View {
        if preservesNaturalRatio {
            mediaBody.aspectRatio(resolvedAspectRatio, contentMode: .fit)
        } else {
            mediaBody
        }
    }

    private var mediaBody: some View {
        ZStack {
            switch loader.state {
            case let .loaded(image):
                if contentMode == .fit {
                    Image(uiImage: image).resizable().scaledToFit()
                } else {
                    Image(uiImage: image).resizable().scaledToFill()
                }
            case .loading:
                palette.deep
                ProgressView().tint(palette.accentDeep)
            case .idle:
                if url == nil { EditorialArtwork(kind: fallback) }
                else { palette.deep }
            case .failed:
                if url == nil { EditorialArtwork(kind: fallback) }
                else { palette.deep }
            }

            if mediaSaver, !requested, url != nil {
                Button {
                    requested = true
                } label: {
                    Label("사진 불러오기", systemImage: "photo")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(palette.ink)
                        .padding(.horizontal, 14)
                        .frame(minHeight: 44)
                        .background(palette.raised.opacity(0.96))
                        .overlay(Rectangle().stroke(palette.ruleStrong, lineWidth: 1))
                }
                .buttonStyle(.plain)
            } else if case .failed = loader.state, url != nil {
                if offline {
                    Image(systemName: "wifi.slash")
                        .font(.system(size: 27, weight: .medium))
                        .symbolRenderingMode(.monochrome)
                        .foregroundColor(palette.inkSoft)
                        .frame(width: 44, height: 44)
                        .accessibilityHidden(true)
                } else {
                    Button {
                        retryNonce += 1
                    } label: {
                        Image(systemName: "arrow.clockwise.circle")
                            .font(.system(size: 27, weight: .medium))
                            .symbolRenderingMode(.monochrome)
                            .foregroundColor(palette.accentDeep)
                            .frame(width: 44, height: 44)
                            .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("사진 다시 불러오기")
                    .accessibilityHint("사진을 다시 요청합니다")
                }
            }
        }
        .clipped()
        .accessibilityLabel(resolvedAccessibilityLabel)
        .task(id: taskID) {
            guard let url, !mediaSaver || requested else { return }
            await loader.load(
                url: url,
                allowNetwork: !offline,
                thumbnailArticleID: thumbnailArticleID,
                cache: cache
            )
        }
    }

    private var resolvedAspectRatio: CGFloat {
        if case let .loaded(image) = loader.state, image.size.height > 0 {
            return image.size.width / image.size.height
        }
        return 4 / 3
    }

    private var resolvedAccessibilityLabel: String {
        if case .failed = loader.state, url != nil {
            return "이미지를 불러오지 못했습니다. \(accessibilityLabel)"
        }
        return accessibilityLabel
    }

    private var taskID: String {
        "\(url?.absoluteString ?? "local")|\(thumbnailArticleID ?? "transient")|\(mediaSaver)|\(offline)|\(requested)|\(retryNonce)"
    }
}
