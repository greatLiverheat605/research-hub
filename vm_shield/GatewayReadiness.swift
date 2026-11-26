import Foundation

enum GatewayLaunchMode: Equatable {
    case normal, prepareGateway, stageGatewayUpdate

    init(arguments: [String]) throws {
        let prepare = arguments.contains("--prepare-gateway")
        let stage = arguments.contains("--stage-gateway-update")
        guard !(prepare && stage) else { throw GatewayReadinessError.invalidLaunchMode }
        self = stage ? .stageGatewayUpdate : prepare ? .prepareGateway : .normal
    }

    var configuresMacOS: Bool { self == .normal }
    var isolatesWAN: Bool { self == .stageGatewayUpdate }
}

// Offline, Foundation-only validation. No subprocesses, networking, or VM APIs.
// The manifest is a complete allowlisted bundle, not a best-effort nft hash.
struct GatewayManifest {
    static let fileNames: Set<String> = [
        "residential.nft", "residential-gateway.service", "90-residential.conf",
        "dnsmasq-lan.conf", "gateway-offline-test.py", "gateway-integrity.py"
    ]
    let files: [String: String]
    let bundleSHA256: String

    init(data: Data) throws {
        let object = try GatewayJSON.object(data)
        guard GatewayJSON.integer(object["schema_version"]) == 4,
              let files = object["gateway_files"] as? [String: String],
              Set(files.keys) == Self.fileNames,
              files.values.allSatisfy({ GatewayJSON.matches($0, "^[0-9a-f]{64}$") }),
              let bundle = object["gateway_bundle_sha256"] as? String,
              GatewayJSON.matches(bundle, "^[0-9a-f]{64}$") else {
            throw GatewayReadinessError.invalidManifest
        }
        let canonical = try JSONSerialization.data(withJSONObject: files, options: [.sortedKeys])
        guard GatewaySHA256.hex(canonical) == bundle else {
            throw GatewayReadinessError.invalidManifest
        }
        self.files = files
        bundleSHA256 = bundle
    }

    static func load(from url: URL) throws -> GatewayManifest {
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true,
              let count = values.fileSize, count <= 64 * 1024 else {
            throw GatewayReadinessError.invalidManifest
        }
        return try GatewayManifest(data: Data(contentsOf: url))
    }
}

enum GatewayReadinessError: Error, LocalizedError, Equatable {
    case invalidManifest, invalidRunID, malformedStatus, invalidStatus
    case failedStatus, changedBoot, nonMonotonicSequence, changedLog, oversizedLog
    case invalidDeployment
    case invalidLaunchMode

    var errorDescription: String? {
        switch self {
        case .invalidManifest: return "网关 manifest 缺失、损坏或未完整绑定 v4 文件包。"
        case .invalidRunID: return "本次网关启动标识无效。"
        case .malformedStatus: return "网关状态 JSON 损坏，停止就绪判定。"
        case .invalidStatus: return "网关状态字段或版本与本次启动要求不符。"
        case .failedStatus: return "网关报告校验失败。"
        case .changedBoot: return "同次启动出现不同网关 boot_id。"
        case .nonMonotonicSequence: return "网关状态序号重复或倒退。"
        case .changedLog: return "本次网关控制台日志被截断或替换。"
        case .oversizedLog: return "网关控制台日志超过安全读取上限。"
        case .invalidDeployment: return "本次网关迁移回执缺失必要字段或迁移未保持关闭。"
        case .invalidLaunchMode: return "网关准备模式与迁移模式不能同时选择。"
        }
    }
}

struct GatewayDeploymentStatus: Equatable {
    let runID: String
    let bundleSHA256: String
}

enum GatewayMigrationStopState { case running, stoppedNormally, stoppedWithError }

enum GatewayMigrationCompletion {
    static func isComplete(receipt: GatewayDeploymentStatus?, stop: GatewayMigrationStopState) throws -> Bool {
        switch stop {
        case .running: return false
        case .stoppedWithError: throw GatewayReadinessError.failedStatus
        case .stoppedNormally:
            guard receipt != nil else { throw GatewayReadinessError.invalidDeployment }
            return true
        }
    }
}

// A staging boot can install a new bundle but cannot activate it against an old
// read-only challenge. This receipt establishes installation with forwarding off,
// never readiness. The coordinator separately requires a normal gateway shutdown.
struct GatewayDeploymentEvidence {
    static let marker = "CS_GATEWAY_DEPLOYMENT_JSON:"
    private let manifest: GatewayManifest
    private let runID: String
    private var previousLog = Data()

