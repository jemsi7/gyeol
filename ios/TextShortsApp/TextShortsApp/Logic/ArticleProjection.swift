import ArticleFeedKit
import Foundation

struct ArticleDisplayPolicy: Sendable {
    func allows(_ item: ArticleFeedItem) -> Bool {
        item.map.headline.unicodeScalars.contains { scalar in
            switch scalar.value {
            case 0x1100...0x11FF,
                 0x3130...0x318F,
                 0xA960...0xA97F,
                 0xAC00...0xD7A3,
                 0xD7B0...0xD7FF,
                 0xFFA0...0xFFDC:
                return true
            default:
                return false
            }
        }
    }

    func filter(_ items: [ArticleFeedItem]) -> [ArticleFeedItem] {
        items.filter(allows)
    }

    func filter(_ response: DiscoveryResponse) -> DiscoveryResponse {
        let routes = response.routes.compactMap { route -> DiscoveryRoute? in
            let allowedItems = route.items.filter { allows($0.article) }
            guard !allowedItems.isEmpty else { return nil }
            let compactedItems = allowedItems.enumerated().map { index, item in
                DiscoveryRouteItem(
                    position: index + 1,
                    role: item.role,
                    reason: item.reason,
                    lastingValue: item.lastingValue,
                    article: item.article
                )
            }
            return DiscoveryRoute(
                id: route.id,
                rank: route.rank,
                slug: route.slug,
                title: route.title,
                summary: route.summary,
                topic: route.topic,
                isRepresentative: route.isRepresentative,
                items: compactedItems
            )
        }
        let retainedRepresentativeID = response.representativeRouteID.flatMap { candidate in
            routes.contains(where: { $0.id == candidate }) ? candidate : nil
        }
        let timelines = response.featuredTimelines.compactMap(displayableTimeline)
        let timelineResults = response.timelineResults.compactMap(displayableTimeline)
        return DiscoveryResponse(
            formatVersion: response.formatVersion,
            generatedAt: response.generatedAt,
            expiresAt: response.expiresAt,
            representativeRouteID: retainedRepresentativeID ?? routes.first?.id,
            routes: routes,
            searchSuggestions: response.searchSuggestions,
            trendingSearchSuggestions: response.trendingSearchSuggestions,
            query: response.query,
            results: response.results.filter { allows($0.article) },
            cooldownSeconds: response.cooldownSeconds,
            collection: response.collection,
            timelineStatus: response.timelineStatus,
            timelinesGeneratedAt: response.timelinesGeneratedAt,
            timelinesRefreshedAt: response.timelinesRefreshedAt,
            featuredTimelines: timelines,
            timelineResults: timelineResults,
            nextCursor: response.nextCursor
        )
    }

    private func displayableTimeline(_ timeline: FeaturedTimeline) -> FeaturedTimeline? {
        let allowed = timeline.nodes.filter { allows($0.article) }
        let mainIDs = Set(allowed.lazy.filter { $0.lane == .main }.map(\.id))
        let retained = allowed.filter { node in
            node.lane == .main || node.anchorNodeID.map(mainIDs.contains) == true
        }
        if isValidTimelineNodes(retained) {
            return FeaturedTimeline(
                id: timeline.id,
                rank: timeline.rank,
                stableKey: timeline.stableKey,
                title: timeline.title,
                summary: timeline.summary,
                topic: timeline.topic,
                startedAt: timeline.startedAt,
                endedAt: timeline.endedAt,
                nodes: retained
            )
        }

        return isValidTimelineNodes(timeline.nodes) ? timeline : nil
    }

    private func isValidTimelineNodes(_ nodes: [FeaturedTimelineNode]) -> Bool {
        guard (4...8).contains(nodes.count), Set(nodes.map(\.id)).count == nodes.count else {
            return false
        }
        let mainIDs = Set(nodes.lazy.filter { $0.lane == .main }.map(\.id))
        guard mainIDs.count >= 3 else { return false }
        return nodes.allSatisfy { node in
            if node.lane == .main {
                return node.anchorNodeID == nil
            }
            return node.anchorNodeID.map(mainIDs.contains) == true
        }
    }
}

