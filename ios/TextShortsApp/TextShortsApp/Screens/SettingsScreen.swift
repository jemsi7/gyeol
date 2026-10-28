import SwiftUI

private enum SettingsDestination {
    case overview
    case notifications
    case reading
    case timeline
    case timelineSubscriptions
    case timelineHistory
}

struct SettingsScreen: View {
    @EnvironmentObject private var store: PrototypeStore
    @State private var showDeleteConfirmation = false
    @State private var destination: SettingsDestination = .overview
    let mode: LayoutMode
    let palette: AppPalette

    var body: some View {
        VStack(spacing: 0) {
            HubHeader(
                kicker: headerKicker,
                title: headerTitle,
                detail: headerDetail,
                titleID: .ux059,
                showAnnotations: store.showAnnotations,
                palette: palette
            )
            ScrollView {
                Group {
                    switch destination {
                    case .overview:
                        overviewContent
                    case .notifications:
                        notificationContent
                    case .reading:
                        readingContent
                    case .timeline:
                        timelineContent
                    case .timelineSubscriptions:
                        timelineSubscriptionsContent
                    case .timelineHistory:
                        timelineHistoryContent
                    }
                }
                .frame(maxWidth: 840)
                .padding(.horizontal, mode == .compact ? 24 : 40)
                .padding(.top, 12)
                .padding(.bottom, 24)
                .frame(maxWidth: .infinity)
            }
        }
        .background(palette.paper)
        .overlay(alignment: .bottom) {
            if let message = store.toastMessage {
                ToastView(message: message, palette: palette)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 14)
            }
        }
        .screenTagged(.scr05, show: store.showAnnotations)
    }

    private var overviewContent: some View {
        VStack(spacing: 0) {
            accountBlock
            notificationSettingsLink
            readingSettingsLink
            timelineSettingsLink
        }
    }

    private var timelineContent: some View {
        VStack(spacing: 0) {
            timelineBackButton
            timelineRefreshNotificationSwitch
            timelineAdditionNotificationSwitch
            timelineManagementLink(
                title: "구독 취소",
                subtitle: "현재 구독 중인 타임라인을 확인하고 해제합니다",
                symbol: "bell.slash",
                destination: .timelineSubscriptions,
                id: .ux136
            )
            timelineManagementLink(
                title: "구독 이력 관리",
                subtitle: "구독과 해제 기록을 보고 다시 구독합니다",
                symbol: "clock.arrow.circlepath",
                destination: .timelineHistory,
                id: .ux137
            )
        }
    }

    private var timelineSubscriptionsContent: some View {
        VStack(spacing: 0) {
            timelineChildBackButton
            timelineManagementState(history: false)
        }
    }

    private var timelineHistoryContent: some View {
        VStack(spacing: 0) {
            timelineChildBackButton
            timelineManagementState(history: true)
        }
    }

    private var notificationContent: some View {
        VStack(spacing: 0) {
            notificationBackButton
            editorialNotificationSwitch
            weeklyInsightNotificationSwitch
            if store.isPushRegistrationRepairNeeded {
                pushRegistrationRepairRow
            }
        }
    }

    private var readingContent: some View {
        VStack(spacing: 0) {
            readingBackButton
            if mode == .wide {
                HStack(alignment: .top, spacing: 40) { typeSizeBlock; leadingBlock }
            } else {
                typeSizeBlock
                leadingBlock
            }
            themeBlock
            motionRow
            mediaSaverRow
            offlineRow
            Text(store.cacheStats.formatted)
                .font(.system(size: 10))
                .foregroundColor(palette.muted)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 14)
                .uxTagged(.ux065, show: store.showAnnotations)
            resetAction
                .padding(.top, 22)
        }
    }

    private var readingSettingsLink: some View {
        settingsDestinationLink(
            title: "읽기 설정",
            subtitle: "글자 크기, 행간, 화면과 오프라인 읽기",
            symbol: "textformat.size",
            accessibilityHint: "글자 크기와 읽기 환경을 조절합니다",
            id: .ux121,
            destination: .reading
        )
    }

    private var notificationSettingsLink: some View {
        settingsDestinationLink(
            title: "알림 설정",
            subtitle: "새 Editorial 발행 소식을 관리합니다",
            symbol: "bell",
            accessibilityHint: "새 Editorial 알림을 켜거나 끕니다",
            id: .ux125,
            destination: .notifications
        )
    }

    private var timelineSettingsLink: some View {
        settingsDestinationLink(
            title: "타임라인 설정",
            subtitle: "구독, 구독 이력과 사건 알림을 관리합니다",
            symbol: "point.3.connected.trianglepath.dotted",
            accessibilityHint: "타임라인 구독과 사건 알림을 관리합니다",
            id: .ux133,
            destination: .timeline
        )
    }

    private func settingsDestinationLink(
        title: String,
        subtitle: String,
        symbol: String,
        accessibilityHint: String,
        id: UXID,
        destination nextDestination: SettingsDestination
    ) -> some View {
        Button {
            showDeleteConfirmation = false
            destination = nextDestination
        } label: {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 20, weight: .medium))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.system(size: 15, weight: .bold))
                        .foregroundColor(palette.ink)
                    Text(subtitle)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                }
                Spacer(minLength: 12)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(palette.muted)
            }
            .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint(accessibilityHint)
        .uxTagged(id, show: store.showAnnotations)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    private var readingBackButton: some View {
        settingsBackButton(id: .ux122)
    }

    private var notificationBackButton: some View {
        settingsBackButton(id: .ux126)
    }

    private var timelineBackButton: some View {
        settingsBackButton(id: .ux134)
    }

    private var timelineChildBackButton: some View {
        Button { destination = .timeline } label: {
            HStack(spacing: 8) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                Text("타임라인 설정")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
            }
            .foregroundColor(palette.accentDeep)
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("타임라인 설정으로 돌아가기")
        .uxTagged(.ux138, show: store.showAnnotations)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    private func settingsBackButton(id: UXID) -> some View {
        Button {
            destination = .overview
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                Text("설정")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
            }
            .foregroundColor(palette.accentDeep)
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("설정으로 돌아가기")
        .uxTagged(id, show: store.showAnnotations)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    private var accountBlock: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                Text("계정")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(palette.ink)
                    .uxTagged(.ux107, show: store.showAnnotations)
                Spacer()
                Text("선택 사항")
                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                    .tracking(0.8)
                    .foregroundColor(palette.muted)
            }
            accountStateContent
            if let message = accountMessage {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "info.circle")
                        .foregroundColor(palette.warm)
                    Text(message)
                        .font(.system(size: 11))
                        .foregroundColor(palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button(action: store.clearAccountFeedback) {
                        Image(systemName: "xmark")
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("계정 안내 닫기")
                }
                .uxTagged(.ux112, show: store.showAnnotations)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 18)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }

    @ViewBuilder
    private var accountStateContent: some View {
        switch store.accountStatus {
        case .signedOut:
            signedOutAccount
        case .restoring:
            accountLoading("계정 확인 중")
        case .signingIn:
            accountLoading("Apple 로그인 중")
        case .deleting:
            accountLoading("계정 삭제 중")
        case let .signedIn(profile):
            signedInAccount(profile)
        case let .error(_, cachedProfile):
            if let cachedProfile {
                signedInAccount(cachedProfile)
            } else {
                signedOutAccount
            }
        }
    }

    private var signedOutAccount: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Apple로 로그인")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundColor(palette.ink)
                Text("로그인하지 않아도 읽을 수 있습니다. 계정은 이 기기의 읽기 설정과 기록을 지우지 않습니다.")
                    .font(.system(size: 11))
                    .lineSpacing(3)
                    .foregroundColor(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            AppleAccountSignInButton(
                uxID: .ux108,
                showAnnotations: store.showAnnotations,
                isDisabled: false,
                onPayload: { payload in
                    Task { await store.completeAppleSignIn(payload: payload) }
                },
                onError: store.handleAppleSignInError,
                onCancel: store.cancelAppleSignIn
            )
            .frame(maxWidth: mode == .wide ? 420 : .infinity)
        }
    }

    private func signedInAccount(_ profile: AccountProfile) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: "person.crop.circle.fill")
                    .font(.system(size: 30))
                    .foregroundColor(palette.accentDeep)
                VStack(alignment: .leading, spacing: 3) {
                    Text(profile.displayName ?? "Apple 계정")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundColor(palette.ink)
                    if let email = profile.email {
                        Text(email)
                            .font(.system(size: 10))
                            .foregroundColor(palette.muted)
                            .lineLimit(2)
                            .textSelection(.enabled)
                    }
                }
                Spacer(minLength: 0)
            }

            HStack(spacing: 12) {
                Button {
                    Task { await store.signOutAccount() }
                } label: {
                    HStack {
                        Text("로그아웃")
                        Spacer()
                        Image(systemName: "rectangle.portrait.and.arrow.right")
                    }
                }
                .buttonStyle(QuietPillButtonStyle(palette: palette))
                .uxTagged(.ux109, show: store.showAnnotations)

                Button {
                    showDeleteConfirmation.toggle()
                } label: {
                    HStack {
                        Text("계정 삭제")
                        Spacer()
                        Image(systemName: showDeleteConfirmation ? "chevron.up" : "chevron.down")
                    }
                }
                .buttonStyle(QuietPillButtonStyle(palette: palette))
                .uxTagged(.ux110, show: store.showAnnotations)
            }

            if showDeleteConfirmation {
                VStack(alignment: .leading, spacing: 12) {
                    Rectangle().fill(Color.red).frame(height: 2)
                    Text("Apple 연결 해제를 시도한 뒤 서버의 계정·로그인 세션·계정 보관함·읽기 진행·읽기 설정과 이 기기의 해당 계정 캐시를 삭제합니다. 로그인하지 않고 만든 비회원 기록은 이 기기에 남습니다.")
                        .font(.system(size: 11))
                        .lineSpacing(3)
                        .foregroundColor(palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 12) {
                        Button("취소") { showDeleteConfirmation = false }
                            .buttonStyle(QuietPillButtonStyle(palette: palette))
                        Button("계정 영구 삭제") {
                            Task {
                                await store.deleteAccount()
                                if store.accountProfile == nil { showDeleteConfirmation = false }
                            }
                        }
                        .buttonStyle(DestructiveAccountButtonStyle())
                        .uxTagged(.ux111, show: store.showAnnotations)
                    }
                }
                .accessibilityElement(children: .contain)
            }
        }
    }

    private var editorialNotificationSwitch: some View {
        notificationSwitch(
            title: "새 Editorial 알림",
            detail: notificationDetail(
                isEnabled: store.isEditorialNotificationSwitchOn,
                disabledDescription: "처음 발간된 Editorial만 알려드립니다."
            ),
            accessibilityHint: "새 Editorial이 처음 발간되면 이 기기로 알려드립니다",
            isOn: editorialNotificationSwitchBinding,
            isChangeAllowed: store.canChangeEditorialNotificationSwitch,
            uxID: .ux123
        )
    }

    private var weeklyInsightNotificationSwitch: some View {
        notificationSwitch(
            title: "새 주간인사이트 알림",
            detail: notificationDetail(
                isEnabled: store.isWeeklyInsightNotificationSwitchOn,
                disabledDescription: "새 주간인사이트가 발간되면 알려드립니다."
            ),
            accessibilityHint: "새 주간인사이트가 발간되면 이 기기로 알려드립니다",
            isOn: weeklyInsightNotificationSwitchBinding,
            isChangeAllowed: store.canChangeWeeklyInsightNotificationSwitch,
            uxID: .ux131
        )
    }

    private var timelineRefreshNotificationSwitch: some View {
        notificationSwitch(
            title: "사건 갱신 알림",
            detail: notificationDetail(
                isEnabled: store.isTimelineRefreshNotificationSwitchOn,
                disabledDescription: "기존 흐름 사이에 중요한 사건이 갱신되면 알려드립니다."
            ),
            accessibilityHint: "타임라인 사건 갱신 알림을 켜거나 끕니다",
            isOn: Binding(
                get: { store.isTimelineRefreshNotificationSwitchOn },
                set: { value in
                    Task { await store.setTimelineRefreshNotificationsEnabled(value) }
                }
            ),
            isChangeAllowed: store.canChangeTimelineNotificationSwitch,
            uxID: .ux135
        )
    }

    private var timelineAdditionNotificationSwitch: some View {
        notificationSwitch(
            title: "사건 추가 알림",
            detail: notificationDetail(
                isEnabled: store.isTimelineAdditionNotificationSwitchOn,
                disabledDescription: "타임라인에 새로운 최신 사건이 추가되면 알려드립니다."
            ),
            accessibilityHint: "타임라인 사건 추가 알림을 켜거나 끕니다",
            isOn: Binding(
                get: { store.isTimelineAdditionNotificationSwitchOn },
                set: { value in
                    Task { await store.setTimelineAdditionNotificationsEnabled(value) }
                }
            ),
            isChangeAllowed: store.canChangeTimelineNotificationSwitch,
            uxID: .ux139
        )
    }

    private func timelineManagementLink(
        title: String,
        subtitle: String,
        symbol: String,
        destination nextDestination: SettingsDestination,
        id: UXID
    ) -> some View {
        Button {
            destination = nextDestination
            Task { await store.refreshTimelineManagement() }
        } label: {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 19, weight: .medium))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(palette.ink)
                    Text(subtitle)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                }
                Spacer(minLength: 12)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(palette.muted)
            }
            .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .uxTagged(id, show: store.showAnnotations)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    @ViewBuilder
    private func timelineManagementState(history: Bool) -> some View {
        if !store.isSignedIn {
            managementMessage(
                title: "로그인이 필요합니다",
                detail: "설정 첫 화면에서 Apple로 로그인하면 계정의 타임라인 기록을 관리할 수 있습니다."
            )
        } else if store.isTimelineManagementLoading {
            HStack(spacing: 12) {
                ProgressView().tint(palette.accentDeep)
                Text("타임라인 기록을 불러오는 중")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(palette.ink)
            }
            .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
        } else if let error = store.timelineManagementError {
            VStack(alignment: .leading, spacing: 12) {
                managementMessage(title: "불러오지 못했어요", detail: error)
                Button("다시 시도") { Task { await store.refreshTimelineManagement() } }
                    .buttonStyle(QuietPillButtonStyle(palette: palette))
            }
            .padding(.vertical, 16)
        } else if history {
            if store.timelineSubscriptionHistory.isEmpty {
                managementMessage(
                    title: "아직 구독 이력이 없습니다",
                    detail: "둘러보기나 검색에서 타임라인을 구독하면 여기에 기록됩니다."
                )
            } else {
                ForEach(store.timelineSubscriptionHistory) { event in
                    timelineHistoryRow(event)
                }
            }
        } else if store.timelineSubscriptionDetails.isEmpty {
            managementMessage(
                title: "구독 중인 타임라인이 없습니다",
                detail: "둘러보기나 검색에서 관심 타임라인을 구독할 수 있습니다."
            )
        } else {
            ForEach(store.timelineSubscriptionDetails) { detail in
                timelineSubscriptionRow(detail)
            }
        }
    }

    private func timelineSubscriptionRow(_ detail: TimelineSubscriptionDetail) -> some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text(detail.title ?? detail.stableKey)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(palette.ink)
                if let summary = detail.summary {
                    Text(summary)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .lineLimit(2)
                }
                Text("구독 시작 \(Self.managementDate(detail.subscribedAt))")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(palette.muted)
            }
            Spacer(minLength: 8)
            HStack(spacing: 14) {
                Button("보기") {
                    Task { await store.openSubscribedTimeline(detail.stableKey) }
                }
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.ink)
                .frame(minHeight: 44)
                .disabled(store.pendingTimelineOpenKey != nil)
                .accessibilityLabel("\(detail.title ?? detail.stableKey) 타임라인 보기")
                .accessibilityIdentifier("TIMELINE-SUBSCRIPTION-OPEN")
                Button("구독 취소") {
                    Task {
                        await store.toggleTimelineSubscription(detail.stableKey)
                        await store.refreshTimelineManagement()
                    }
                }
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minHeight: 44)
                .disabled(store.pendingTimelineSubscriptionKeys.contains(detail.stableKey))
            }
        }
        .frame(maxWidth: .infinity, minHeight: 88, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    private func timelineHistoryRow(_ event: TimelineSubscriptionHistoryEvent) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Text(event.title ?? event.stableKey)
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(palette.ink)
                Text("\(event.eventType == .subscribe ? "구독" : "구독 해제") · \(Self.managementDate(event.occurredAt))")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(palette.muted)
            }
            Spacer(minLength: 8)
            if !event.isCurrentlySubscribed {
                Button("다시 구독") {
                    Task {
                        await store.toggleTimelineSubscription(event.stableKey)
                        await store.refreshTimelineManagement()
                    }
                }
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(palette.accentDeep)
                .frame(minHeight: 44)
                .disabled(store.pendingTimelineSubscriptionKeys.contains(event.stableKey))
            } else {
                Text("구독 중")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(palette.accentDeep)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 72, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    private func managementMessage(title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(.system(size: 18, weight: .bold))
                .foregroundColor(palette.ink)
            Text(detail)
                .font(.system(size: 11))
                .foregroundColor(palette.muted)
                .lineSpacing(3)
        }
        .frame(maxWidth: .infinity, minHeight: 150, alignment: .topLeading)
        .padding(.top, 24)
    }

    private static func managementDate(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ko_KR")
        formatter.dateFormat = "yyyy.MM.dd HH:mm"
        return formatter.string(from: date)
    }

    private var pushRegistrationRepairRow: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 20, weight: .medium))
                .foregroundColor(palette.accentDeep)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                Text("이 기기는 아직 알림을 받지 않아요")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(palette.ink)
                Text(store.pushRegistrationRepairDescription)
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            Button("다시 시도") {
                Task { await store.repairPushRegistration() }
            }
            .font(.system(size: 12, weight: .semibold))
            .foregroundColor(palette.accentDeep)
            .buttonStyle(.plain)
            .disabled(!store.canChangeEditorialNotificationSwitch)
        }
        .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .accessibilityElement(children: .contain)
        .accessibilityHint("이 기기를 알림 대상으로 다시 등록합니다")
        .uxTagged(.ux132, show: store.showAnnotations)
    }

    private func notificationSwitch(
        title: String,
        detail: String,
        accessibilityHint: String,
        isOn: Binding<Bool>,
        isChangeAllowed: Bool,
        uxID: UXID
    ) -> some View {
        Toggle(isOn: isOn) {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: notificationSymbol)
                    .font(.system(size: 20, weight: .medium))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.system(size: 12, weight: .bold))
                        .foregroundColor(palette.ink)
                    Text(detail)
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .toggleStyle(SwissSwitchToggleStyle(palette: palette))
        .disabled(!isChangeAllowed)
        .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .accessibilityHint(accessibilityHint)
        .uxTagged(uxID, show: store.showAnnotations)
    }

    private var editorialNotificationSwitchBinding: Binding<Bool> {
        Binding(
            get: { store.isEditorialNotificationSwitchOn },
            set: { isEnabled in
                Task { await store.setEditorialNotificationsEnabled(isEnabled) }
            }
        )
    }

    private var weeklyInsightNotificationSwitchBinding: Binding<Bool> {
        Binding(
            get: { store.isWeeklyInsightNotificationSwitchOn },
            set: { isEnabled in
                Task { await store.setWeeklyInsightNotificationsEnabled(isEnabled) }
            }
        )
    }

    private var notificationSymbol: String {
        switch store.editorialPushState {
        case .enabled: return "bell.badge.fill"
        case .denied, .failed(_): return "bell.slash"
        default: return "bell"
        }
    }

    private func notificationDetail(
        isEnabled: Bool,
        disabledDescription: String
    ) -> String {
        switch store.editorialPushState {
        case .signedOut: return "로그인 후 사용할 수 있습니다."
        case .disabled: return disabledDescription
        case .requesting: return "iOS 알림 권한을 확인하고 있습니다."
        case .registering: return "이 기기를 안전하게 등록하고 있습니다."
        case .enabled: return isEnabled ? "이 기기에서 알림을 받습니다." : disabledDescription
        case .denied: return "iOS 설정에서 알림 권한을 허용해 주세요."
        case .failed(_): return "등록을 완료하지 못했습니다. 다시 시도해 주세요."
        }
    }

    private var headerKicker: String {
        switch destination {
        case .overview: return "SETTINGS"
        case .notifications: return "NOTIFICATIONS"
        case .reading: return "READING"
        case .timeline, .timelineSubscriptions, .timelineHistory: return "TIMELINE"
        }
    }

    private var headerTitle: String {
        switch destination {
        case .overview: return "설정"
        case .notifications: return "알림 설정"
        case .reading: return "읽기 설정"
        case .timeline: return "타임라인 설정"
        case .timelineSubscriptions: return "구독 취소"
        case .timelineHistory: return "구독 이력 관리"
        }
    }

    private var headerDetail: String {
        switch destination {
        case .overview: return "계정과 앱 환경을 관리합니다"
        case .notifications:
            return mode == .compact ? "" : "발행 유형별 새 소식을 관리합니다"
        case .reading:
            return mode == .compact ? "" : "변경 내용은 바로 저장됩니다"
        case .timeline:
            return mode == .compact ? "" : "구독과 사건 알림을 관리합니다"
        case .timelineSubscriptions:
            return mode == .compact ? "" : "현재 구독 중인 타임라인입니다"
        case .timelineHistory:
            return mode == .compact ? "" : "구독과 해제 기록을 최신순으로 봅니다"
        }
    }

    private func accountLoading(_ title: String) -> some View {
        HStack(spacing: 12) {
            ProgressView().tint(palette.accentDeep)
            Text(title)
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(palette.ink)
        }
        .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
    }

    private var accountMessage: String? {
        if case let .error(message, _) = store.accountStatus { return message }
        return store.accountFeedback
    }

