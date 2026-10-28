import ArticleFeedKit
import Foundation
import UIKit
import UserNotifications

enum EditorialPushState: Equatable, Sendable {
    case signedOut
    case disabled
    case requesting
    case registering
    case enabled
    case denied
    case failed(String)

    var isDeliveryActive: Bool {
        switch self {
        case .requesting, .registering, .enabled:
            return true
        case .signedOut, .disabled, .denied, .failed:
            return false
        }
    }

    var canChangeSwitch: Bool {
        switch self {
        case .signedOut, .requesting, .registering:
            return false
        case .disabled, .enabled, .denied, .failed:
            return true
        }
    }
}

enum PushAuthorizationStatus: Equatable, Sendable {
    case notDetermined
    case denied
    case authorized
}

enum NotificationPreferenceKind: Equatable, Sendable {
    case editorial
    case weeklyInsight
    case timelineUpdates
    case timelineRefreshes
    case timelineAdditions
}

@MainActor
protocol PushAuthorizationServicing: AnyObject {
    func authorizationStatus() async -> PushAuthorizationStatus
    func requestAuthorization() async throws -> Bool
    func registerForRemoteNotifications()
}

@MainActor
final class SystemPushAuthorizationService: PushAuthorizationServicing {
    func authorizationStatus() async -> PushAuthorizationStatus {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined: return .notDetermined
        case .denied: return .denied
        case .authorized, .provisional, .ephemeral: return .authorized
        @unknown default: return .denied
        }
    }

    func requestAuthorization() async throws -> Bool {
        try await UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]
        )
    }

    func registerForRemoteNotifications() {
        UIApplication.shared.registerForRemoteNotifications()
    }
}

protocol PushAppAttestManaging: AnyObject {
    func makeAssertion(
        payload: PushRegistrationPayload,
        recoverySessionToken: String
    ) async throws -> AssertionEnvelope
}

extension AppAttestManager: PushAppAttestManaging {
    func makeAssertion(
        payload: PushRegistrationPayload,
        recoverySessionToken: String
    ) async throws -> AssertionEnvelope {
        try await makeAssertion(
            payload: payload,
            recoverySessionToken: Optional(recoverySessionToken)
        )
    }
}

@MainActor
protocol EditorialPushServicing: AnyObject {
    func authorizationStatus() async -> PushAuthorizationStatus
    func preferences() -> PushNotificationPreferences
    func isOptedIn() -> Bool
    func requestAuthorizationForCurrentPreferences() async throws -> EditorialPushState
    func setEnabled(
        _ kind: NotificationPreferenceKind,
        enabled: Bool
    ) async throws -> EditorialPushState
    func refresh() async -> EditorialPushState
    func enable() async throws -> EditorialPushState
    func disable() async
    func register(deviceToken: Data) async throws
    func unregister() async
}

@MainActor
final class LiveEditorialPushService: EditorialPushServicing {
    private let client: any PushRegistrationAPIClientProtocol
    private let appAttest: any PushAppAttestManaging
    private let credentials: any AccountCredentialStoring
    private let device: DeviceInfo
    private let environment: APNsEnvironment
    private let authorization: any PushAuthorizationServicing
    private let defaults: UserDefaults
    private let legacyOptInKey: String
    private let editorialPreferenceKey: String
    private let weeklyPreferenceKey: String
    private let timelinePreferenceKey: String
    private let timelineRefreshPreferenceKey: String
    private let timelineAdditionPreferenceKey: String
    private let v2MigrationKey: String
    private let defaultPreferenceMigrationKey: String

    init(
        client: any PushRegistrationAPIClientProtocol,
        appAttest: any PushAppAttestManaging,
        credentials: any AccountCredentialStoring,
        device: DeviceInfo,
        environment: APNsEnvironment,
        authorization: (any PushAuthorizationServicing)? = nil,
        defaults: UserDefaults = .standard,
        namespace: String = "TextShortsApp"
    ) {
        self.client = client
        self.appAttest = appAttest
        self.credentials = credentials
        self.device = device
        self.environment = environment
        self.authorization = authorization ?? SystemPushAuthorizationService()
        self.defaults = defaults
        legacyOptInKey = "\(namespace).editorialPushOptIn"
        editorialPreferenceKey = "\(namespace).pushPreference.editorial"
        weeklyPreferenceKey = "\(namespace).pushPreference.weeklyInsight"
        timelinePreferenceKey = "\(namespace).pushPreference.timelineUpdates"
        timelineRefreshPreferenceKey = "\(namespace).pushPreference.timelineRefreshes"
        timelineAdditionPreferenceKey = "\(namespace).pushPreference.timelineAdditions"
        v2MigrationKey = "\(namespace).pushPreference.v2Migrated"
        defaultPreferenceMigrationKey = "\(namespace).pushPreference.v3Defaults"
        migratePreferenceDefaultsIfNeeded()
    }

