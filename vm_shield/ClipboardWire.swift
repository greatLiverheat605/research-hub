import Foundation
import Darwin

// A text-only, request/response protocol over a local VM socket. No IP listener.
enum ClipboardWire {
    static let port: UInt32 = 51785
    static let maxTextBytes = 256 * 1024
    static let maxFrameBytes = 2 * 1024 * 1024

    struct Message: Codable, Sendable {
        enum Operation: String, Codable, Sendable { case get, set, text, ok, error }
        let operation: Operation
        var text: String? = nil
    }

    static func failure(_ message: String) -> NSError {
        NSError(domain: "ClaudeShieldClipboard", code: 1,
                userInfo: [NSLocalizedDescriptionKey: message])
    }

    static func configure(_ fd: Int32) throws {
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        var noSignal: Int32 = 1
        guard setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                         socklen_t(MemoryLayout<timeval>.size)) == 0,
              setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout,
                         socklen_t(MemoryLayout<timeval>.size)) == 0,
              setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal,
                         socklen_t(MemoryLayout<Int32>.size)) == 0 else {
            throw failure("无法设置本地通道超时。")
        }
    }

    static func send(_ message: Message, to fd: Int32) throws {
        try validate(message)
        let payload = try JSONEncoder().encode(message)
        guard payload.count <= maxFrameBytes else { throw failure("文字超过传输上限。") }
        var size = UInt32(payload.count).bigEndian
        let header = withUnsafeBytes(of: &size) { Data($0) }
        try writeAll(header + payload, to: fd)
    }

    // SO_RCVTIMEO bounds each read; this bounds the whole frame so a peer that
    // drips one byte at a time cannot hold the channel open indefinitely.
    static func receive(from fd: Int32, within seconds: TimeInterval = 10) throws -> Message {
        let deadline = Date().addingTimeInterval(seconds)
        let header = try readExactly(4, from: fd, deadline: deadline)
        let count = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard count > 0, count <= maxFrameBytes else { throw failure("无效的文字传输长度。") }
        let payload = try readExactly(Int(count), from: fd, deadline: deadline)
        let message = try JSONDecoder().decode(Message.self, from: payload)
        try validate(message)
        return message
    }

    private static func validate(_ message: Message) throws {
        guard (message.text?.utf8.count ?? 0) <= maxTextBytes else {
            throw failure("一次最多传输 256 KiB 文字。")
        }
        switch message.operation {
        case .set, .text, .error:
            guard message.text != nil else { throw failure("缺少文字内容。") }
        case .get, .ok:
            guard message.text == nil else { throw failure("无效的文字传输消息。") }
        }
    }

    // Text that came from the guest is untrusted. Characters that can hide or
    // reorder what the user sees (C0/C1 controls other than tab/newline, bidi
    // overrides, zero-width marks) are rejected before it reaches the host pasteboard.
    static func unsafeGuestTextReason(_ text: String) -> String? {
        for scalar in text.unicodeScalars {
            switch scalar.value {
            case 0x09, 0x0A, 0x0D:
                continue
            case 0x00...0x1F, 0x7F...0x9F:
                return "包含不可见控制字符（U+\(String(format: "%04X", scalar.value))）"
            case 0x061C, 0x200B...0x200F, 0x202A...0x202E, 0x2066...0x2069, 0xFEFF:
                return "包含会隐藏或倒序显示文字的字符（U+\(String(format: "%04X", scalar.value))）"
            default:
                continue
            }
        }
        return nil
    }

    // Guest-supplied status text is shown on the host only after it is made
    // single-line, stripped of invisible characters, and truncated.
    static func displaySafe(_ text: String, limit: Int = 120) -> String {
        var scalars = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            if scalar.value < 0x20 || (0x7F...0x9F).contains(scalar.value) {
                scalars.append(" ")
            } else if unsafeGuestTextReason(String(scalar)) == nil {
                scalars.append(scalar)
            }
        }
        let line = String(scalars)
        return line.count > limit ? String(line.prefix(limit)) + "…" : line
    }

    private static func readExactly(_ count: Int, from fd: Int32, deadline: Date) throws -> Data {
        var data = Data(count: count)
        try data.withUnsafeMutableBytes { buffer in
            var offset = 0
            while offset < count {
                guard Date() < deadline else { throw failure("本地通道传输超时。") }
                let result = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), count - offset)
                if result < 0 && errno == EINTR { continue }
                guard result > 0 else { throw failure("本地通道中断或超时。") }
                offset += result
            }
        }
        return data
    }

    private static func writeAll(_ data: Data, to fd: Int32) throws {
        try data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < data.count {
                let result = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), data.count - offset)
                if result < 0 && errno == EINTR { continue }
                guard result > 0 else { throw failure("本地通道中断或超时。") }
                offset += result
            }
        }
    }
}
