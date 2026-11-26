import AppKit
import Darwin

// Integration adapter: replace this with the app's existing LaunchResult type if needed.
enum LaunchResult: Sendable {
    case success(String)
    case failure(String)
}

/// Launch requests only. A successful result does not assert network connectivity,
/// effective browser policy, or the availability of any remote service.
@MainActor
enum AppLauncher {
    private static let proxy = "http://127.0.0.1:7894"
    private static var launchInProgress = false
    private static var children: [Process] = []

    private static var usEnv: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["TZ"] = "America/Los_Angeles"
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        // Do not accidentally start an Electron app as a Node CLI.
        env.removeValue(forKey: "ELECTRON_RUN_AS_NODE")
        return env
    }

    // HTTP/1.1 recovery is opt-in and temporary. It does not repair the proxy path.
    static func launchChrome(
        http1OnlyRecovery: Bool = false,
        completion: @escaping @MainActor (LaunchResult) -> Void
    ) {
        begin(completion: completion) {
            let binary = URL(fileURLWithPath:
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            // Preserve the original persistent profile, including its cookies.
            let profile = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".chrome-ai-clean", isDirectory: true)
            try checkExecutable(binary)
            // Refuse to reuse our still-running child, even before Chrome creates its lock.
            children.removeAll { !$0.isRunning }
            if children.contains(where: {
                $0.arguments?.contains("--user-data-dir=\(profile.path)") == true
            }) {
                throw LaunchError.message("专用 Chrome 已运行；完全退出该实例后再应用新参数。")
            }
            try prepareChromeProfile(profile)
            return try await spawn(binary, arguments: chromeArguments(
                profile: profile, http1OnlyRecovery: http1OnlyRecovery
            ))
        }
    }

    static func launchClaude(
        http1OnlyRecovery: Bool = false,
        completion: @escaping @MainActor (LaunchResult) -> Void
    ) {
        begin(completion: completion) {
            let appURL = URL(fileURLWithPath: "/Applications/Claude.app", isDirectory: true)
            guard let bundle = Bundle(url: appURL),
                  let bundleID = bundle.bundleIdentifier,
                  let binary = bundle.executableURL else {
                throw LaunchError.message("无法读取已安装 Claude 的 bundle ID 或可执行文件。")
            }
            try checkExecutable(binary)
            // This is graceful termination, with a deadline; never force-kill or clear data.
            try await terminateClaudeAndWait(bundleID: bundleID)
            return try await spawn(binary, arguments: claudeArguments(
                http1OnlyRecovery: http1OnlyRecovery
            ))
        }
    }

    static func chromeArguments(profile: URL, http1OnlyRecovery: Bool) -> [String] {
        [
            "--user-data-dir=\(profile.path)",
            "--no-first-run",
            "--no-default-browser-check",
            "--accept-lang=en-US,en"
        ] + commonArguments(http1OnlyRecovery: http1OnlyRecovery)
        // No start URL. This function must not be used as a connectivity probe.
    }

    static func claudeArguments(http1OnlyRecovery: Bool) -> [String] {
        // Electron does not implement all Chrome preferences/switches. The host app
        // can also override proxy/language settings after startup; see the audit.
        commonArguments(http1OnlyRecovery: http1OnlyRecovery)
    }

    private static func commonArguments(http1OnlyRecovery: Bool) -> [String] {
        var arguments = [
            "--proxy-server=\(proxy)",
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--lang=en-US",
            "-AppleLanguages", "(en-US)",
            "-AppleLocale", "en_US"
        ]
        if http1OnlyRecovery { arguments.append("--disable-http2") }
        // Keep Chromium's implicit localhost bypass for desktop callbacks/integrations.
        // This is HTTP(S)/WebSocket proxy configuration, not an OS network firewall.
        return arguments
    }

    static func prepareChromeProfile(_ profile: URL) throws {
        let fm = FileManager.default
        var statBuffer = stat()
        if lstat(profile.path, &statBuffer) == 0 {
            guard (statBuffer.st_mode & S_IFMT) == S_IFDIR else {
                throw LaunchError.message("专用 Chrome Profile 必须是目录，不能是符号链接。")
            }
        } else {
            guard errno == ENOENT else {
                throw LaunchError.message("无法检查专用 Chrome Profile。")
            }
            try fm.createDirectory(at: profile, withIntermediateDirectories: true,
                                   attributes: [.posixPermissions: 0o700])
        }
        // lstat deliberately detects dangling symlinks, unlike fileExists(atPath:).
        // A stale lock is an actionable error, never grounds for deleting it blindly.
        for name in ["SingletonLock", "SingletonSocket", "SingletonCookie"] {
            if lstat(profile.appendingPathComponent(name).path, &statBuffer) == 0 {
                throw LaunchError.message(
                    "专用 Chrome Profile 存在 \(name)；请先退出实例，确认无占用后再处理残留锁。"
                )
            }
            guard errno == ENOENT else {
                throw LaunchError.message("无法检查 Profile 锁：\(name)。")
            }
        }
        // Do not read/rewrite Preferences or Local State while Chromium can own them.
        // Command-line preference overrides avoid truncating or losing unrelated data.
    }

    private static func terminateClaudeAndWait(bundleID: String) async throws {
        let initial = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
        for app in initial where !app.isTerminated {
            guard app.terminate() else {
                throw LaunchError.message("Claude 未接受正常退出请求；请保存工作并手动退出。")
            }
        }
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(8))
        while true {
            let current = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            let stillRunning = (initial + current).contains { !$0.isTerminated }
            if !stillRunning { return }
            guard clock.now < deadline else {
                throw LaunchError.message("Claude 退出超时；未强制结束，也未启动重复实例。")
            }
            try await Task.sleep(for: .milliseconds(100))
        }
    }

    private static func checkExecutable(_ binary: URL) throws {
        guard FileManager.default.isExecutableFile(atPath: binary.path) else {
            throw LaunchError.message("应用可执行文件不存在或不可执行：\(binary.path)")
        }
    }

    private static func spawn(_ binary: URL, arguments: [String]) async throws -> LaunchResult {
        let process = Process()
        process.executableURL = binary
        process.arguments = arguments
        process.environment = usEnv
        process.standardInput = FileHandle.nullDevice
        // Avoid an undrained Pipe deadlocking a verbose child. Production integration
        // can use a bounded, privacy-filtered file log instead.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        children.removeAll { !$0.isRunning }
        children.append(process)
        try await Task.sleep(for: .milliseconds(500))
        guard process.isRunning else {
            throw LaunchError.message(
                "启动进程提前退出（状态 \(process.terminationStatus)）；可能发生单实例转交，不能确认新参数生效。"
            )
        }
        return .success("已创建进程 PID \(process.processIdentifier)；未进行远端连通性检测。")
    }

    private static func begin(
        completion: @escaping @MainActor (LaunchResult) -> Void,
        operation: @escaping @MainActor () async throws -> LaunchResult
    ) {
        guard !launchInProgress else {
            completion(.failure("已有启动操作正在进行。"))
            return
        }
        launchInProgress = true
        Task { @MainActor in
            let result: LaunchResult
            do { result = try await operation() }
            catch { result = .failure(error.localizedDescription) }
            launchInProgress = false
            completion(result)
        }
    }

    /// Builds source only; this helper does not execute AppleScript or request elevation.
    /// Call from the app's explicit timezone action, never from a diagnostic timer.
    static func timezoneAppleScript(identifier: String) throws -> String {
        guard TimeZone.knownTimeZoneIdentifiers.contains(identifier) else {
            throw LaunchError.message("不支持的 IANA 时区标识。")
        }
        let shellArgument = "'" + identifier.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let command = "/usr/sbin/systemsetup -settimezone " + shellArgument
        let appleScriptLiteral = command
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "do shell script \"\(appleScriptLiteral)\" with administrator privileges"
    }

    private enum LaunchError: LocalizedError {
        case message(String)
        var errorDescription: String? {
            switch self { case .message(let message): return message }
        }
    }
}