    func authorizationStatus() async -> PushAuthorizationStatus {
        await authorization.authorizationStatus()
    }

    func preferences() -> PushNotificationPreferences {
        let legacyTimeline = defaults.bool(forKey: timelinePreferenceKey)
        return PushNotificationPreferences(
            editorial: defaults.bool(forKey: editorialPreferenceKey),
            weeklyInsight: defaults.bool(forKey: weeklyPreferenceKey),
            timelineUpdates: legacyTimeline,
            timelineRefreshes: (defaults.object(forKey: timelineRefreshPreferenceKey) as? Bool)
                ?? legacyTimeline,
            timelineAdditions: (defaults.object(forKey: timelineAdditionPreferenceKey) as? Bool)
                ?? legacyTimeline
        )
    }

    func isOptedIn() -> Bool {
        preferences().anyEnabled
    }

    func requestAuthorizationForCurrentPreferences() async throws -> EditorialPushState {
        guard validSession() != nil else { return .signedOut }
        guard preferences().anyEnabled else { return .disabled }
        var status = await authorization.authorizationStatus()
        if status == .notDetermined {
            let granted = try await authorization.requestAuthorization()
            status = granted ? .authorized : .denied
        }
        guard status == .authorized else { return .denied }
        authorization.registerForRemoteNotifications()
        return .registering
    }

    func refresh() async -> EditorialPushState {
        guard validSession() != nil else { return .signedOut }
        guard preferences().anyEnabled else { return .disabled }
        switch await authorization.authorizationStatus() {
        case .notDetermined: return .disabled
        case .denied: return .denied
        case .authorized:
            authorization.registerForRemoteNotifications()
            return .registering
        }
    }

    func enable() async throws -> EditorialPushState {
        try await setEnabled(.editorial, enabled: true)
    }

    func setEnabled(
        _ kind: NotificationPreferenceKind,
        enabled: Bool
    ) async throws -> EditorialPushState {
        guard validSession() != nil else { return .signedOut }
        let current = preferences()
        var candidate = current
        switch kind {
        case .editorial: candidate.editorial = enabled
        case .weeklyInsight: candidate.weeklyInsight = enabled
        case .timelineUpdates: candidate.timelineUpdates = enabled
        case .timelineRefreshes: candidate.timelineRefreshes = enabled
        case .timelineAdditions: candidate.timelineAdditions = enabled
        }
        guard candidate != current else {
            return candidate.anyEnabled ? .enabled : .disabled
        }
        guard let session = validSession() else { return .signedOut }
        if !candidate.anyEnabled {
            try await client.unregisterPush(
                installationID: device.installationID,
                sessionToken: session.sessionToken
            )
            save(candidate)
            return .disabled
        }
        var status = await authorization.authorizationStatus()
        if status == .notDetermined {
            let granted = try await authorization.requestAuthorization()
            status = granted ? .authorized : .denied
        }
        if current.anyEnabled {
            do {
                let response = try await client.updatePushPreferences(
                    installationID: device.installationID,
                    preferences: candidate,
                    sessionToken: session.sessionToken
                )
                try validate(response, expected: candidate)
                save(candidate)
                return status == .authorized ? .enabled : .denied
            } catch let ArticleFeedError.server(code, _)
                where code == "push_registration_not_found" {
                save(candidate)
                guard status == .authorized else { return .denied }
                authorization.registerForRemoteNotifications()
                return .registering
            }
        }
        save(candidate)
        guard status == .authorized else { return .denied }
        authorization.registerForRemoteNotifications()
        return .registering
    }

    func disable() async {
        await unregister()
        save(.none)
    }

    func register(deviceToken: Data) async throws {
        guard isOptedIn(), await authorization.authorizationStatus() == .authorized else {
            return
        }
        guard let session = validSession() else {
            throw AccountAuthenticationError.noSession
        }
        let payload = PushRegistrationPayload(
            installationID: device.installationID,
            deviceToken: Self.base64URL(deviceToken),
            environment: environment,
            preferences: preferences(),
            device: device
        )
        let assertion = try await appAttest.makeAssertion(
            payload: payload,
            recoverySessionToken: session.sessionToken
        )
        let response = try await client.registerPush(
            payload: payload,
            auth: assertion,
            sessionToken: session.sessionToken
        )
        try validate(response, expected: preferences())
    }

