import Foundation
import Darwin

// In-process stand-in for the guest clipboard tool, used only by --panel-demo
// and --panel-selftest. It speaks the real ClipboardWire framing over a local
// socketpair, so the host transfer path is exercised without any VM or network.
final class DemoGuest: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: String?

    init(clipboard: String?) { stored = clipboard }

    var clipboard: String? {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }

    // Returns the host end of a fresh channel; the guest end is served on a
    // background thread and closed after one request, like ClipboardGuest.
    func connect() throws -> Int32 {
        var pair: [Int32] = [-1, -1]
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &pair) == 0 else {
            throw ClipboardWire.failure("无法创建演示通道。")
        }
        let guestEnd = pair[1]
        Thread.detachNewThread { [self] in
            defer { Darwin.close(guestEnd) }
            do {
                try ClipboardWire.configure(guestEnd)
                let request = try ClipboardWire.receive(from: guestEnd)
                let reply: ClipboardWire.Message
                switch request.operation {
                case .set:
                    clipboard = request.text
                    reply = .init(operation: .ok)
                case .get:
                    if let text = clipboard {
                        reply = .init(operation: .text, text: text)
                    } else {
                        reply = .init(operation: .error, text: "虚拟机剪贴板中没有纯文字。")
                    }
                default:
                    reply = .init(operation: .error, text: "不支持的文字传输请求。")
                }
                try ClipboardWire.send(reply, to: guestEnd)
            } catch {}
        }
        return pair[0]
    }
}