    init(manifest: GatewayManifest, runID: String) throws {
        guard GatewayJSON.matches(runID, "^[0-9a-f]{32}$") else {
            throw GatewayReadinessError.invalidRunID
        }
        self.manifest = manifest
        self.runID = runID
    }

    mutating func evaluate(log: Data) throws -> GatewayDeploymentStatus? {
        guard log.count <= GatewayReadiness.maximumLogBytes else { throw GatewayReadinessError.oversizedLog }
        guard log.starts(with: previousLog) else { throw GatewayReadinessError.changedLog }
        previousLog = log
        guard let text = String(data: log, encoding: .utf8) else {
            throw GatewayReadinessError.malformedStatus
        }
        if text.contains("CS_GATEWAY_DEPLOYMENT_FAIL") { throw GatewayReadinessError.failedStatus }
        var result: GatewayDeploymentStatus?
        // Serial consoles emit CRLF. Swift's Character treats CRLF as one
        // grapheme, so split on the actual LF byte rather than Character "\n".
        let lines = log.split(separator: 0x0a, omittingEmptySubsequences: false)
        for (index, bytes) in lines.enumerated() {
            let line = String(decoding: bytes, as: UTF8.self)
            guard let range = line.range(of: Self.marker) else { continue }
            if index == lines.count - 1 && log.last != 0x0a { return nil }
            let payload = String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
            let record: [String: Any]
            do { record = try GatewayJSON.object(Data(payload.utf8)) }
            catch { throw GatewayReadinessError.malformedStatus }
            guard let nonce = record["run_id"] as? String,
                  GatewayJSON.matches(nonce, "^[0-9a-f]{32}$") else {
                throw GatewayReadinessError.invalidDeployment
            }
            guard nonce == runID else { continue }
            let fields: Set<String> = ["schema_version", "run_id", "gateway_bundle_sha256", "state", "forward", "offline_tests"]
            guard result == nil, Set(record.keys) == fields,
                  GatewayJSON.integer(record["schema_version"]) == 1,
                  record["gateway_bundle_sha256"] as? String == manifest.bundleSHA256,
                  record["state"] as? String == "installed_forwarding_off_pending_cold_boot",
                  record["offline_tests"] as? String == "PASS",
                  GatewayJSON.integer(record["forward"]) == 0 else {
                throw GatewayReadinessError.invalidDeployment
            }
            result = GatewayDeploymentStatus(runID: runID, bundleSHA256: manifest.bundleSHA256)
        }
        return result
    }
}

struct GatewayReadyStatus: Equatable {
    let bootID: String
    let sequence: Int
    let wan: String
}

struct GatewayReadiness {
    static let marker = "CS_GATEWAY_STATUS_JSON:"
    static let maximumLogBytes = 16 * 1024 * 1024
    private let manifest: GatewayManifest
    let runID: String
    private var previousLog = Data()

    init(manifest: GatewayManifest, runID: String) throws {
        guard GatewayJSON.matches(runID, "^[0-9a-f]{32}$") else {
            throw GatewayReadinessError.invalidRunID
        }
        self.manifest = manifest
        self.runID = runID
    }

