import AppKit
import Darwin

final class ClipboardGuest: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let status = NSTextField(labelWithString: "正在连接本地文字通道…")
    private var listener: Int32 = -1

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 510, height: 175),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "ClaudeShield 文字剪贴板"
        let instructions = NSTextField(wrappingLabelWithString:
            "在外层虚拟机窗口点击“发送文字”或“取回文字”。\n\n收到文字后，在目标输入框按 Command-V 粘贴。\n保持本工具运行，可以最小化窗口。")
        instructions.frame = NSRect(x: 20, y: 55, width: 465, height: 100)
        status.frame = NSRect(x: 20, y: 18, width: 465, height: 25)
        window.contentView?.addSubview(instructions)
        window.contentView?.addSubview(status)
        window.center()
        window.makeKeyAndOrderFront(nil)
        do {
            try startListener()
            status.stringValue = "已就绪 · 只在点击传输按钮时交换文字"
        } catch {
            status.stringValue = "未就绪：\(error.localizedDescription)"
        }
    }

    private func startListener() throws {
        var modelSize = 0
        guard sysctlbyname("hw.model", nil, &modelSize, nil, 0) == 0 else {
            throw ClipboardWire.failure("无法确认虚拟机型号。")
        }
        var model = [CChar](repeating: 0, count: modelSize)
        guard sysctlbyname("hw.model", &model, &modelSize, nil, 0) == 0,
              String(cString: model).hasPrefix("VirtualMac") else {
            throw ClipboardWire.failure("请在 macOS 虚拟机内打开本工具。")
        }
        let fd = socket(AF_VSOCK, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ClipboardWire.failure("本地虚拟通道不可用。") }
        var address = sockaddr_vm()
        address.svm_len = UInt8(MemoryLayout<sockaddr_vm>.size)
        address.svm_family = sa_family_t(AF_VSOCK)
        address.svm_port = ClipboardWire.port
        address.svm_cid = UInt32.max  // VMADDR_CID_ANY
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_vm>.size))
            }
        }
        guard result == 0, Darwin.listen(fd, 2) == 0 else {
            Darwin.close(fd)
            throw ClipboardWire.failure("无法监听文字通道；请确认新版启动器已启用通道，且本工具没有重复运行。")
        }
        listener = fd
        DispatchQueue.global(qos: .utility).async { [weak self] in
            while true {
                var peer = sockaddr_vm()
                var length = socklen_t(MemoryLayout<sockaddr_vm>.size)
                let client = withUnsafeMutablePointer(to: &peer) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        Darwin.accept(fd, $0, &length)
                    }
                }
                if client < 0 {
                    if errno == EINTR { continue }
                    break
                }
                // Only the host endpoint may access this guest's clipboard.
                guard peer.svm_cid == 2 else { Darwin.close(client); continue }
                self?.handle(client)
                Darwin.close(client)
            }
        }
    }

    private func handle(_ client: Int32) {
        do {
            try ClipboardWire.configure(client)
            let request = try ClipboardWire.receive(from: client)
            let reply: ClipboardWire.Message = DispatchQueue.main.sync {
                switch request.operation {
                case .set:
                    NSPasteboard.general.clearContents()
                    guard NSPasteboard.general.setString(request.text!, forType: .string) else {
                        return .init(operation: .error, text: "无法写入虚拟机剪贴板。")
                    }
                    self.status.stringValue = "已收到宿主文字 · 在目标输入框按 Command-V"
                    return .init(operation: .ok)
                case .get:
                    guard let text = NSPasteboard.general.string(forType: .string) else {
                        return .init(operation: .error, text: "虚拟机剪贴板中没有纯文字。")
                    }
                    guard text.utf8.count <= ClipboardWire.maxTextBytes else {
                        return .init(operation: .error, text: "一次最多传输 256 KiB 文字。")
                    }
                    self.status.stringValue = "已发送文字到宿主"
                    return .init(operation: .text, text: text)
                default:
                    return .init(operation: .error, text: "不支持的文字传输请求。")
                }
            }
            try ClipboardWire.send(reply, to: client)
        } catch {
            // Do not log clipboard data, peer messages, or decoded payloads.
            DispatchQueue.main.async { [weak self] in
                self?.status.stringValue = "文字传输未完成，请重新点击传输按钮。"
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct ClipboardGuestProgram {
    static func main() {
        let app = NSApplication.shared
        let delegate = ClipboardGuest()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
