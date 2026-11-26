import SwiftUI
import AppKit

// Host-side control sidebar. It only displays coordinator state and forwards
// button presses; it has no network, file, or VM access of its own.

enum PanelStage: Equatable {
    case preparing, gatewayStarting, gatewayReady, installing(Double), running, stopped, gatewayStopped
    case error(String), demo
}

@MainActor
final class ControlPanelModel: ObservableObject {
    enum CheckState { case pending, pass, fail, info }

    struct Check: Identifiable {
        let id: String
        let title: String
        var detail: String
        var state: CheckState
    }

    struct Transfer: Identifiable {
        let id = UUID()
        let toGuest: Bool
        let summary: String
        let succeeded: Bool
        let time = Date()
    }

    struct PendingText {
        let text: String
        let characters: Int
        let lines: Int
    }

    @Published var stage: PanelStage = .preparing
    @Published var message = "正在准备专用环境…"
    @Published var transferMessage = "在虚拟机中打开 ClaudeShield Clipboard 后即可传输文字"
    @Published var busy = false
    @Published var pending: PendingText?
    @Published private(set) var transfers: [Transfer] = []
    @Published private(set) var checks: [Check] = [
        .init(id: "vsock", title: "宿主不接受虚拟机连接", detail: "VSOCK 只由宿主主动发起，未注册任何监听", state: .pass),
        .init(id: "filter", title: "取回文字先过滤再确认", detail: "拒绝控制符、倒序与零宽字符；写入前需预览", state: .pass),
        .init(id: "nic", title: "macOS 仅私有网卡", detail: "等待虚拟机配置", state: .pending),
        .init(id: "share", title: "共享目录只读且无链接", detail: "等待启动前检查", state: .pending),
        .init(id: "console", title: "网关日志限额 16 MiB", detail: "等待网关配置", state: .pending),
        .init(id: "gateway", title: "网关自检与出站拒绝", detail: "等待网关启动", state: .pending)
    ]

    var onSend: () -> Void = {}
    var onFetch: () -> Void = {}
    var onConfirm: () -> Void = {}
    var onCancel: () -> Void = {}

    var canTransfer: Bool { !busy && (stage == .running || stage == .demo) }

    func setCheck(_ id: String, _ state: CheckState, _ detail: String) {
        guard let index = checks.firstIndex(where: { $0.id == id }) else { return }
        checks[index].state = state
        checks[index].detail = detail
    }

    func record(toGuest: Bool, summary: String, succeeded: Bool) {
        transfers.insert(.init(toGuest: toGuest, summary: summary, succeeded: succeeded), at: 0)
        if transfers.count > 20 { transfers.removeLast(transfers.count - 20) }
    }

    // Mirrors the states the coordinator already writes to status.json.
    func apply(state: String, detail: String) {
        switch state {
        case "gateway-starting": stage = .gatewayStarting
        case "gateway-ready": stage = .gatewayReady
        case "installing": stage = .installing(Double(detail) ?? 0)
        case "installed": stage = .installing(1)
        case "running": stage = .running
        case "stopped": stage = .stopped
        case "gateway-stopped": stage = .gatewayStopped
        case "error": stage = .error(detail)
        default: break
        }
    }
}

// MARK: - Palette

private enum Tone {
    case good, busy, idle, bad, demo

    var color: Color {
        switch self {
        case .good: return Color(nsColor: .systemGreen)
        case .busy: return Color(nsColor: .systemOrange)
        case .idle: return Color(nsColor: .tertiaryLabelColor)
        case .bad: return Color(nsColor: .systemRed)
        case .demo: return Color(nsColor: .systemPurple)
        }
    }
}

private let accent = Color(red: 0.16, green: 0.55, blue: 0.52)

// MARK: - Views