    func unregister() async {
        guard let session = validSession() else { return }
        try? await client.unregisterPush(
            installationID: device.installationID,
            sessionToken: session.sessionToken
        )
    }

    private func validSession() -> AccountSession? {
        guard let session = try? credentials.load(), session.expiresAt > Date() else {
            return nil
        }
        return session
    }

    private func migratePreferenceDefaultsIfNeeded() {
        guard !defaults.bool(forKey: defaultPreferenceMigrationKey) else { return }
        let hasV2Migration = defaults.bool(forKey: v2MigrationKey)
        let hasEditorialPreference = defaults.object(forKey: editorialPreferenceKey) != nil
        let hasWeeklyPreference = defaults.object(forKey: weeklyPreferenceKey) != nil
        let legacyPreference = defaults.object(forKey: legacyOptInKey) as? Bool

        if !hasV2Migration,
           !hasEditorialPreference,
           !hasWeeklyPreference,
           legacyPreference == nil {
            save(.allEnabled)
        } else if !hasV2Migration {
            if !hasEditorialPreference {
                defaults.set(legacyPreference ?? false, forKey: editorialPreferenceKey)
            }
            if !hasWeeklyPreference {
                defaults.set(false, forKey: weeklyPreferenceKey)
            }
        }
        defaults.set(true, forKey: v2MigrationKey)
        defaults.set(true, forKey: defaultPreferenceMigrationKey)
    }

    private func save(_ value: PushNotificationPreferences) {
        defaults.set(value.editorial, forKey: editorialPreferenceKey)
        defaults.set(value.weeklyInsight, forKey: weeklyPreferenceKey)
        defaults.set(value.timelineUpdates, forKey: timelinePreferenceKey)
        defaults.set(value.timelineRefreshes, forKey: timelineRefreshPreferenceKey)
        defaults.set(value.timelineAdditions, forKey: timelineAdditionPreferenceKey)
        defaults.set(value.anyEnabled, forKey: legacyOptInKey)
    }