struct ArticleProjector: Sendable {
    private let parser = ArticleMapParser()
    private let displayPolicy = ArticleDisplayPolicy()

    func project(_ items: [ArticleFeedItem]) -> [PrototypeArticle] {
        displayPolicy.filter(items).map(project)
    }

    func projectDisplayable(_ item: ArticleFeedItem) -> PrototypeArticle? {
        guard displayPolicy.allows(item) else { return nil }
        return project(item)
    }

    func project(_ item: ArticleFeedItem) -> PrototypeArticle {
        if hasRenderableV3Scenes(item.map) {
            return projectV3(item)
        }
        return projectLegacy(item)
    }

    private func projectLegacy(_ item: ArticleFeedItem) -> PrototypeArticle {
        let blocks = parser.parse(item.map)
        var drafts: [SceneDraft] = []
        var current = SceneDraft()

        func flush() {
            guard !current.isEmpty else { return }
            drafts.append(current)
            current = SceneDraft()
        }

        for block in blocks {
            switch block {
            case let .markdown(markdown):
                for rawPart in markdown.components(separatedBy: "\n\n") {
                    let part = normalize(rawPart)
                    guard !part.isEmpty else { continue }
                    if part.hasPrefix(">") {
                        flush()
                        current.quote = normalize(String(part.dropFirst()))
                        flush()
                    } else {
                        if current.paragraphs.count == 2 { flush() }
                        current.paragraphs.append(part)
                    }
                }
            case let .image(image):
                flush()
                current.image = image
            case let .visualization(visualization):
                flush()
                current.visualization = visualization
                flush()
            case let .unsupported(raw):
                if current.paragraphs.count == 2 { flush() }
                current.paragraphs.append(raw)
            }
        }
        flush()

        if drafts.isEmpty {
            drafts = [SceneDraft(paragraphs: [item.map.summary], image: nil, quote: nil)]
        }

        let source = sourceName(item)
        let scenes = drafts.enumerated().map { index, draft in
            let firstParagraph = draft.paragraphs.first
                ?? draft.quote
                ?? draft.visualization?.altText
                ?? item.map.summary
            return PrototypeScene(
                id: index,
                label: label(for: index, keywords: item.map.keywords),
                title: draft.visualization?.title
                    ?? (index == 0 ? item.map.headline : derivedTitle(from: firstParagraph, index: index)),
                summary: draft.visualization?.altText ?? (index == 0 ? item.map.summary : ""),
                body: draft.paragraphs.first ?? "",
                secondary: draft.paragraphs.dropFirst().first ?? "",
                artwork: nil,
                artworkLabel: draft.image?.caption ?? item.map.headline,
                caption: draft.image?.caption ?? "",
                quote: draft.quote ?? "",
                remoteImageURL: draft.image?.url,
                remoteImageID: draft.image?.imageID,
                visualization: draft.visualization
            )
        }

        return PrototypeArticle(
            id: item.articleID,
            source: source,
            domain: item.domain ?? item.sourceURL?.host ?? source,
            readTime: readTime(for: item.map.bodyMarkdown),
            date: dateString(item.publishedAt),
            scenes: scenes,
            keywords: Array(item.map.keywords.prefix(3)),
            sourceURL: item.sourceURL,
            status: item.status == .degraded ? .degraded : .ok,
            publishedAt: item.publishedAt,
            headline: item.map.headline,
            documentType: item.documentType,
            sourcePublisher: item.sourcePublisher,
            publisherContext: item.map.publisherContext,
            revisionNote: item.revisionNote,
            chainProgressNote: item.chainProgressNote
        )
    }

