// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "DuskPanel",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "DuskCore", targets: ["DuskCore"]),
        .executable(name: "DuskPanel", targets: ["DuskPanel"]),
        .executable(name: "DuskCoreTests", targets: ["DuskCoreTests"]),
    ],
    targets: [
        .target(name: "DuskCore"),
        .executableTarget(name: "DuskPanel", dependencies: ["DuskCore"]),
        .executableTarget(
            name: "DuskCoreTests",
            dependencies: ["DuskCore"],
            path: "Tests/DuskCoreTests"
        ),
    ]
)
