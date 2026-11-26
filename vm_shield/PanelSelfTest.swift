import AppKit

// Drives the real panel and transfer code against DemoGuest (--panel-selftest),
// asserting each hop and saving a PNG of the window after every step.
@MainActor
struct PanelSelfTest {
    let panel: ControlPanelModel
    let guest: DemoGuest
    let pasteboard: NSPasteboard
    let window: NSWindow
    let output: URL?
    let send: () -> Task<Void, Never>?
    let fetch: () -> Task<Void, Never>?

    func run() async -> Bool {
        var failures: [String] = []
        func expect(_ condition: Bool, _ name: String) {
            print((condition ? "PASS " : "FAIL ") + name)
            if !condition { failures.append(name) }
        }
        if let output { try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true) }
        await settle()
        snapshot("01-ready")

        // Host → guest.
        let sent = "git status --short"
        await send()?.value
        expect(guest.clipboard == sent, "host text reaches guest over ClipboardWire")
        expect(panel.transfers.first?.toGuest == true && panel.transfers.first?.succeeded == true, "send recorded")
        await settle()
        snapshot("02-sent")

        // Guest → host, previewed then confirmed.
        let reply = "ok: 3 files changed\n  M vm/ControlPanel.swift\n  A vm/DemoGuest.swift\n  A vm/PanelSelfTest.swift"
        guest.clipboard = reply
        let confirmed = fetch()
        expect(await waitFor { panel.pending != nil }, "guest text held for preview")
        expect(pasteboard.string(forType: .string) == sent, "host pasteboard untouched before confirmation")
        await settle()
        snapshot("03-preview")
        panel.onConfirm()
        await confirmed?.value
        expect(pasteboard.string(forType: .string) == reply, "confirmed guest text written to host pasteboard")
        await settle()
        snapshot("04-fetched")

        // Guest → host, cancelled.
        guest.clipboard = "rm -rf ~/should-not-arrive"
        let cancelled = fetch()
        expect(await waitFor { panel.pending != nil }, "second fetch previewed")
        panel.onCancel()
        await cancelled?.value
        expect(pasteboard.string(forType: .string) == reply, "cancelled text not written")

        // Guest → host with a hidden bidi override: rejected before preview.
        guest.clipboard = "echo safe\u{202E}txt.hs"
        await fetch()?.value
        expect(panel.pending == nil, "hostile text never previewed")
        expect(pasteboard.string(forType: .string) == reply, "hostile text not written")
        expect(panel.transfers.first?.succeeded == false, "rejection recorded as failure")
        await settle()
        snapshot("05-rejected")

        print(failures.isEmpty ? "PANEL SELFTEST PASS" : "PANEL SELFTEST FAIL: \(failures.joined(separator: ", "))")
        return failures.isEmpty
    }

    private func waitFor(_ condition: () -> Bool) async -> Bool {
        for _ in 0..<100 {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(50))
        }
        return condition()
    }

    private func settle() async {
        try? await Task.sleep(for: .milliseconds(400))
        window.contentView?.layoutSubtreeIfNeeded()
    }

    private func snapshot(_ name: String) {
        guard let output, let view = window.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?
            .write(to: output.appendingPathComponent(name + ".png"))
    }
}