struct ControlPanelView: View {
    @ObservedObject var model: ControlPanelModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.6)
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    section("环境") { machines }
                    section("文字传输") { transfer }
                    section("隔离检查") { checkList }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 18)
            }
            Divider().opacity(0.6)
            footer
        }
        .frame(width: 320)
        .background(SidebarMaterial())
    }

    private var header: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(LinearGradient(colors: [accent, accent.opacity(0.72)],
                                         startPoint: .topLeading, endPoint: .bottomTrailing))
                Image(systemName: "shield.lefthalf.filled")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text("ClaudeShield").font(.system(size: 15, weight: .semibold))
                Text("隔离开发环境").font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Pill(text: stageLabel.text, tone: stageLabel.tone)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }

    private var stageLabel: (text: String, tone: Tone) {
        switch model.stage {
        case .preparing: return ("准备中", .busy)
        case .gatewayStarting: return ("网关自检", .busy)
        case .gatewayReady: return ("网关就绪", .busy)
        case .installing: return ("安装中", .busy)
        case .running: return ("运行中", .good)
        case .stopped: return ("已关机", .idle)
        case .gatewayStopped: return ("网关停止", .bad)
        case .error: return ("未就绪", .bad)
        case .demo: return ("演示", .demo)
        }
    }

    // MARK: Machines

    private var machines: some View {
        Card {
            MachineRow(symbol: "server.rack", name: "Linux 网关",
                       detail: "私有链路 · 出站默认拒绝", status: gatewayStatus)
            Divider().padding(.leading, 44)
            MachineRow(symbol: "desktopcomputer", name: "专用 macOS",
                       detail: "仅私有网卡 · VSOCK 通道", status: macStatus)
            if case .installing(let progress) = model.stage {
                ProgressView(value: progress)
                    .tint(accent)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
            }
        }
    }

    private var gatewayStatus: (String, Tone) {
        switch model.stage {
        case .preparing: return ("准备中", .idle)
        case .gatewayStarting: return ("自检中", .busy)
        case .gatewayReady, .installing, .running, .stopped: return ("已就绪", .good)
        case .gatewayStopped: return ("已停止", .bad)
        case .error: return ("异常", .bad)
        case .demo: return ("未启动", .demo)
        }
    }

    private var macStatus: (String, Tone) {
        switch model.stage {
        case .installing(let progress): return ("安装 \(Int(progress * 100))%", .busy)
        case .running: return ("运行中", .good)
        case .stopped: return ("已关机", .idle)
        case .error, .gatewayStopped: return ("未运行", .bad)
        case .demo: return ("模拟客机", .demo)
        default: return ("等待网关", .idle)
        }
    }

    // MARK: Transfer

    private var transfer: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                TransferButton(symbol: "arrow.up.right", title: "发送到虚拟机",
                               subtitle: "使用宿主剪贴板", enabled: model.canTransfer, action: model.onSend)
                TransferButton(symbol: "arrow.down.left", title: "取回到宿主",
                               subtitle: "预览后写入", enabled: model.canTransfer, action: model.onFetch)
            }
            if let pending = model.pending {
                PreviewCard(pending: pending, confirm: model.onConfirm, cancel: model.onCancel)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            HStack(spacing: 6) {
                if model.busy && model.pending == nil {
                    ProgressView().controlSize(.small)
                }
                Text(model.transferMessage)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.transfers.isEmpty {
                Card {
                    ForEach(Array(model.transfers.prefix(5).enumerated()), id: \.element.id) { index, item in
                        if index > 0 { Divider().padding(.leading, 36) }
                        TransferRow(item: item)
                    }
                }
            }
        }
        .animation(.easeOut(duration: 0.18), value: model.pending?.text)
    }

    // MARK: Checks

    private var checkList: some View {
        Card {
            ForEach(Array(model.checks.enumerated()), id: \.element.id) { index, check in
                if index > 0 { Divider().padding(.leading, 36) }
                CheckRow(check: check)
            }
        }
    }

    private var footer: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: footerSymbol)
                .font(.system(size: 12))
                .foregroundStyle(footerTone.color)
                .padding(.top, 1)
            Text(model.message)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private var footerTone: Tone { stageLabel.tone }
    private var footerSymbol: String {
        switch model.stage {
        case .error, .gatewayStopped: return "exclamationmark.triangle.fill"
        case .running: return "checkmark.circle.fill"
        default: return "info.circle.fill"
        }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
                .padding(.leading, 2)
            content()
        }
    }
}

private struct Card<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor).opacity(0.75)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Color(nsColor: .separatorColor).opacity(0.6), lineWidth: 0.5))
    }
}

private struct Pill: View {
    let text: String
    let tone: Tone

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(tone.color).frame(width: 6, height: 6)
            Text(text).font(.system(size: 11, weight: .medium))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(Capsule().fill(tone.color.opacity(0.13)))
        .foregroundStyle(tone == .idle ? Color.secondary : tone.color)
    }
}