#if DEBUG
    // Production navigation is owned by PrimaryBottomNavigation.
    private var backButton: some View {
        Button { store.go(to: store.previousScreen) } label: {
            Image(systemName: "arrow.left").frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("이전 화면")
        .uxTagged(.ux058, show: store.showAnnotations)
    }
#endif

    private var typeSizeBlock: some View {
        settingBlock(title: "글자 크기", id: .ux060) {
            SegmentedControl(
                options: ReaderTypeSize.allCases,
                label: { $0.rawValue },
                selection: Binding(get: { store.settings.typeSize }, set: store.setTypeSize),
                palette: palette
            )
        }
    }

    private var leadingBlock: some View {
        settingBlock(title: "행간", id: .ux061) {
            SegmentedControl(
                options: ReaderLeading.allCases,
                label: { $0.rawValue },
                selection: Binding(get: { store.settings.leading }, set: store.setLeading),
                palette: palette
            )
        }
    }

    private var themeBlock: some View {
        settingBlock(title: "화면", id: .ux062) {
            SegmentedControl(
                options: ReaderTheme.allCases,
                label: { $0.rawValue },
                selection: Binding(get: { store.settings.theme }, set: store.setTheme),
                palette: palette
            )
        }
    }

    private var motionRow: some View {
        Toggle(
            isOn: Binding(get: { store.settings.motionEnabled }, set: store.setMotionEnabled)
        ) {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: "wind")
                    .font(.system(size: 20))
                    .foregroundColor(palette.accentDeep)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 3) {
                    Text("장면 전환").font(.system(size: 12, weight: .bold))
                    Text("역할에 맞는 짧은 움직임을 사용합니다.")
                        .font(.system(size: 10))
                        .foregroundColor(palette.muted)
                }
            }
        }
        .toggleStyle(SwissSwitchToggleStyle(palette: palette))
        .frame(minHeight: 76)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }

    private var mediaSaverRow: some View {
        switchRow(
            title: "미디어 절약",
            subtitle: "느린 인터넷 연결 상태에서는 원하는 사진만 눌러 불러올 수 있습니다.",
            isOn: Binding(get: { store.settings.mediaSaver }, set: store.setMediaSaver)
        )
        .uxTagged(.ux063, show: store.showAnnotations)
    }

    private var offlineRow: some View {
        switchRow(
            title: "오프라인 15건",
            subtitle: "최근 기사와 사진을 이 기기에 보관합니다.",
            isOn: Binding(get: { store.settings.offlineCache }, set: store.setOfflineCache)
        )
        .uxTagged(.ux064, show: store.showAnnotations)
    }

    private var resetAction: some View {
        Button { store.resetSettings() } label: {
            HStack {
                Image(systemName: "arrow.counterclockwise")
                Text("읽기 설정 기본값으로")
                Spacer()
                Image(systemName: "arrow.right")
            }
        }
        .buttonStyle(QuietPillButtonStyle(palette: palette))
        .uxTagged(.ux066, show: store.showAnnotations)
    }

