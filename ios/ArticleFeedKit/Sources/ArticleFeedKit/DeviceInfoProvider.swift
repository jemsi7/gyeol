import Foundation

#if os(iOS)
import UIKit
import Darwin

@MainActor
public enum DeviceInfoProvider {
    public static func current(
        defaults: UserDefaults = .standard,
        namespace: String = "ArticleFeedKit"
    ) -> DeviceInfo {
        let installationKey = "\(namespace).installationID"
        let installationID: UUID
        if let raw = defaults.string(forKey: installationKey), let stored = UUID(uuidString: raw) {
            installationID = stored
        } else {
            installationID = UUID()
            defaults.set(installationID.uuidString.lowercased(), forKey: installationKey)
        }
        let device = UIDevice.current
        let info = Bundle.main.infoDictionary ?? [:]
        return DeviceInfo(
            installationID: installationID,
            vendorID: device.identifierForVendor,
            platform: device.userInterfaceIdiom == .pad ? .ipados : .ios,
            model: hardwareModel(),
            osVersion: device.systemVersion,
            appVersion: info["CFBundleShortVersionString"] as? String ?? "0",
            buildNumber: info["CFBundleVersion"] as? String ?? "0",
            locale: Locale.current.identifier.replacingOccurrences(of: "_", with: "-")
        )
    }

    private static func hardwareModel() -> String {
        var systemInfo = utsname()
        uname(&systemInfo)
        return withUnsafePointer(to: &systemInfo.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
    }
}
#endif