private struct MachineRow: View {
    let symbol: String
    let name: String
    let detail: String
    let status: (String, Tone)

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 14))
                .foregroundStyle(accent)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.system(size: 13, weight: .medium))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 6)
            Pill(text: status.0, tone: status.1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}

private struct TransferButton: View {
    let symbol: String
    let title: String
    let subtitle: String
    let enabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 8) {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(enabled ? .white : Color.secondary)
                    .frame(width: 26, height: 26)
                    .background(Circle().fill(enabled ? accent : Color(nsColor: .quaternaryLabelColor)))
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.system(size: 12, weight: .semibold))
                    Text(subtitle).font(.system(size: 10.5)).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(11)
            .contentShape(Rectangle())
        }
        .buttonStyle(TileStyle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.6)
    }
}

// Press feedback without view-local state (bare swiftc builds have no SwiftUI macro plugin).
private struct TileStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(configuration.isPressed ? accent.opacity(0.12) : Color(nsColor: .controlBackgroundColor).opacity(0.75)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(configuration.isPressed ? accent.opacity(0.6) : Color(nsColor: .separatorColor).opacity(0.6),
                              lineWidth: configuration.isPressed ? 1 : 0.5))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

private struct PreviewCard: View {
    let pending: ControlPanelModel.PendingText
    let confirm: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "eye.fill").foregroundStyle(accent)
                Text("确认写入宿主剪贴板").font(.system(size: 12, weight: .semibold))
                Spacer()
                Text("\(pending.characters) 字符 · \(pending.lines) 行")
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            ScrollView {
                Text(pending.text)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            .frame(maxHeight: 150)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6)
                .strokeBorder(Color(nsColor: .separatorColor).opacity(0.6), lineWidth: 0.5))
            Text("文字来自虚拟机。粘贴进终端前，请确认没有你不认识的命令。")
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("取消", action: cancel).keyboardShortcut(.cancelAction)
                Button("写入剪贴板", action: confirm)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .tint(accent)
            }
            .controlSize(.small)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(accent.opacity(0.07)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(accent.opacity(0.45), lineWidth: 1))
    }
}

private struct TransferRow: View {
    let item: ControlPanelModel.Transfer

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.succeeded
                  ? (item.toGuest ? "arrow.up.right.circle.fill" : "arrow.down.left.circle.fill")
                  : "xmark.circle.fill")
                .font(.system(size: 15))
                .foregroundStyle(item.succeeded ? accent : Tone.bad.color)
                .frame(width: 18)
            Text(item.summary)
                .font(.system(size: 11.5))
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 6)
            Text(item.time, style: .time)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

private struct CheckRow: View {
    let check: ControlPanelModel.Check

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 13))
                .foregroundStyle(color)
                .frame(width: 18)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(check.title).font(.system(size: 12, weight: .medium))
                Text(check.detail)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    private var symbol: String {
        switch check.state {
        case .pass: return "checkmark.seal.fill"
        case .fail: return "xmark.octagon.fill"
        case .pending: return "clock"
        case .info: return "info.circle"
        }
    }

    private var color: Color {
        switch check.state {
        case .pass: return Tone.good.color
        case .fail: return Tone.bad.color
        case .pending: return Tone.idle.color
        case .info: return Color(nsColor: .systemBlue)
        }
    }
}

private struct SidebarMaterial: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .sidebar
        view.blendingMode = .behindWindow
        view.state = .followsWindowActiveState
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

// Shown in place of the VM display when no VM is configured (demo/self-test).
struct DemoDisplayView: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.09, green: 0.11, blue: 0.13), Color(red: 0.05, green: 0.06, blue: 0.08)],
                           startPoint: .top, endPoint: .bottom)
            VStack(spacing: 14) {
                Image(systemName: "display")
                    .font(.system(size: 40, weight: .light))
                    .foregroundStyle(.white.opacity(0.55))
                Text("演示模式 · 未启动虚拟机")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.9))
                Text("文字通道连接到进程内的模拟客机，使用与真实 VSOCK 相同的帧编码与校验。\n宿主剪贴板替换为独立的演示剪贴板，不影响你正在用的内容。")
                    .font(.system(size: 12))
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.white.opacity(0.55))
                    .frame(maxWidth: 420)
            }
        }
    }
}
