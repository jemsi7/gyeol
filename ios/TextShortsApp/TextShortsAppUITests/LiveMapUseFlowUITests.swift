import XCTest

final class LiveMapUseFlowUITests: XCTestCase {
    private let sceneTransitionSettleInterval: TimeInterval = 0.35
    private let completionTransitionSettleInterval: TimeInterval = 0.65
    private let screenshotLeadInInterval: TimeInterval = 1.0
    private let screenshotStabilityInterval: TimeInterval = 0.25
    private let screenshotStabilityAttempts = 5
    private let keys = [
        "LIVE_MAP_BASE_URL",
        "LIVE_MAP_EXPECTED_ARTICLE_ID",
        "LIVE_MAP_EXPECTED_HEADLINE",
        "LIVE_MAP_EXPECTED_SCENE_COUNT",
        "LIVE_MAP_SHA256",
    ]

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testPersistedMapReadsFromFirstSceneThroughBoundary() throws {
        let launchEnvironment = try launchEnvironmentFromTestBundle()
        let expectedArticleID = launchEnvironment["LIVE_MAP_EXPECTED_ARTICLE_ID"]!
        let expectedHeadline = launchEnvironment["LIVE_MAP_EXPECTED_HEADLINE"]!
        let expectedSceneCount = try XCTUnwrap(
            Int(launchEnvironment["LIVE_MAP_EXPECTED_SCENE_COUNT"]!)
        )

        let app = XCUIApplication()
        app.launchArguments = ["-live-map-e2e"]
        app.launchEnvironment = launchEnvironment
        app.launch()
        let productWindow = app.windows.firstMatch
        XCTAssertTrue(productWindow.waitForExistence(timeout: 5))

        let homeScreen = element("SCR-10", in: app)
        XCTAssertTrue(homeScreen.waitForExistence(timeout: 10))
        let read = app.buttons["UX-096"]
        XCTAssertTrue(read.waitForExistence(timeout: 3))
        read.tap()

        let readerScreen = element("SCR-02", in: app)
        XCTAssertTrue(readerScreen.waitForExistence(timeout: 10))
        let dismissGuide = app.buttons["UX-103"]
        if dismissGuide.waitForExistence(timeout: 2) { dismissGuide.tap() }
        waitForTransitionToSettle(sceneTransitionSettleInterval)
        XCTAssertEqual(
            try sentinel("LIVE-MAP-ARTICLE-ID", in: app),
            expectedArticleID
        )
        XCTAssertEqual(
            try sentinel("LIVE-MAP-HEADLINE", in: app),
            expectedHeadline
        )
        XCTAssertFalse(app.staticTexts["BAR 그래프로 읽는 도시의 변화"].exists)

        for scene in 1...expectedSceneCount {
            XCTAssertEqual(
                try sentinel("LIVE-MAP-SCENE-N-of-M", in: app),
                "\(scene) / \(expectedSceneCount)"
            )
            if scene == 1 {
                attachStableScreenshot(productWindow, name: "live-map-first-scene")
            }
            if scene < expectedSceneCount {
                let next = app.buttons["다음 장면"]
                XCTAssertTrue(next.waitForExistence(timeout: 3))
                next.tap()
                waitForTransitionToSettle(sceneTransitionSettleInterval)
            }
        }

        attachStableScreenshot(productWindow, name: "live-map-last-scene")
        let finish = app.buttons["기사 마치기"]
        XCTAssertTrue(finish.waitForExistence(timeout: 3))
        finish.tap()
        let boundaryScreen = element("SCR-03", in: app)
        XCTAssertTrue(boundaryScreen.waitForExistence(timeout: 5))
        waitForTransitionToSettle(completionTransitionSettleInterval)
        XCTAssertTrue(app.staticTexts["기사 읽기 완료"].exists)
        attachStableScreenshot(productWindow, name: "live-map-boundary")
    }

    private func launchEnvironmentFromTestBundle() throws -> [String: String] {
        let bundle = Bundle(for: LiveMapUseFlowUITests.self)
        let encoded = try XCTUnwrap(
            bundle.object(forInfoDictionaryKey: "LIVE_MAP_E2E_CONFIG_B64") as? String,
            "missing LIVE_MAP_E2E_CONFIG_B64 test-bundle payload"
        )
        let data = try XCTUnwrap(
            Data(base64Encoded: encoded),
            "invalid LIVE_MAP_E2E_CONFIG_B64 base64"
        )
        let decoded = try JSONDecoder().decode([String: String].self, from: data)
        XCTAssertEqual(
            Set(decoded.keys),
            Set(keys),
            "live-map payload must contain exactly the expected launch keys"
        )
        guard Set(decoded.keys) == Set(keys),
              decoded.values.allSatisfy({ !$0.isEmpty }) else {
            throw LiveMapUITestConfigurationError.invalidPayload
        }

        let baseURL = try XCTUnwrap(URL(string: decoded["LIVE_MAP_BASE_URL"]!))
        XCTAssertEqual(baseURL.scheme, "http")
        XCTAssertEqual(baseURL.host, "127.0.0.1")
        XCTAssertNotNil(baseURL.port)
        guard baseURL.scheme == "http", baseURL.host == "127.0.0.1", baseURL.port != nil,
              Self.isLowercaseHex(decoded["LIVE_MAP_EXPECTED_ARTICLE_ID"]!, count: 64),
              Self.isLowercaseHex(decoded["LIVE_MAP_SHA256"]!, count: 64),
              let sceneCount = Int(decoded["LIVE_MAP_EXPECTED_SCENE_COUNT"]!),
              (3...9).contains(sceneCount) else {
            throw LiveMapUITestConfigurationError.invalidPayload
        }
        return decoded
    }

    private static func isLowercaseHex(_ value: String, count: Int) -> Bool {
        let allowed = Set("0123456789abcdef")
        return value.count == count && value.allSatisfy(allowed.contains)
    }

    private func sentinel(_ identifier: String, in app: XCUIApplication) throws -> String {
        let element = app.staticTexts[identifier]
        XCTAssertTrue(element.waitForExistence(timeout: 5), "missing \(identifier)")
        return element.label
    }

    private func element(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)[identifier]
    }

    private func waitForTransitionToSettle(_ interval: TimeInterval) {
        Thread.sleep(forTimeInterval: interval)
    }

    private func attachStableScreenshot(_ element: XCUIElement, name: String) {
        Thread.sleep(forTimeInterval: screenshotLeadInInterval)
        var previous = element.screenshot()
        for _ in 0..<screenshotStabilityAttempts {
            Thread.sleep(forTimeInterval: screenshotStabilityInterval)
            let current = element.screenshot()
            if current.pngRepresentation == previous.pngRepresentation {
                attachScreenshot(current, name: name)
                return
            }
            previous = current
        }
        XCTFail("\(name) did not reach two consecutive stable frames")
        attachScreenshot(previous, name: name)
    }

    private func attachScreenshot(_ screenshot: XCUIScreenshot, name: String) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

private enum LiveMapUITestConfigurationError: Error {
    case invalidPayload
}
