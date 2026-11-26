import Foundation
import Darwin

// The guest can read everything under the read-only virtiofs share, and the
// host-side share service resolves the guest's lookups. Refuse to expose the
// share unless it holds only plain directories and regular files owned by this
// user: no symlinks (which could point the guest at other host paths), no
// hard-linked files, devices, sockets or FIFOs.
enum GuestShareAudit {
    static let maximumEntries = 10_000

    static func verify(_ root: URL) throws {
        let owner = getuid()
        var pending = [root.path]
        var seen = 0
        while let path = pending.popLast() {
            var info = stat()
            guard lstat(path, &info) == 0 else { throw failure("无法检查共享目录：\(path)") }
            guard info.st_uid == owner else { throw failure("共享目录含非本用户文件：\(path)") }
            switch info.st_mode & S_IFMT {
            case S_IFDIR:
                let names = try FileManager.default.contentsOfDirectory(atPath: path)
                pending.append(contentsOf: names.map { (path as NSString).appendingPathComponent($0) })
            case S_IFREG:
                guard info.st_nlink == 1 else { throw failure("共享目录含硬链接文件：\(path)") }
            case S_IFLNK:
                throw failure("共享目录含符号链接：\(path)")
            default:
                throw failure("共享目录含特殊文件：\(path)")
            }
            seen += 1
            guard seen <= maximumEntries else { throw failure("共享目录文件过多，请精简后再启动。") }
        }
    }

    private static func failure(_ message: String) -> NSError {
        NSError(domain: "ClaudeShieldShare", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