#if DEBUG
    // Kept for forced-screen regression; normal settings auto-save and have no Done step.
    private var footerActions: some View {
        HStack(spacing: 12) {
            Button { store.resetSettings() } label: {
                HStack { Image(systemName: "arrow.counterclockwise"); Text("기본값") }
            }
            .buttonStyle(QuietPillButtonStyle(palette: palette))
            .uxTagged(.ux066, show: store.showAnnotations)
            Button { store.finishSettings() } label: {
                HStack { Text("완료"); Spacer(); Image(systemName: "checkmark") }
            }
            .buttonStyle(PrimaryPillButtonStyle(palette: palette))
            .uxTagged(.ux067, show: store.showAnnotations)
        }
        .padding(.horizontal, mode == .compact ? 24 : 40)
        .padding(.vertical, 8)
        .frame(height: 70)
        .overlay(alignment: .top) { Rectangle().fill(palette.ruleStrong).frame(height: 1) }
    }
#endif

    private func settingBlock<Content: View>(title: String, id: UXID, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 12, weight: .bold))
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 15)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
        .uxTagged(id, show: store.showAnnotations)
    }

    private func switchRow(title: String, subtitle: String, isOn: Binding<Bool>) -> some View {
        Toggle(isOn: isOn) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 12, weight: .bold))
                Text(subtitle)
                    .font(.system(size: 10))
                    .foregroundColor(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .toggleStyle(SwissSwitchToggleStyle(palette: palette))
        .frame(minHeight: 76)
        .overlay(alignment: .bottom) { Rectangle().fill(palette.line).frame(height: 1) }
    }
}