    // Re-reading the complete append-only console detects truncation/replacement.
    // Every current-run record is checked, including records after an earlier PASS.
    // An unfinished status line suppresses readiness until it is completely read.
    mutating func evaluate(log: Data) throws -> GatewayReadyStatus? {
        guard log.count <= Self.maximumLogBytes else { throw GatewayReadinessError.oversizedLog }
        guard log.starts(with: previousLog) else { throw GatewayReadinessError.changedLog }
        previousLog = log
        guard let text = String(data: log, encoding: .utf8) else {
            throw GatewayReadinessError.malformedStatus
        }
        if text.contains("CS_GATEWAY_INTEGRITY_FAIL") || text.contains("CS_GATEWAY_OFFLINE_TESTS_FAIL") {
            throw GatewayReadinessError.failedStatus
        }
        var latest: GatewayReadyStatus?
        let lines = log.split(separator: 0x0a, omittingEmptySubsequences: false)
        for (index, bytes) in lines.enumerated() {
            let line = String(decoding: bytes, as: UTF8.self)
            guard let range = line.range(of: Self.marker) else { continue }
            if index == lines.count - 1 && log.last != 0x0a { return nil }
            let payload = String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
            let record: [String: Any]
            do { record = try GatewayJSON.object(Data(payload.utf8)) }
            catch { throw GatewayReadinessError.malformedStatus }
            guard let nonce = record["run_id"] as? String,
                  GatewayJSON.matches(nonce, "^[0-9a-f]{32}$") else {
                throw GatewayReadinessError.invalidStatus
            }
            // Well-formed stale records cannot satisfy the current challenge.
            guard nonce == runID else { continue }
            if record["verdict"] as? String == "fail" { throw GatewayReadinessError.failedStatus }
            let expectedFields: Set<String> = ["schema_version", "run_id", "boot_id",
                "gateway_bundle_sha256", "files", "verdict", "forward", "ipv6_forward",
                "lan", "wan", "tests", "rules", "sequence"]
            guard Set(record.keys) == expectedFields,
                  GatewayJSON.integer(record["schema_version"]) == 3,
                  let bootID = record["boot_id"] as? String,
                  GatewayJSON.matches(bootID, "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
                  UUID(uuidString: bootID) != nil,
                  record["gateway_bundle_sha256"] as? String == manifest.bundleSHA256,
                  record["files"] as? [String: String] == manifest.files,
                  record["verdict"] as? String == "ready",
                  GatewayJSON.integer(record["forward"]) == 1,
                  GatewayJSON.integer(record["ipv6_forward"]) == 0,
                  record["lan"] as? String == "10.77.0.1/24",
                  let wan = record["wan"] as? String, GatewayJSON.validWAN(wan),
                  record["tests"] as? String == "PASS", record["rules"] as? String == "PASS",
                  let sequence = GatewayJSON.integer(record["sequence"]), sequence > 0 else {
                throw GatewayReadinessError.invalidStatus
            }
            if let latest {
                guard latest.bootID == bootID else { throw GatewayReadinessError.changedBoot }
                guard sequence > latest.sequence else { throw GatewayReadinessError.nonMonotonicSequence }
            }
            latest = GatewayReadyStatus(bootID: bootID, sequence: sequence, wan: wan)
        }
        return latest
    }
}

private enum GatewayJSON {
    static func object(_ data: Data) throws -> [String: Any] {
        guard data.count <= GatewayReadiness.maximumLogBytes,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              uniqueObjectKeys(data) else {
            throw GatewayReadinessError.malformedStatus
        }
        return object
    }

    // Foundation accepts duplicate JSON members. Reject them rather than silently
    // choosing one of two verdicts/hashes. The JSON syntax has already been parsed.
    private static func uniqueObjectKeys(_ data: Data) -> Bool {
        let bytes = Array(data)
        var stack = [Set<String>?]()
        var index = 0
        while index < bytes.count {
            switch bytes[index] {
            case 123: stack.append(Set<String>())
            case 91: stack.append(nil)
            case 125, 93: _ = stack.popLast()
            case 34:
                let start = index
                index += 1
                while index < bytes.count {
                    if bytes[index] == 92 { index += 2; continue }
                    if bytes[index] == 34 { break }
                    index += 1
                }
                guard index < bytes.count else { return false }
                var next = index + 1
                while next < bytes.count && [9, 10, 13, 32].contains(bytes[next]) { next += 1 }
                if next < bytes.count && bytes[next] == 58 {
                    guard !stack.isEmpty, var keys = stack.removeLast(),
                          let key = try? JSONSerialization.jsonObject(with: Data(bytes[start...index]),
                                                                     options: [.fragmentsAllowed]) as? String,
                          keys.insert(key).inserted else { return false }
                    stack.append(keys)
                }
            default: break
            }
            index += 1
        }
        return stack.isEmpty
    }

    static func integer(_ value: Any?) -> Int? {
        guard let value = value as? NSNumber,
              ["s", "i", "l", "q", "S", "I", "L", "Q"].contains(String(cString: value.objCType)) else {
            return nil
        }
        return Int(value.stringValue)
    }

