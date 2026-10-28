import Foundation

public enum ArticleContentBlock: Equatable, Sendable {
    case markdown(String)
    case image(ArticleImage)
    case visualization(ArticleVisualization)
    case unsupported(String)
}

public struct ArticleMapParser: Sendable {
    private static let directive = try! NSRegularExpression(
        pattern: #"<([a-z][a-z0-9_]*):([^<>]+)>"#
    )

    public init() {}

    public func parse(_ map: ArticleMap) -> [ArticleContentBlock] {
        let body = map.bodyMarkdown
        let fullRange = NSRange(body.startIndex..<body.endIndex, in: body)
        let matches = Self.directive.matches(in: body, range: fullRange)
        var blocks: [ArticleContentBlock] = []
        var cursor = body.startIndex
        let imagesByReference = Dictionary(
            map.images.map { ($0.ref, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let visualizationsByReference = Dictionary(
            map.visualizations.map { ($0.ref, $0) },
            uniquingKeysWith: { first, _ in first }
        )

        for match in matches {
            guard
                let matchRange = Range(match.range(at: 0), in: body),
                let typeRange = Range(match.range(at: 1), in: body),
                let payloadRange = Range(match.range(at: 2), in: body)
            else { continue }

            appendText(String(body[cursor..<matchRange.lowerBound]), to: &blocks)
            let type = String(body[typeRange])
            let payload = String(body[payloadRange])
            let raw = String(body[matchRange])
            if type == "img", let reference = Int(payload), let image = imagesByReference[reference] {
                blocks.append(.image(image))
            } else if type == "chart",
                      let reference = Int(payload),
                      let visualization = visualizationsByReference[reference] {
                blocks.append(.visualization(visualization))
            } else {
                blocks.append(.unsupported(raw))
            }
            cursor = matchRange.upperBound
        }
        appendText(String(body[cursor...]), to: &blocks)
        return blocks
    }

    private func appendText(_ raw: String, to blocks: inout [ArticleContentBlock]) {
        let text = raw.trimmingCharacters(in: .newlines)
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        blocks.append(.markdown(text))
    }
}
