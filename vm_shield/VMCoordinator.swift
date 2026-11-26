import AppKit
import SwiftUI
import Virtualization
import Darwin

// Two VMs, one private Ethernet socket pair. Only the Linux gateway has a NAT NIC.
// The protected macOS VM is never given NAT, bridged, or host-only networking.
@MainActor
final class Coordinator: NSObject, NSApplicationDelegate, @preconcurrency VZVirtualMachineDelegate {
    private var root: URL!
    private var window: NSWindow!
    private let view = VZVirtualMachineView()
    private let panel = ControlPanelModel()
    private var pendingDecision: CheckedContinuation<Bool, Never>?
    // --panel-demo / --panel-selftest: no VM, no environment; an in-process guest
    // and a private pasteboard stand in so the host UI and wire path can be checked.
    private var demoGuest: DemoGuest?
    private var hostPasteboard = NSPasteboard.general
    private var gateway: VZVirtualMachine?
    private var mac: VZVirtualMachine?
    private var handles: [FileHandle] = []
    private var gatewayConsole: CappedConsoleLog?
    private var installer: VZMacOSInstaller?
    private var progressObserver: NSKeyValueObservation?
    private var lockDescriptor: Int32 = -1
    private var installing = false
    private var quitting = false
    private var gatewayReadiness: GatewayReadiness?
    private var gatewayDeploymentEvidence: GatewayDeploymentEvidence?
    private var gatewayReadyStatus: GatewayReadyStatus?
    private var gatewayRunID: String?
    private var runChallengeDirectory: URL?
    private var runChallengeISO: URL?
    private var gatewayStoppedNormally = false
    private var gatewayStopError: Error?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = CommandLine.arguments
        if let i = arguments.firstIndex(of: "--environment"), arguments.indices.contains(i + 1) {
            root = URL(fileURLWithPath: arguments[i + 1], isDirectory: true)
        } else if let path = Bundle.main.object(forInfoDictionaryKey: "EnvironmentPath") as? String {
            root = URL(fileURLWithPath: path, isDirectory: true)
        }
        if arguments.contains("--panel-demo") || arguments.contains("--panel-selftest") {
            startDemo(selfTest: arguments.firstIndex(of: "--panel-selftest").map {
                arguments.indices.contains($0 + 1) ? URL(fileURLWithPath: arguments[$0 + 1]) : nil
            })
            return
        }
        buildWindow()
        Task { @MainActor in
            do {
                guard root != nil, VZVirtualMachine.isSupported else {
                    throw failure("缺少环境目录，或此 Mac 不支持虚拟化。")
                }
                lockDescriptor = open(root.appendingPathComponent("environment.lock").path,
                                      O_CREAT | O_RDWR, 0o600)
                guard lockDescriptor >= 0, flock(lockDescriptor, LOCK_EX | LOCK_NB) == 0 else {
                    throw failure("该专用环境已经在运行。")
                }
                let installed = FileManager.default.fileExists(atPath:
                    root.appendingPathComponent("macos-installed").path)
                let mode = try GatewayLaunchMode(arguments: arguments)
                let prepareGateway = mode == .prepareGateway
                let stageGatewayUpdate = mode == .stageGatewayUpdate
                try await configure(gatewayOnly: !mode.configuresMacOS, offlineGatewayWAN: mode.isolatesWAN)
                if arguments.contains("--validate-only") {
                    writeStatus("validated", detail: (prepareGateway || stageGatewayUpdate)
                        ? "Gateway-only configuration validated; no VM started."
                        : "Both VM configurations validated; no VM started.")
                    NSApp.terminate(nil)
                    return
                }
                try await startGateway()
                if stageGatewayUpdate {
                    panel.message = "正在暂存网关更新；macOS 不会配置或启动。"
                    try await waitForGatewayMigration()
                    writeStatus("migration-staged", detail:
                        "Current-run deployment receipt verified; gateway shut down normally; forwarding off; cold boot acceptance pending.")
                    NSApp.terminate(nil)
                    return
                }
                panel.message = "网关正在配置；macOS 尚未启动。"
                try await waitForGateway()
                if prepareGateway {
                    panel.message = "网关已配置，正在正常关机保存。"
                    let marker = root.appendingPathComponent("gateway-provisioned")
                    try? Data("provisioned\n".utf8).write(to: marker, options: .atomic)
                    if let gateway {
                        try gateway.requestStop()
                        let deadline = ContinuousClock.now.advanced(by: .seconds(60))
                        while gateway.state != .stopped && ContinuousClock.now < deadline {
                            try await Task.sleep(for: .seconds(1))
                        }
                        guard gateway.state == .stopped else { throw failure("网关未完成正常关机。") }
                    }
                    writeStatus("gateway-provisioned", detail: "Firewall installed and guest shut down cleanly.")
                    NSApp.terminate(nil)
                    return
                }
                if installed {
                    try await startMac()
                } else {
                    try await installMac()
                }
            } catch {
                report(error)
            }
        }
    }

    private func buildWindow(display: NSView? = nil) {
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "ClaudeShield"
        window.minSize = NSSize(width: 900, height: 600)
        let container = NSView()
        window.contentView = container
        panel.onSend = { [weak self] in self?.sendClipboardText() }
        panel.onFetch = { [weak self] in self?.getClipboardText() }
        panel.onConfirm = { [weak self] in self?.resolvePending(true) }
        panel.onCancel = { [weak self] in self?.resolvePending(false) }
        let sidebar = NSHostingView(rootView: ControlPanelView(model: panel))
        sidebar.translatesAutoresizingMaskIntoConstraints = false
        let screen = display ?? view
        view.capturesSystemKeys = true
        screen.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(sidebar)
        container.addSubview(screen)
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            sidebar.topAnchor.constraint(equalTo: container.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            sidebar.widthAnchor.constraint(equalToConstant: 320),
            screen.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor),
            screen.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            screen.topAnchor.constraint(equalTo: container.topAnchor),
            screen.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        ])
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @discardableResult
    private func sendClipboardText() -> Task<Void, Never>? {
        guard let text = hostPasteboard.string(forType: .string) else {
            panel.transferMessage = "宿主剪贴板中没有纯文字。"
            return nil
        }
        guard text.utf8.count <= ClipboardWire.maxTextBytes else {
            panel.transferMessage = "一次最多传输 256 KiB 文字。"
            return nil
        }
        return transferClipboard(.init(operation: .set, text: text))
    }

    @discardableResult
    private func getClipboardText() -> Task<Void, Never>? {
        transferClipboard(.init(operation: .get))
    }

    // One request/response per connection. The host always dials; the guest
    // (or the demo stand-in) never gets a way to open a channel to the host.
    private func openGuestChannel() async throws -> (fd: Int32, release: () -> Void) {
        if let demoGuest { return (try demoGuest.connect(), {}) }
        guard mac?.state == .running,
              let device = mac?.socketDevices.first as? VZVirtioSocketDevice else {
            throw ClipboardWire.failure("请先启动专用 macOS 虚拟机。")
        }
        let connection = try await device.connect(toPort: ClipboardWire.port)
        let fd = dup(connection.fileDescriptor)
        guard fd >= 0 else {
            connection.close()
            throw ClipboardWire.failure("无法打开文字通道。")
        }
        return (fd, { connection.close() })
    }

    private func transferClipboard(_ request: ClipboardWire.Message) -> Task<Void, Never>? {
        guard panel.canTransfer else { return nil }
        panel.busy = true
        panel.transferMessage = "正在传输文字…"
        let pasteboardVersion = hostPasteboard.changeCount
        let toGuest = request.operation == .set
        return Task { @MainActor in
            defer { panel.busy = false }
            do {
                let channel = try await openGuestChannel()
                defer { channel.release() }
                let fd = channel.fd
                let reply = try await Task.detached {
                    defer { Darwin.close(fd) }
                    try ClipboardWire.configure(fd)
                    try ClipboardWire.send(request, to: fd)
                    return try ClipboardWire.receive(from: fd)
                }.value
                if reply.operation == .error {
                    throw ClipboardWire.failure("虚拟机返回：" + ClipboardWire.displaySafe(reply.text ?? "文字传输失败。"))
                }
                if request.operation == .get {
                    guard reply.operation == .text, let text = reply.text else {
                        throw ClipboardWire.failure("未收到虚拟机文字。")
                    }
                    if let reason = ClipboardWire.unsafeGuestTextReason(text) {
                        throw ClipboardWire.failure("已拒绝虚拟机文字：\(reason)。")
                    }
                    guard await confirmGuestText(text) else {
                        panel.transferMessage = "已取消，宿主剪贴板未改变。"
                        panel.record(toGuest: false, summary: "已取消取回", succeeded: false)
                        return
                    }
                    guard hostPasteboard.changeCount == pasteboardVersion else {
                        throw ClipboardWire.failure("宿主剪贴板刚有更新，请重新点击取回。")
                    }
                    hostPasteboard.clearContents()
                    guard hostPasteboard.setString(text, forType: .string) else {
                        throw ClipboardWire.failure("无法写入宿主剪贴板。")
                    }
                    panel.transferMessage = "已取回文字 · 切回宿主应用按 Command-V"
                    panel.record(toGuest: false, summary: Self.summary(text), succeeded: true)
                } else {
                    guard reply.operation == .ok else { throw ClipboardWire.failure("虚拟机未确认接收。") }
                    panel.transferMessage = "已发送文字 · 在虚拟机输入框按 Command-V"
                    panel.record(toGuest: true, summary: Self.summary(request.text ?? ""), succeeded: true)
                    if demoGuest == nil { window.makeFirstResponder(view) }
                }
            } catch {
                panel.transferMessage = "未完成：\(error.localizedDescription)"
                panel.record(toGuest: toGuest, summary: ClipboardWire.displaySafe(error.localizedDescription, limit: 26),
                             succeeded: false)
            }
        }
    }

    private static func summary(_ text: String) -> String {
        let firstLine = ClipboardWire.displaySafe(text.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? "", limit: 40)
        return "\(text.count) 字符 · " + (firstLine.isEmpty ? "（空行开头）" : firstLine)
    }

    // Guest text is untrusted: show what will land on the host pasteboard and
    // require an explicit choice, so a multi-line command cannot arrive unseen.
    private func confirmGuestText(_ text: String) async -> Bool {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).count
        panel.pending = .init(text: text, characters: text.count, lines: lines)
        panel.transferMessage = "请检查预览，确认后才会写入宿主剪贴板。"
        NSApp.requestUserAttention(.informationalRequest)
        return await withCheckedContinuation { pendingDecision = $0 }
    }

    private func resolvePending(_ accepted: Bool) {
        panel.pending = nil
        pendingDecision?.resume(returning: accepted)
        pendingDecision = nil
    }

    private func configure(gatewayOnly: Bool, offlineGatewayWAN: Bool) async throws {
        guard !offlineGatewayWAN || gatewayOnly else {
            throw failure("离线维护模式必须只配置网关。")
        }
        // A missing/legacy manifest is a hard error before any VM is configured.
        let manifest = try GatewayManifest.load(from: root.appendingPathComponent("manifest.json"))
        let runID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        gatewayReadiness = try GatewayReadiness(manifest: manifest, runID: runID)
        gatewayDeploymentEvidence = try GatewayDeploymentEvidence(manifest: manifest, runID: runID)
        gatewayRunID = runID
        gatewayReadyStatus = nil
        gatewayStoppedNormally = false
        gatewayStopError = nil
        // Runs only when this app is deliberately configured (including a future
        // --validate-only invocation). Building the app does not invoke hdiutil.
        runChallengeISO = try createRunChallenge(runID: runID, manifest: manifest)
        var sockets: [Int32] = [-1, -1]
        guard socketpair(AF_UNIX, SOCK_DGRAM, 0, &sockets) == 0 else {
            throw failure("无法创建虚拟机之间的私有网络。")
        }
        for socket in sockets {
            var send: Int32 = 65536
            var receive: Int32 = 262144
            _ = setsockopt(socket, SOL_SOCKET, SO_SNDBUF, &send, socklen_t(MemoryLayout<Int32>.size))
            _ = setsockopt(socket, SOL_SOCKET, SO_RCVBUF, &receive, socklen_t(MemoryLayout<Int32>.size))
            handles.append(FileHandle(fileDescriptor: socket, closeOnDealloc: true))
        }
        let gateConfig = try gatewayConfiguration(lan: handles[0], offlineWAN: offlineGatewayWAN)
        try gateConfig.validate()
        gateway = VZVirtualMachine(configuration: gateConfig)
        gateway?.delegate = self
        if gatewayOnly { view.virtualMachine = gateway }
        if !gatewayOnly {
            let macConfig = try await macConfiguration(lan: handles[1])
            try macConfig.validate()
            mac = VZVirtualMachine(configuration: macConfig)
            mac?.delegate = self
            view.virtualMachine = mac
        }
    }

    private func gatewayConfiguration(lan: FileHandle, offlineWAN: Bool) throws -> VZVirtualMachineConfiguration {
        let config = VZVirtualMachineConfiguration()
        let boot = VZEFIBootLoader()
        let efi = root.appendingPathComponent("gateway-efi.bin")
        boot.variableStore = FileManager.default.fileExists(atPath: efi.path)
            ? VZEFIVariableStore(url: efi)
            : try VZEFIVariableStore(creatingVariableStoreAt: efi)
        config.bootLoader = boot
        config.cpuCount = 2
        config.memorySize = 1024 * 1024 * 1024
        config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
        let graphics = VZVirtioGraphicsDeviceConfiguration()
        graphics.scanouts = [VZVirtioGraphicsScanoutConfiguration(widthInPixels: 1280, heightInPixels: 720)]
        config.graphicsDevices = [graphics]
        config.keyboards = [VZUSBKeyboardConfiguration()]
        config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
        config.storageDevices = [
            try blockDevice("gateway.raw", readOnly: false),
            try blockDevice("gateway-seed.iso", readOnly: true)
        ]
        guard let runChallengeISO else { throw failure("缺少本次启动的只读 CSRUN 挑战盘。") }
        config.storageDevices.append(VZVirtioBlockDeviceConfiguration(attachment:
            try VZDiskImageStorageDeviceAttachment(url: runChallengeISO, readOnly: true)))
        let wan = VZVirtioNetworkDeviceConfiguration()
        wan.macAddress = VZMACAddress(string: "52:54:00:77:00:01")!
        if offlineWAN {
            // Migration has no NAT, bridge, or host IP interface. Retain the
            // otherwise unused socket peer only; no code reads or forwards it.
            var sockets: [Int32] = [-1, -1]
            guard socketpair(AF_UNIX, SOCK_DGRAM, 0, &sockets) == 0 else {
                throw failure("无法创建离线维护网卡。")
            }
            let endpoint = FileHandle(fileDescriptor: sockets[0], closeOnDealloc: true)
            let unusedPeer = FileHandle(fileDescriptor: sockets[1], closeOnDealloc: true)
            handles.append(contentsOf: [endpoint, unusedPeer])
            wan.attachment = VZFileHandleNetworkDeviceAttachment(fileHandle: endpoint)
        } else {
            wan.attachment = VZNATNetworkDeviceAttachment()
        }
        let privateNIC = VZVirtioNetworkDeviceConfiguration()
        privateNIC.macAddress = VZMACAddress(string: "52:54:00:77:00:02")!
        privateNIC.attachment = VZFileHandleNetworkDeviceAttachment(fileHandle: lan)
        config.networkDevices = [wan, privateNIC]
        let logURL = root.appendingPathComponent("gateway-console.log")
        // Never append to an old boot, follow a symlink, or ignore truncation errors.
        let logFD = open(logURL.path, O_CREAT | O_WRONLY | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard logFD >= 0 else { throw failure("无法安全重建本次网关控制台日志。") }
        var logInfo = stat()
        guard fstat(logFD, &logInfo) == 0, (logInfo.st_mode & S_IFMT) == S_IFREG,
              fchmod(logFD, 0o600) == 0 else {
            Darwin.close(logFD)
            throw failure("网关控制台日志必须为私有普通文件。")
        }
        let log = FileHandle(fileDescriptor: logFD, closeOnDealloc: true)
        handles.append(log)
        let console = CappedConsoleLog(file: log)
        gatewayConsole = console
        panel.setCheck("console", .pass, "超出 16 MiB 的串口输出直接丢弃，不写入磁盘")
        let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
        serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: nil,
                                                            fileHandleForWriting: console.guestWriter)
        config.serialPorts = [serial]
        return config
    }

    private func macConfiguration(lan: FileHandle) async throws -> VZVirtualMachineConfiguration {
        let fm = FileManager.default
        let hardwareURL = root.appendingPathComponent("mac-hardware.bin")
        let identifierURL = root.appendingPathComponent("mac-identifier.bin")
        let hardware: VZMacHardwareModel
        if fm.fileExists(atPath: hardwareURL.path) {
            guard let saved = VZMacHardwareModel(dataRepresentation: try Data(contentsOf: hardwareURL)),
                  saved.isSupported else { throw failure("已保存的 macOS 硬件型号不受此宿主支持。") }
            hardware = saved
        } else {
            let image: VZMacOSRestoreImage = try await withCheckedThrowingContinuation { continuation in
                VZMacOSRestoreImage.load(from: root.appendingPathComponent("images/macos.ipsw")) {
                    continuation.resume(with: $0)
                }
            }
            guard let requirements = image.mostFeaturefulSupportedConfiguration else {
                throw failure("Apple 恢复镜像不支持此 Mac。")
            }
            hardware = requirements.hardwareModel
            try hardware.dataRepresentation.write(to: hardwareURL, options: .atomic)
        }
        let platform = VZMacPlatformConfiguration()
        platform.hardwareModel = hardware
        if fm.fileExists(atPath: identifierURL.path) {
            guard let saved = VZMacMachineIdentifier(dataRepresentation: try Data(contentsOf: identifierURL)) else {
                throw failure("虚拟机标识文件损坏。")
            }
            platform.machineIdentifier = saved
        } else {
            platform.machineIdentifier = VZMacMachineIdentifier()
            try platform.machineIdentifier.dataRepresentation.write(to: identifierURL, options: .atomic)
        }
        let auxiliary = root.appendingPathComponent("mac-auxiliary.bin")
        platform.auxiliaryStorage = fm.fileExists(atPath: auxiliary.path)
            ? VZMacAuxiliaryStorage(contentsOf: auxiliary)
            : try VZMacAuxiliaryStorage(creatingStorageAt: auxiliary, hardwareModel: hardware)
        let config = VZVirtualMachineConfiguration()
        config.platform = platform
        config.bootLoader = VZMacOSBootLoader()
        config.cpuCount = 6
        config.memorySize = 12 * 1024 * 1024 * 1024
        config.storageDevices = [try blockDevice("macos.raw", readOnly: false)]
        // Local installation also works if the guest's virtiofs mount fails.
        // This image is a data disk; it adds no network path or guest service.
        if fm.fileExists(atPath: root.appendingPathComponent("guest-installer.raw").path) {
            config.storageDevices.append(try blockDevice("guest-installer.raw", readOnly: true))
        }
        if fm.fileExists(atPath: root.appendingPathComponent("clipboard-tools.raw").path) {
            config.storageDevices.append(try blockDevice("clipboard-tools.raw", readOnly: true))
        }
        // Explicit text transfer only. This transport uses no IP or Ethernet.
        config.socketDevices = [VZVirtioSocketDeviceConfiguration()]
        config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
        let display = VZMacGraphicsDeviceConfiguration()
        display.displays = [VZMacGraphicsDisplayConfiguration(widthInPixels: 1920,
                                                             heightInPixels: 1200,
                                                             pixelsPerInch: 110)]
        config.graphicsDevices = [display]
        config.keyboards = [VZUSBKeyboardConfiguration()]
        config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
        let nic = VZVirtioNetworkDeviceConfiguration()
        nic.macAddress = VZMACAddress(string: "52:54:00:77:00:03")!
        nic.attachment = VZFileHandleNetworkDeviceAttachment(fileHandle: lan)
        // This is the macOS guest's ONLY network interface; it cannot reach host NAT directly.
        config.networkDevices = [nic]
        panel.setCheck("nic", .pass, "1 张私有链路网卡，无 NAT、桥接或宿主网络")
        let shareURL = root.appendingPathComponent("share", isDirectory: true)
        do {
            try GuestShareAudit.verify(shareURL)
            panel.setCheck("share", .pass, "只读挂载；无符号链接、硬链接或特殊文件")
        } catch {
            panel.setCheck("share", .fail, error.localizedDescription)
            throw error
        }
        let shared = VZVirtioFileSystemDeviceConfiguration(tag: "claudeshield")
        shared.share = VZSingleDirectoryShare(directory: VZSharedDirectory(url: shareURL, readOnly: true))
        config.directorySharingDevices = [shared]
        return config
    }

    private func blockDevice(_ name: String, readOnly: Bool) throws -> VZVirtioBlockDeviceConfiguration {
        let attachment = try VZDiskImageStorageDeviceAttachment(url: root.appendingPathComponent(name),
                                                                readOnly: readOnly)
        return VZVirtioBlockDeviceConfiguration(attachment: attachment)
    }

    private func createRunChallenge(runID: String, manifest: GatewayManifest) throws -> URL {
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("claudeshield-run-\(UUID().uuidString)",
                                                                    isDirectory: true)
        try fm.createDirectory(at: directory, withIntermediateDirectories: false,
                               attributes: [.posixPermissions: 0o700])
        do {
            let source = directory.appendingPathComponent("content", isDirectory: true)
            try fm.createDirectory(at: source, withIntermediateDirectories: false,
                                   attributes: [.posixPermissions: 0o700])
            let challenge: [String: Any] = ["schema_version": 1, "run_id": runID,
                                            "gateway_bundle_sha256": manifest.bundleSHA256]
            try JSONSerialization.data(withJSONObject: challenge, options: [.sortedKeys])
                .write(to: source.appendingPathComponent("run.json"), options: .atomic)
            try fm.setAttributes([.posixPermissions: 0o600],
                                 ofItemAtPath: source.appendingPathComponent("run.json").path)
            let iso = directory.appendingPathComponent("CSRUN.iso")
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/hdiutil")
            process.arguments = ["makehybrid", "-iso", "-joliet", "-default-volume-name", "CSRUN",
                                 "-o", iso.path, source.path]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
            process.waitUntilExit()
            guard process.terminationReason == .exit, process.terminationStatus == 0,
                  fm.fileExists(atPath: iso.path) else {
                throw failure("无法创建本次启动的只读 CSRUN 挑战盘。")
            }
            try fm.setAttributes([.posixPermissions: 0o400], ofItemAtPath: iso.path)
            runChallengeDirectory = directory
            return iso
        } catch {
            try? fm.removeItem(at: directory)
            throw error
        }
    }

    private func readCurrentGatewayStatus() throws -> GatewayReadyStatus? {
        guard gatewayReadiness != nil else { throw failure("缺少网关就绪校验器。") }
        let logURL = root.appendingPathComponent("gateway-console.log")
        let values = try logURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true,
              let size = values.fileSize, size <= GatewayReadiness.maximumLogBytes else {
            throw failure("网关控制台日志不是有效的本次启动日志。")
        }
        let result = try gatewayReadiness!.evaluate(log: Data(contentsOf: logURL))
        gatewayReadyStatus = result
        return result
    }

    private func startGateway() async throws {
        guard let gateway else { throw failure("网关配置不可用。") }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            gateway.start { continuation.resume(with: $0) }
        }
        writeStatus("gateway-starting", detail: "Waiting for guest firewall service.")
    }

    private func waitForGateway() async throws {
        guard let root else { throw failure("缺少环境目录。") }
        let deadline = ContinuousClock.now.advanced(by: .seconds(1200))
        let provisionedMarker = root.appendingPathComponent("gateway-provisioned")
        let alreadyProvisioned = FileManager.default.fileExists(atPath: provisionedMarker.path) ||
                                 FileManager.default.fileExists(atPath: root.appendingPathComponent("macos-installed").path)
        while ContinuousClock.now < deadline {
            guard gateway?.state == .running else { throw failure("网关已停止；macOS 不会启动。") }
            let ready = try readCurrentGatewayStatus()
            let log = try String(contentsOf: root.appendingPathComponent("gateway-console.log"), encoding: .utf8)
            // First-boot provisioning may restart services. Wait until it has ended
            // as well as seeing the current firewall version and its isolated tests.
            let provisioningFinished = log.range(of: #"Cloud-init v\..* finished at"#,
                                                 options: .regularExpression) != nil
            if ready != nil && (alreadyProvisioned || provisioningFinished) {
                if provisioningFinished && !FileManager.default.fileExists(atPath: provisionedMarker.path) {
                    try? Data("provisioned\n".utf8).write(to: provisionedMarker, options: .atomic)
                }
                panel.setCheck("gateway", .pass, "本次启动自检通过：仅转发到固定中转，网关自身出站被拒绝")
                writeStatus("gateway-ready",
                            detail: "isolation-ready & local-network-ready; public-egress-unverified (no remote probe).")
                return
            }
            try await Task.sleep(for: .seconds(2))
        }
        throw failure("网关配置超时；请查看本地 gateway-console.log。macOS 未启动。")
    }

    private func waitForGatewayMigration() async throws {
        guard let gateway, gatewayDeploymentEvidence != nil else {
            throw failure("缺少本次网关迁移配置。")
        }
        let deadline = ContinuousClock.now.advanced(by: .seconds(1200))
        while ContinuousClock.now < deadline {
            if let gatewayStopError { throw gatewayStopError }
            let logURL = root.appendingPathComponent("gateway-console.log")
            let values = try logURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true,
                  let size = values.fileSize, size <= GatewayReadiness.maximumLogBytes else {
                throw failure("网关迁移日志无效。")
            }
            let receipt = try gatewayDeploymentEvidence!.evaluate(log: Data(contentsOf: logURL))
            let stop: GatewayMigrationStopState = gateway.state == .stopped && gatewayStoppedNormally
                ? .stoppedNormally : .running
            if try GatewayMigrationCompletion.isComplete(receipt: receipt, stop: stop) {
                return
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw failure("网关迁移或正常关机超时；macOS 未配置或启动。")
    }

    private func installMac() async throws {
        guard let mac else { throw failure("macOS 配置不可用。") }
        guard gateway?.state == .running, try readCurrentGatewayStatus() != nil else {
            throw failure("本次网关完整证据未就绪，macOS 安装不会启动。")
        }
        installing = true
        panel.message = "正在安装 macOS；所有联网仅经过受限网关。"
        let task = VZMacOSInstaller(virtualMachine: mac,
                                  restoringFromImageAt: root.appendingPathComponent("images/macos.ipsw"))
        installer = task
        progressObserver = task.progress.observe(\.fractionCompleted, options: [.new]) { [weak self] progress, _ in
            DispatchQueue.main.async {
                self?.panel.message = "正在安装 macOS：\(Int(progress.fractionCompleted * 100))%"
                self?.writeStatus("installing", detail: String(progress.fractionCompleted))
            }
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            task.install { continuation.resume(with: $0) }
        }
        progressObserver = nil
        installer = nil
        installing = false
        try Data("installed\n".utf8).write(to: root.appendingPathComponent("macos-installed"), options: .atomic)
        writeStatus("installed", detail: "macOS installation completed; first-run account setup remains.")
        // Restore completion can arrive before the recovery VM's final stop event.
        // End only that completed installer session before a normal macOS boot.
        if mac.canStop { try await mac.stop() }
        let deadline = ContinuousClock.now.advanced(by: .seconds(20))
        while mac.state != .stopped && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(100))
        }
        guard mac.state == .stopped else {
            throw failure("macOS 已安装，但恢复环境尚未停止；关闭此窗口后重新打开即可正常启动。")
        }
        try await startMac()
    }

    private func startMac() async throws {
        guard let mac else { throw failure("macOS 配置不可用。") }
        guard gateway?.state == .running, try readCurrentGatewayStatus() != nil else {
            throw failure("本次网关完整证据未就绪，macOS 不会启动。")
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            mac.start { continuation.resume(with: $0) }
        }
        panel.message = "专用 macOS 已启动；唯一网络路径经过受限网关。"
        writeStatus("running", detail: "Protected guest running; no remote connectivity assertion.")
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        if virtualMachine === gateway {
            gatewayStoppedNormally = true
            panel.message = "网关已停止；专用 macOS 没有其他网络出口。"
            writeStatus("gateway-stopped", detail: "Private link has no alternate network attachment.")
        } else {
            panel.message = "专用 macOS 已关机。"
            writeStatus("stopped", detail: "Protected guest shut down.")
        }
    }

    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        if virtualMachine === gateway { gatewayStopError = error }
        report(error)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        if let runChallengeDirectory { try? FileManager.default.removeItem(at: runChallengeDirectory) }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting || (mac?.state != .running && gateway?.state != .running) { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "关闭专用环境？"
        alert.informativeText = installing ? "系统安装尚未完成，关闭可能需要重新安装。" : "请先在虚拟机内关机，避免丢失未保存的工作。"
        alert.addButton(withTitle: "继续运行")
        alert.addButton(withTitle: "停止并退出")
        guard alert.runModal() == .alertSecondButtonReturn else { return .terminateCancel }
        quitting = true
        Task { @MainActor in
            if let mac, mac.canStop { try? await mac.stop() }
            if let gateway, gateway.canStop { try? await gateway.stop() }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func writeStatus(_ state: String, detail: String) {
        panel.apply(state: state, detail: detail)
        guard let root else { return }
        var payload: [String: Any] = [
            "state": state,
            "detail": detail,
            "pid": getpid(),
            "updatedAt": ISO8601DateFormatter().string(from: Date()),
            "isolationReady": (state == "gateway-ready" || state == "running"),
            "localNetworkReady": (state == "gateway-ready" || state == "running"),
            "normalShutdown": gatewayStoppedNormally,
            "publicEgressVerified": false
        ]
        if let gatewayRunID { payload["gatewayRunID"] = gatewayRunID }
        if let gatewayReadyStatus {
            payload["gatewayBootID"] = gatewayReadyStatus.bootID
            payload["gatewaySequence"] = gatewayReadyStatus.sequence
        }
        let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        try? data?.write(to: root.appendingPathComponent("status.json"), options: .atomic)
    }

    private func startDemo(selfTest output: URL??) {
        let guest = DemoGuest(clipboard: "cd ~/project\nclaude\n# 在虚拟机里运行，结果用“取回到宿主”带回")
        demoGuest = guest
        hostPasteboard = NSPasteboard(name: NSPasteboard.Name("local.claudeshield.panel-demo"))
        hostPasteboard.clearContents()
        hostPasteboard.setString("git status --short", forType: .string)
        panel.stage = .demo
        panel.message = "演示模式：没有配置或启动任何虚拟机，也没有网络访问。"
        panel.transferMessage = "演示剪贴板已就绪，可以试试两个方向的传输。"
        for id in ["nic", "share", "console", "gateway"] {
            panel.setCheck(id, .info, "演示模式未启动虚拟机，启动真实环境后检查")
        }
        buildWindow(display: NSHostingView(rootView: DemoDisplayView()))
        guard let output else { return }
        Task { @MainActor in
            let passed = await PanelSelfTest(panel: panel, guest: guest, pasteboard: hostPasteboard,
                                              window: window, output: output,
                                              send: { self.sendClipboardText() },
                                              fetch: { self.getClipboardText() }).run()
            hostPasteboard.releaseGlobally()
            exit(passed ? 0 : 1)
        }
    }

    private func report(_ error: Error) {
        panel.message = "专用环境未就绪：\(error.localizedDescription)"
        writeStatus("error", detail: error.localizedDescription)
    }

    private func failure(_ text: String) -> NSError {
        NSError(domain: "ClaudeShieldVM", code: 1, userInfo: [NSLocalizedDescriptionKey: text])
    }
}

@main
struct VMProgram {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let coordinator = Coordinator()
        app.delegate = coordinator
        withExtendedLifetime(coordinator) { app.run() }
    }
}
