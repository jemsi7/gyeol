// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ArticleFeedKit",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(name: "ArticleFeedKit", targets: ["ArticleFeedKit"]),
        .executable(name: "ArticleFeedE2E", targets: ["ArticleFeedE2E"]),
    ],
    targets: [
        .target(
            name: "ArticleFeedKit",
            resources: [.process("Resources")]
        ),
        .executableTarget(
            name: "ArticleFeedE2E",
            dependencies: ["ArticleFeedKit"]
        ),
        .testTarget(
            name: "ArticleFeedKitTests",
            dependencies: ["ArticleFeedKit"],
            resources: [.process("Fixtures")]
        ),
    ]
)
