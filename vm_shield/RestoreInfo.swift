import Foundation
import Virtualization

VZMacOSRestoreImage.fetchLatestSupported { result in
    do {
        let image = try result.get()
        let object: [String: Any] = [
            "url": image.url.absoluteString,
            "build": image.buildVersion,
            "supported": image.mostFeaturefulSupportedConfiguration != nil
        ]
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        exit(0)
    } catch {
        fputs("Could not fetch Apple restore metadata: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}
dispatchMain()