    static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression)?.lowerBound == text.startIndex &&
            text.range(of: pattern, options: .regularExpression)?.upperBound == text.endIndex
    }

    static func validWAN(_ text: String) -> Bool {
        let pieces = text.split(separator: "/", omittingEmptySubsequences: false)
        guard pieces.count == 2, let prefix = Int(pieces[1]), (1...32).contains(prefix),
              String(prefix) == pieces[1] else { return false }
        let parts = pieces[0].split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        var bytes = [Int]()
        for part in parts {
            guard let byte = Int(part), (0...255).contains(byte), String(byte) == part else { return false }
            bytes.append(byte)
        }
        // WAN may be RFC1918 under VZ NAT, but never loopback, multicast,
        // unspecified, link-local, the isolated guest LAN, or Mihomo fake-IP.
        return bytes[0] > 0 && bytes[0] < 224 && bytes[0] != 127 &&
            !(bytes[0] == 169 && bytes[1] == 254) &&
            !(bytes[0] == 198 && (bytes[1] == 18 || bytes[1] == 19)) &&
            !(bytes[0] == 10 && bytes[1] == 77 && bytes[2] == 0)
    }
}

// Small SHA-256 implementation keeps the readiness validator Foundation-only.
// It hashes the manifest's canonical compact JSON; it never reads arbitrary files.
enum GatewaySHA256 {
    static func hex(_ data: Data) -> String {
        let constants: [UInt32] = [
            0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
            0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
            0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
            0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
            0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
            0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
            0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
            0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
        var bytes = Array(data)
        let bitLength = UInt64(bytes.count) * 8
        bytes.append(0x80)
        while bytes.count % 64 != 56 { bytes.append(0) }
        bytes.append(contentsOf: (0..<8).reversed().map { UInt8(truncatingIfNeeded: bitLength >> ($0 * 8)) })
        var hash: [UInt32] = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
        func rotate(_ x: UInt32, _ n: UInt32) -> UInt32 { (x >> n) | (x << (32 - n)) }
        for base in stride(from: 0, to: bytes.count, by: 64) {
            var words = [UInt32](repeating: 0, count: 64)
            for i in 0..<16 {
                let j = base + i * 4
                words[i] = UInt32(bytes[j]) << 24 | UInt32(bytes[j + 1]) << 16 |
                    UInt32(bytes[j + 2]) << 8 | UInt32(bytes[j + 3])
            }
            for i in 16..<64 {
                let x = words[i - 15], y = words[i - 2]
                let s0 = rotate(x, 7) ^ rotate(x, 18) ^ (x >> 3)
                let s1 = rotate(y, 17) ^ rotate(y, 19) ^ (y >> 10)
                words[i] = words[i - 16] &+ s0 &+ words[i - 7] &+ s1
            }
            var a = hash[0], b = hash[1], c = hash[2], d = hash[3]
            var e = hash[4], f = hash[5], g = hash[6], h = hash[7]
            for i in 0..<64 {
                let s1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)
                let choice = (e & f) ^ (~e & g)
                let first = h &+ s1 &+ choice &+ constants[i] &+ words[i]
                let s0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)
                let majority = (a & b) ^ (a & c) ^ (b & c)
                h = g; g = f; f = e; e = d &+ first
                d = c; c = b; b = a; a = first &+ s0 &+ majority
            }
            for (i, word) in [a,b,c,d,e,f,g,h].enumerated() { hash[i] = hash[i] &+ word }
        }
        return hash.map { String(format: "%08x", $0) }.joined()
    }
}

// The gateway's serial console is guest-controlled output. Instead of handing
// the VM the log file itself, the VM writes into a pipe and only the first
// `limit` bytes reach disk; the rest is read and discarded so the guest never
// blocks and can never fill the host disk.
final class CappedConsoleLog: @unchecked Sendable {
    let limit: Int
    private let pipe = Pipe()
    private let file: FileHandle
    private var written = 0
    private var failed = false
    private let finished = DispatchSemaphore(value: 0)

    var guestWriter: FileHandle { pipe.fileHandleForWriting }

    init(file: FileHandle, limit: Int = GatewayReadiness.maximumLogBytes) {
        self.file = file
        self.limit = limit
        pipe.fileHandleForReading.readabilityHandler = { [self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                finished.signal()
                return
            }
            let room = limit - written
            guard room > 0, !failed else { return }
            do {
                try file.write(contentsOf: data.prefix(room))
                written += min(room, data.count)
            } catch {
                failed = true
            }
        }
    }

    // Test hook: wait until the writer side is closed and all input is drained.
    func waitForEndOfInput(timeout: TimeInterval) -> Bool {
        finished.wait(timeout: .now() + timeout) == .success
    }
}
