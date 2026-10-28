import CryptoKit
import Foundation

enum ArticleMediaCacheError: Error, Equatable {
    case unavailableOffline
    case invalidResponse
}

struct ThumbnailRetentionRequest: Equatable, Sendable {
    let url: URL
    let articleID: String
}

protocol ArticleMediaCaching: Sendable {
    func data(for url: URL, allowNetwork: Bool) async throws -> Data
    func prefetch(_ urls: [URL]) async
    func retainThumbnail(_ data: Data, for url: URL, articleID: String) async
    func releaseThumbnails(for articleID: String) async
    func remove(_ urls: [URL]) async
    func clearTransient() async
    func clear() async
    func diskUsage() async -> Int
}

actor ArticleMediaCache: ArticleMediaCaching {
    static let shared = ArticleMediaCache()

    private struct ThumbnailRetentionEntry: Codable, Equatable {
        let url: String
        var articleIDs: [String]
    }

    private struct ThumbnailRetentionManifest: Codable, Equatable {
        static let currentVersion = 1

        var version = currentVersion
        var entries: [String: ThumbnailRetentionEntry] = [:]
        var releasedArticleIDs: [String] = []

        private enum CodingKeys: String, CodingKey {
            case version
            case entries
            case releasedArticleIDs
        }

        init() {}

        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            version = try values.decodeIfPresent(Int.self, forKey: .version)
                ?? Self.currentVersion
            entries = try values.decodeIfPresent(
                [String: ThumbnailRetentionEntry].self,
                forKey: .entries
            ) ?? [:]
            releasedArticleIDs = try values.decodeIfPresent(
                [String].self,
                forKey: .releasedArticleIDs
            ) ?? []
        }
    }

    private struct NetworkImagePayload: @unchecked Sendable {
        let data: Data
        let response: URLResponse
    }

    private let cache: URLCache
    private let session: URLSession
    private let fileManager: FileManager
    private let retainedDirectory: URL
    private let manifestURL: URL
    private var retainedManifest: ThumbnailRetentionManifest?
    private var inFlight: [URL: Task<NetworkImagePayload, Error>] = [:]

    init(
        cache: URLCache = .shared,
        session: URLSession = .shared,
        retainedDirectory: URL? = nil,
        fileManager: FileManager = .default
    ) {
        self.cache = cache
        self.session = session
        self.fileManager = fileManager
        let resolvedDirectory = retainedDirectory
            ?? Self.defaultRetainedDirectory(fileManager: fileManager)
        self.retainedDirectory = resolvedDirectory
        manifestURL = resolvedDirectory.appendingPathComponent(
            "manifest.json",
            isDirectory: false
        )
    }

    func data(for url: URL, allowNetwork: Bool) async throws -> Data {
        if let retained = retainedData(for: url) {
            return retained
        }

        let cachedRequest = URLRequest(
            url: url,
            cachePolicy: .returnCacheDataDontLoad,
            timeoutInterval: 20
        )
        if let response = cache.cachedResponse(for: cachedRequest),
           !response.data.isEmpty {
            return response.data
        }
        guard allowNetwork else {
            throw ArticleMediaCacheError.unavailableOffline
        }

        if let task = inFlight[url] {
            return try await task.value.data
        }

        let request = URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 20
        )
        let task = Task { [session] in
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  !data.isEmpty,
                  http.mimeType?.lowercased().hasPrefix("image/") != false else {
                throw ArticleMediaCacheError.invalidResponse
            }
            return NetworkImagePayload(data: data, response: response)
        }
        inFlight[url] = task

        do {
            let payload = try await task.value
            inFlight.removeValue(forKey: url)
            cache.storeCachedResponse(
                CachedURLResponse(response: payload.response, data: payload.data),
                for: request
            )
            return payload.data
        } catch {
            inFlight.removeValue(forKey: url)
            throw error
        }
    }

    func prefetch(_ urls: [URL]) async {
        for url in Array(Set(urls)).prefix(15) {
            _ = try? await data(for: url, allowNetwork: true)
        }
    }

    func retainThumbnail(
        _ data: Data,
        for url: URL,
        articleID: String
    ) async {
        guard !data.isEmpty, Self.isValidArticleID(articleID) else { return }
        loadManifestIfNeeded()
        guard var manifest = retainedManifest else { return }
        guard !manifest.releasedArticleIDs.contains(articleID) else { return }

        let key = Self.cacheKey(for: url)
        let existingEntry = manifest.entries[key]
        guard existingEntry == nil || existingEntry?.url == url.absoluteString else {
            return
        }
        let imageURL = retainedImageURL(for: key)
        let fileExisted = fileManager.fileExists(atPath: imageURL.path)

        do {
            try prepareRetainedDirectory()
            if !fileExisted || existingEntry == nil {
                try data.write(to: imageURL, options: .atomic)
            }
        } catch {
            return
        }

        var articleIDs = existingEntry?.articleIDs ?? []
        if !articleIDs.contains(articleID) {
            articleIDs.append(articleID)
            articleIDs.sort()
        }
        manifest.entries[key] = ThumbnailRetentionEntry(
            url: url.absoluteString,
            articleIDs: articleIDs
        )

        guard persist(manifest) else {
            if existingEntry == nil {
                try? fileManager.removeItem(at: imageURL)
            }
            return
        }
        retainedManifest = manifest
        cache.removeCachedResponse(for: URLRequest(url: url))
    }

    func releaseThumbnails(for articleID: String) async {
        guard Self.isValidArticleID(articleID) else { return }
        loadManifestIfNeeded()
        guard let current = retainedManifest else { return }

        var candidate = current
        var releasedArticleIDs = Set(candidate.releasedArticleIDs)
        let insertedTombstone = releasedArticleIDs.insert(articleID).inserted
        candidate.releasedArticleIDs = releasedArticleIDs.sorted()
        var removedKeys: [String] = []
        var changed = insertedTombstone
        for (key, entry) in current.entries where entry.articleIDs.contains(articleID) {
            changed = true
            let remaining = entry.articleIDs.filter { $0 != articleID }
            if remaining.isEmpty {
                candidate.entries.removeValue(forKey: key)
                removedKeys.append(key)
            } else {
                candidate.entries[key] = ThumbnailRetentionEntry(
                    url: entry.url,
                    articleIDs: remaining
                )
            }
        }
        guard changed, persist(candidate) else { return }

        retainedManifest = candidate
        for key in removedKeys {
            try? fileManager.removeItem(at: retainedImageURL(for: key))
        }
    }

    func remove(_ urls: [URL]) async {
        let uniqueURLs = Set(urls)
        for url in uniqueURLs {
            cache.removeCachedResponse(for: URLRequest(url: url))
        }

        loadManifestIfNeeded()
        guard let current = retainedManifest else { return }
        var candidate = current
        var removedKeys: [String] = []
        for url in uniqueURLs {
            let key = Self.cacheKey(for: url)
            guard candidate.entries[key]?.url == url.absoluteString else { continue }
            candidate.entries.removeValue(forKey: key)
            removedKeys.append(key)
        }
        guard !removedKeys.isEmpty, persist(candidate) else { return }

        retainedManifest = candidate
        for key in removedKeys {
            try? fileManager.removeItem(at: retainedImageURL(for: key))
        }
    }

    func clearTransient() async {
        cache.removeAllCachedResponses()
    }

    func clear() async {
        cache.removeAllCachedResponses()
        loadManifestIfNeeded()
        guard let current = retainedManifest else { return }
        let empty = ThumbnailRetentionManifest()
        guard persist(empty) else { return }

        retainedManifest = empty
        for key in current.entries.keys {
            try? fileManager.removeItem(at: retainedImageURL(for: key))
        }
    }

    func diskUsage() async -> Int {
        let retainedBytes = (
            try? fileManager.contentsOfDirectory(
                at: retainedDirectory,
                includingPropertiesForKeys: [.fileSizeKey],
                options: [.skipsHiddenFiles]
            )
        )?.reduce(into: 0) { total, url in
            total += (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        } ?? 0
        return cache.currentDiskUsage + retainedBytes
    }

    private func retainedData(for url: URL) -> Data? {
        loadManifestIfNeeded()
        let key = Self.cacheKey(for: url)
        guard let entry = retainedManifest?.entries[key],
              entry.url == url.absoluteString,
              !entry.articleIDs.isEmpty,
              let data = try? Data(contentsOf: retainedImageURL(for: key)),
              !data.isEmpty else {
            return nil
        }
        return data
    }

    private func loadManifestIfNeeded() {
        guard retainedManifest == nil else { return }
        guard let data = try? Data(contentsOf: manifestURL),
              let decoded = try? JSONDecoder().decode(
                  ThumbnailRetentionManifest.self,
                  from: data
              ),
              decoded.version == ThumbnailRetentionManifest.currentVersion else {
            retainedManifest = ThumbnailRetentionManifest()
            return
        }

        var normalized = ThumbnailRetentionManifest()
        normalized.releasedArticleIDs = Array(
            Set(decoded.releasedArticleIDs.filter(Self.isValidArticleID))
        ).sorted()
        for (key, entry) in decoded.entries {
            guard let url = URL(string: entry.url),
                  Self.cacheKey(for: url) == key else {
                continue
            }
            let articleIDs = Array(
                Set(entry.articleIDs.filter(Self.isValidArticleID))
            ).sorted()
            guard !articleIDs.isEmpty,
                  let data = try? Data(contentsOf: retainedImageURL(for: key)),
                  !data.isEmpty else {
                continue
            }
            normalized.entries[key] = ThumbnailRetentionEntry(
                url: entry.url,
                articleIDs: articleIDs
            )
        }
        retainedManifest = normalized
    }

    private func persist(_ manifest: ThumbnailRetentionManifest) -> Bool {
        do {
            try prepareRetainedDirectory()
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            try encoder.encode(manifest).write(to: manifestURL, options: .atomic)
            return true
        } catch {
            return false
        }
    }

    private func prepareRetainedDirectory() throws {
        try fileManager.createDirectory(
            at: retainedDirectory,
            withIntermediateDirectories: true
        )
        var directory = retainedDirectory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try directory.setResourceValues(values)
    }

    private func retainedImageURL(for key: String) -> URL {
        retainedDirectory.appendingPathComponent("\(key).image", isDirectory: false)
    }

    private static func defaultRetainedDirectory(fileManager: FileManager) -> URL {
        let root = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.temporaryDirectory
        return root
            .appendingPathComponent("ArticleThumbnailCache", isDirectory: true)
            .appendingPathComponent("v1", isDirectory: true)
    }

    private static func cacheKey(for url: URL) -> String {
        SHA256.hash(data: Data(url.absoluteString.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func isValidArticleID(_ value: String) -> Bool {
        !value.isEmpty
            && value.count <= 128
            && !value.contains { $0 == "\n" || $0 == "\r" || $0 == "\0" }
    }
}

actor MemoryArticleMediaCache: ArticleMediaCaching {
    private var values: [URL: Data]
    private var thumbnailOwners: [URL: Set<String>] = [:]
    private var releasedThumbnailOwners: Set<String> = []
    private(set) var requested: [URL] = []
    private(set) var removed: [URL] = []
    private(set) var retainedThumbnailRequests: [ThumbnailRetentionRequest] = []
    private(set) var releasedArticleIDs: [String] = []
    private(set) var clearTransientCallCount = 0
    private(set) var clearCallCount = 0

    init(values: [URL: Data] = [:]) {
        self.values = values
    }

    func data(for url: URL, allowNetwork: Bool) async throws -> Data {
        requested.append(url)
        if let data = values[url] { return data }
        guard allowNetwork else { throw ArticleMediaCacheError.unavailableOffline }
        throw ArticleMediaCacheError.invalidResponse
    }

    func prefetch(_ urls: [URL]) async {
        requested.append(contentsOf: urls)
    }

    func retainThumbnail(
        _ data: Data,
        for url: URL,
        articleID: String
    ) async {
        guard !data.isEmpty, !articleID.isEmpty else { return }
        guard !releasedThumbnailOwners.contains(articleID) else { return }
        var owners = thumbnailOwners[url] ?? []
        if owners.insert(articleID).inserted {
            retainedThumbnailRequests.append(
                ThumbnailRetentionRequest(url: url, articleID: articleID)
            )
        }
        thumbnailOwners[url] = owners
        values[url] = data
    }

    func releaseThumbnails(for articleID: String) async {
        releasedArticleIDs.append(articleID)
        releasedThumbnailOwners.insert(articleID)
        for url in Array(thumbnailOwners.keys) {
            guard var owners = thumbnailOwners[url] else { continue }
            owners.remove(articleID)
            if owners.isEmpty {
                thumbnailOwners.removeValue(forKey: url)
                values.removeValue(forKey: url)
            } else {
                thumbnailOwners[url] = owners
            }
        }
    }

    func remove(_ urls: [URL]) async {
        var seen = Set<URL>()
        for url in urls where seen.insert(url).inserted {
            removed.append(url)
            thumbnailOwners.removeValue(forKey: url)
            values.removeValue(forKey: url)
        }
    }

    func clearTransient() async {
        clearTransientCallCount += 1
        values = values.filter { thumbnailOwners[$0.key]?.isEmpty == false }
    }

    func clear() async {
        clearCallCount += 1
        thumbnailOwners.removeAll()
        releasedThumbnailOwners.removeAll()
        values.removeAll()
    }

    func diskUsage() async -> Int {
        values.values.reduce(0) { $0 + $1.count }
    }
}