    private func projectV3(_ item: ArticleFeedItem) -> PrototypeArticle {
        let imagesByReference = Dictionary(
            item.map.images.map { ($0.ref, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let visualizationsByReference = Dictionary(
            item.map.visualizations.map { ($0.ref, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let sceneImageRefs = Set(item.map.scenes.compactMap(\.imageRef))
        let coverImage: ArticleImage?
        if let coverRef = item.map.coverImageRef {
            coverImage = sceneImageRefs.contains(coverRef) ? nil : imagesByReference[coverRef]
        } else {
            coverImage = item.map.images.first { image in
                image.role?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "social"
                    && !sceneImageRefs.contains(image.ref)
            }
        }
        let scenes = item.map.scenes.enumerated().map { index, scene in
            let explicitImage = scene.imageRef.flatMap { imagesByReference[$0] }
            let image = explicitImage ?? (index == 0 ? coverImage : nil)
            let visualization = scene.chartRef.flatMap { visualizationsByReference[$0] }
            return PrototypeScene(
                id: index,
                label: scene.label,
                title: scene.title,
                summary: scene.summary,
                body: scene.body,
                secondary: scene.secondary,
                artwork: nil,
                artworkLabel: image?.caption ?? item.map.headline,
                caption: image?.caption ?? "",
                quote: scene.quote,
                stalenessNote: scene.stalenessNote,
                remoteImageURL: image?.url,
                remoteImageID: image?.imageID,
                visualization: visualization,
                role: scene.role
            )
        }

        let source = sourceName(item)
        return PrototypeArticle(
            id: item.articleID,
            source: source,
            domain: item.domain ?? item.sourceURL?.host ?? source,
            readTime: readTime(for: item.map.bodyMarkdown),
            date: dateString(item.publishedAt),
            scenes: scenes,
            keywords: Array(item.map.keywords.prefix(3)),
            sourceURL: item.sourceURL,
            status: item.status == .degraded ? .degraded : .ok,
            publishedAt: item.publishedAt,
            headline: item.map.headline,
            documentType: item.documentType,
            sourcePublisher: item.sourcePublisher,
            publisherContext: item.map.publisherContext,
            revisionNote: item.revisionNote,
            chainProgressNote: item.chainProgressNote
        )
    }

    private func hasRenderableV3Scenes(_ map: ArticleMap) -> Bool {
        let sceneIDs = map.scenes.map(\.id)
        return map.formatVersion >= 3
            && (3...9).contains(map.scenes.count)
            && Set(sceneIDs).count == sceneIDs.count
            && map.scenes.allSatisfy { scene in
                !scene.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    && !scene.role.rawValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    && !scene.label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    && !scene.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
    }

    private func sourceName(_ item: ArticleFeedItem) -> String {
        let raw = item.domain ?? item.sourceURL?.host ?? "출처 미상"
        return raw.replacingOccurrences(of: "www.", with: "")
    }

    private func label(for index: Int, keywords: [String]) -> String {
        if keywords.indices.contains(index) { return keywords[index] }
        return index == 0 ? "핵심" : "장면 \(index + 1)"
    }

    private func derivedTitle(from text: String, index: Int) -> String {
        let firstSentence = text.split(whereSeparator: { ".!?。！？".contains($0) }).first.map(String.init) ?? text
        let clipped = String(firstSentence.prefix(28)).trimmingCharacters(in: .whitespacesAndNewlines)
        return clipped.isEmpty ? "이어지는 장면 \(index + 1)" : clipped
    }

    private func readTime(for text: String) -> String {
        let meaningful = text.filter { !$0.isWhitespace }.count
        return "\(max(1, Int(ceil(Double(meaningful) / 450.0))))분 읽기"
    }

    private func dateString(_ date: Date?) -> String {
        guard let date else { return "날짜 미상" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.dateFormat = "yyyy. M. d."
        return formatter.string(from: date)
    }

    private func normalize(_ raw: String) -> String {
        raw
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: #"^#{1,6}\s*"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "__", with: "")
    }
}

private struct SceneDraft {
    var paragraphs: [String] = []
    var image: ArticleImage?
    var quote: String?
    var visualization: ArticleVisualization?

    var isEmpty: Bool {
        paragraphs.isEmpty && image == nil && quote == nil && visualization == nil
    }
}