    private func validate(
        _ response: PushRegistrationResponse,
        expected: PushNotificationPreferences
    ) throws {
        guard response.active,
              response.installationID == device.installationID,
              response.environment == environment,
              response.preferences == expected else {
            throw ArticleFeedError.invalidResponse
        }
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

final class MemoryEditorialPushService: EditorialPushServicing {
    var status: PushAuthorizationStatus
    private var storedPreferences: PushNotificationPreferences
    var optedIn: Bool {
        get { storedPreferences.anyEnabled }
        set { storedPreferences = newValue ? .editorialOnly : .none }
    }
    var enableResult: EditorialPushState
    var registerError: ArticleFeedError?
    var authorizationRequestError: ArticleFeedError?
    var registeredTokens: [Data] = []
    private(set) var enableCount = 0
    private(set) var preferenceUpdateCount = 0
    private(set) var authorizationRequestCount = 0
    private(set) var unregisterCount = 0

    init(
        status: PushAuthorizationStatus = .notDetermined,
        optedIn: Bool = false,
        preferences: PushNotificationPreferences? = nil,
        enableResult: EditorialPushState = .registering,
        registerError: ArticleFeedError? = nil,
        authorizationRequestError: ArticleFeedError? = nil
    ) {
        self.status = status
        storedPreferences = preferences ?? (optedIn ? .editorialOnly : .none)
        self.enableResult = enableResult
        self.registerError = registerError
        self.authorizationRequestError = authorizationRequestError
    }

    func authorizationStatus() async -> PushAuthorizationStatus { status }
    func preferences() -> PushNotificationPreferences { storedPreferences }
    func isOptedIn() -> Bool { optedIn }
    func requestAuthorizationForCurrentPreferences() async throws -> EditorialPushState {
        guard storedPreferences.anyEnabled else { return .disabled }
        authorizationRequestCount += 1
        if let authorizationRequestError { throw authorizationRequestError }
        return enableResult
    }
    func refresh() async -> EditorialPushState {
        guard storedPreferences.anyEnabled else { return .disabled }
        return enableResult
    }
    func enable() async throws -> EditorialPushState {
        try await setEnabled(.editorial, enabled: true)
    }
    func setEnabled(
        _ kind: NotificationPreferenceKind,
        enabled: Bool
    ) async throws -> EditorialPushState {
        let hadEnabledPreference = storedPreferences.anyEnabled
        var candidate = storedPreferences
        switch kind {
        case .editorial: candidate.editorial = enabled
        case .weeklyInsight: candidate.weeklyInsight = enabled
        case .timelineUpdates: candidate.timelineUpdates = enabled
        case .timelineRefreshes: candidate.timelineRefreshes = enabled
        case .timelineAdditions: candidate.timelineAdditions = enabled
        }
        guard candidate != storedPreferences else {
            return candidate.anyEnabled ? .enabled : .disabled
        }
        preferenceUpdateCount += 1
        if enabled { enableCount += 1 }
        storedPreferences = candidate
        if !candidate.anyEnabled {
            await unregister()
            return .disabled
        }
        if hadEnabledPreference { return .enabled }
        return enabled ? enableResult : .enabled
    }
    func disable() async {
        await unregister()
        storedPreferences = .none
    }
    func register(deviceToken: Data) async throws {
        guard storedPreferences.anyEnabled else { return }
        if let registerError { throw registerError }
        registeredTokens.append(deviceToken)
    }
    func unregister() async { unregisterCount += 1 }
}

struct PendingPushArticle: Equatable, Sendable {
    let articleID: String
    let collection: DiscoveryCollection
}

struct PendingTimelinePush: Equatable, Sendable {
    let eventID: UUID
    let stableKey: String
    let nodeID: UUID
    let updateKind: TimelineUpdateKind
}

enum PendingPushDestination: Equatable, Sendable {
    case article(PendingPushArticle)
    case timeline(PendingTimelinePush)
}

@MainActor
final class PushNotificationBridge: ObservableObject {
    static let shared = PushNotificationBridge()

    @Published private(set) var latestDeviceToken: Data?
    @Published private(set) var deviceTokenRevision = 0
    @Published private(set) var pendingDestination: PendingPushDestination?

    var pendingArticle: PendingPushArticle? {
        guard case let .article(article) = pendingDestination else { return nil }
        return article
    }
    var pendingArticleID: String? { pendingArticle?.articleID }

    private init() {}

    func receive(deviceToken: Data) {
        guard !deviceToken.isEmpty, deviceToken.count <= 256 else { return }
        latestDeviceToken = deviceToken
        deviceTokenRevision += 1
    }

    func receive(userInfo: [AnyHashable: Any]) {
        guard let kind = userInfo["kind"] as? String else { return }
        if kind == "timeline_updated" {
            receiveTimeline(userInfo: userInfo)
            return
        }
        guard let collection = Self.collection(for: kind),
              let articleID = userInfo["article_id"] as? String,
              !articleID.isEmpty,
              articleID.count <= 128 else { return }
        pendingDestination = .article(
            PendingPushArticle(articleID: articleID, collection: collection)
        )
    }

    func consumeDestination() -> PendingPushDestination? {
        defer { pendingDestination = nil }
        return pendingDestination
    }

    func consumeArticle() -> PendingPushArticle? {
        guard case let .article(article) = pendingDestination else { return nil }
        pendingDestination = nil
        return article
    }

    private func receiveTimeline(userInfo: [AnyHashable: Any]) {
        guard let rawEventID = userInfo["event_id"] as? String,
              let eventID = UUID(uuidString: rawEventID),
              let stableKey = userInfo["timeline_stable_key"] as? String,
              Self.isValidStableKey(stableKey),
              let rawNodeID = userInfo["timeline_node_id"] as? String,
              let nodeID = UUID(uuidString: rawNodeID),
              let rawUpdateKind = userInfo["timeline_update_kind"] as? String,
              let updateKind = TimelineUpdateKind(rawValue: rawUpdateKind),
              updateKind == .inserted || updateKind == .occurred else { return }
        pendingDestination = .timeline(
            PendingTimelinePush(
                eventID: eventID,
                stableKey: stableKey,
                nodeID: nodeID,
                updateKind: updateKind
            )
        )
    }

    private static func isValidStableKey(_ value: String) -> Bool {
        guard (1...128).contains(value.count) else { return false }
        return value.unicodeScalars.allSatisfy {
            !CharacterSet.controlCharacters.contains($0)
        }
    }

    private static func collection(for kind: String) -> DiscoveryCollection? {
        switch kind {
        case "editorial_published": return .editorial
        case "weekly_insight_published": return .weeklyInsight
        default: return nil
        }
    }
}

final class PushAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            Task { @MainActor in PushNotificationBridge.shared.receive(userInfo: userInfo) }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in PushNotificationBridge.shared.receive(deviceToken: deviceToken) }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        Task { @MainActor in PushNotificationBridge.shared.receive(userInfo: userInfo) }
        completionHandler()
    }
}
