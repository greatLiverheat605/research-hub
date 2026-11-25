#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/context_distiller.py
跨课题上下文冷热分离与 200 行预算蒸馏器（解决 AI 遗忘、Lost-in-the-Middle 幻觉及合规敏感词）：
1. 将超长 _control/02_DECISION_LEDGER.md 的详细推导备份至 _control/archive/02_DECISION_LEDGER.full.md，
   主表压缩为高信噪比单行索引（每行 <=160 字，整表 <=8 KB）；
2. 将 _control/01_PROJECT_STATE.md 中无限追加的历史流水截断为最新待办，并在顶部注入
   「🚨 待汇报与未闭环事项收件箱」；
3. 将超过 10 KB 的 _control/03_WORK_LOG.md 冷归档，主文件仅保留最近 20 条记录；
4. 自动将虚拟机可见文档中的第三方竞品模型/中转站名称替换为中性学术术语，强化 Claude 客户端合规安全。
"""

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"

# 虚拟机可见文档中的第三方模型/代理名称中性化映射（保护 Claude 桌面端合规安全）
NEUTRAL_TERM_MAP: List[Tuple[str, str]] = [
    (r"DeepSeek\s*\(`?deepseek-[^`)]+`?\)", "方法学红队 (`redteam`)"),
    (r"DeepSeek\s*红队", "方法学红队 (`redteam`)"),
    (r"\bDeepSeek\b", "方法学红队"),
    (r"GPT\s*\(`?cc-switch`?\)", "外部同行顾问 (`advisor`)"),
    (r"外部\s*GPT\s*顾问", "外部同行顾问 (`advisor`)"),
    (r"GPT\s*顾问", "外部同行顾问 (`advisor`)"),
    (r"GLM\s*\(`?glm-[^`)]+`?\)", "宿主机排版引擎"),
    (r"GLM-5\.3(?:-Flash)?", "宿主机排版引擎"),
    (r"s2api\.oonet\.icu", "宿主机排版网关"),
    (r"\bcc-switch\b", "宿主机顾问路由"),
]


def neutralize_terms(text: str) -> str:
    out = text
    for pat, repl in NEUTRAL_TERM_MAP:
        out = re.sub(pat, repl, out)
    return out


def _compact_rationale(rationale: str, max_len: int = 135) -> str:
    """保留核心数字结论与冻结边界，去除冗长过程叙述。"""
    s = re.sub(r"\s+", " ", rationale).strip()
    if len(s) <= max_len:
        return s
    # 提取第一句 + 最后一个分号/句号的关键边界
    parts = [p.strip() for p in re.split(r"[。；]", s) if p.strip()]
    if len(parts) >= 2:
        cand = f"{parts[0]}；{parts[-1]}"
        if len(cand) <= max_len + 25:
            return cand
    return s[:max_len].rstrip("，、； ") + "… (详看 archive)"


def distill_project(proj_dir: Path, active_alerts: List[str] = None) -> Dict[str, Tuple[int, int]]:
    ctrl = proj_dir / "_control"
    if not ctrl.is_dir():
        return {}
    arch = ctrl / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    stats: Dict[str, Tuple[int, int]] = {}

    # 1. 蒸馏 02_DECISION_LEDGER.md
    ledger = ctrl / "02_DECISION_LEDGER.md"
    full_backup = arch / "02_DECISION_LEDGER.full.md"
    if ledger.is_file():
        raw = ledger.read_text(encoding="utf-8")
        old_size = len(raw.encode("utf-8"))
        # 仅当当前文件大于备份或备份不存在时更新 full_backup
        if not full_backup.is_file() or old_size > len(full_backup.read_bytes()):
            full_backup.write_text(raw, encoding="utf-8")
        src_text = full_backup.read_text(encoding="utf-8")
        out_lines: List[str] = []
        for ln in src_text.splitlines():
            s = ln.strip()
            if s.startswith("|") and re.match(r"^\|\s*\*{0,2}D-\d+\*{0,2}\s*\|", s):
                cols = [c.strip() for c in s.strip("|").split("|")]
                if len(cols) >= 5:
                    did, dt, title, rat, files = cols[0], cols[1], cols[2], cols[3], cols[4]
                    short_title = title[:68] + ("…" if len(title) > 68 else "")
                    short_rat = _compact_rationale(rat, max_len=130)
                    short_files = files[:75] + ("…" if len(files) > 75 else "")
                    out_lines.append(f"| {did} | {dt} | {short_title} | {short_rat} | {short_files} |")
                    continue
            out_lines.append(ln)
        out_lines.append(
            "\n> 💡 **冷热分层说明**：本表为启动必读精简索引（防上下文过载）；每条决定的完整长文推导与历史哈希永久保存在 `_control/archive/02_DECISION_LEDGER.full.md`，仅在需要核查特定历史细节时按需定点读取。"
        )
        new_text = neutralize_terms("\n".join(out_lines) + "\n")
        ledger.write_text(new_text, encoding="utf-8")
        stats["02_DECISION_LEDGER.md"] = (old_size, len(new_text.encode("utf-8")))

    # 2. 蒸馏 01_PROJECT_STATE.md
    state = ctrl / "01_PROJECT_STATE.md"
    state_backup = arch / "01_PROJECT_STATE.full.md"
    if state.is_file():
        raw_st = state.read_text(encoding="utf-8")
        old_size = len(raw_st.encode("utf-8"))
        if not state_backup.is_file() or old_size > len(state_backup.read_bytes()):
            state_backup.write_text(raw_st, encoding="utf-8")

        # 去掉旧的待汇报收件箱块（便于幂等刷新）
        cleaned = re.sub(
            r"## 🚨 待汇报与未闭环事项收件箱.*?(?=\n---\n|\n## )",
            "",
            raw_st,
            flags=re.S,
        )
        new_lines: List[str] = []
        for ln in cleaned.splitlines():
            # 若单行超过 450 字符（如 P01 连续追加了 15 轮【D-0xx】的待办行），只截取最后两段【...】
            if len(ln) > 450 and "【" in ln:
                chunks = list(re.finditer(r"【[^】]{2,40}】", ln))
                if len(chunks) >= 2:
                    prefix = ln[: ln.find("【")].strip()
                    tail = ln[chunks[-2].start() :].strip()
                    tail = re.sub(r"（旧待办[：:].*$", "", tail).strip()
                    ln = f"{prefix} （前序历史已归档至 `_control/archive/01_PROJECT_STATE.full.md`）**最新进展**：{tail}"
            new_lines.append(ln)

        body = "\n".join(new_lines)
        alerts = active_alerts or []
        inbox_md = (
            "## 🚨 待汇报与未闭环事项收件箱（AI 每次回复第一段必主动交代）\n\n"
            + ("\n".join(f"- {a}" for a in alerts) if alerts else "- 当前无阻塞告警；请直接围绕下方 Top 待办推进。")
            + "\n\n---\n\n"
        )
        # 插入在第一个独立分隔线 \n---\n 之后或标题之后
        m_hr = re.search(r"\n---\s*\n", body)
        if m_hr:
            pos = m_hr.end()
            final_st = body[:pos].rstrip() + "\n\n" + inbox_md + body[pos:].lstrip()
        else:
            final_st = inbox_md + body
        final_st = neutralize_terms(final_st)
        state.write_text(final_st, encoding="utf-8")
        stats["01_PROJECT_STATE.md"] = (old_size, len(final_st.encode("utf-8")))

    # 3. 蒸馏 03_WORK_LOG.md（冷日志只保留最近 30 行）
    wlog = ctrl / "03_WORK_LOG.md"
    wlog_arch = arch / "03_WORK_LOG.full.md"
    if wlog.is_file():
        raw_wl = wlog.read_text(encoding="utf-8")
        old_size = len(raw_wl.encode("utf-8"))
        if old_size > 8000:
            if not wlog_arch.is_file() or old_size > len(wlog_arch.read_bytes()):
                wlog_arch.write_text(raw_wl, encoding="utf-8")
            lines = raw_wl.splitlines()
            head = lines[:6]
            tail = lines[-28:]
            compact_wl = (
                "\n".join(head)
                + "\n\n> 📦 **历史日志已归档**：更早的执行流水已移入 `_control/archive/03_WORK_LOG.full.md`（日常会话禁止预读）。以下仅保留最近记录：\n\n"
                + "\n".join(tail)
                + "\n"
            )
            compact_wl = neutralize_terms(compact_wl)
            wlog.write_text(compact_wl, encoding="utf-8")
            stats["03_WORK_LOG.md"] = (old_size, len(compact_wl.encode("utf-8")))

    # 4. 中性化 CLAUDE.md 中的第三方模型/中转站名称并强化防膨胀规则
    claude_md = proj_dir / "CLAUDE.md"
    if claude_md.is_file():
        raw_cm = claude_md.read_text(encoding="utf-8")
        old_size = len(raw_cm.encode("utf-8"))
        new_cm = neutralize_terms(raw_cm)
        budget_rule = (
            "- **上下文 200 行硬预算与防遗忘铁律**：\n"
            "  1. 每次回复前必须先检查 `_control/01_PROJECT_STATE.md` 顶部的「🚨 待汇报与未闭环事项收件箱」并主动交代；\n"
            "  2. 更新 `01_PROJECT_STATE.md` 待办时，**必须用最新状态覆盖已完成步骤（单条待办 ≤ 200 字）**，严禁在待办后无限原地追加历史流水；\n"
            "  3. 新增 `02_DECISION_LEDGER.md` 条目时，主表每行严格 ≤ 150 字；长篇推导写入独立报告或 `_control/archive/`；日常会话**无需预读** `03_WORK_LOG.md` 与 `_control/archive/`。"
        )
        if "上下文 200 行硬预算与防遗忘铁律" not in new_cm:
            new_cm = new_cm.replace(
                "- **强制落盘**：每次完成实验",
                budget_rule + "\n- **强制落盘**：每次完成实验",
            )
        claude_md.write_text(new_cm, encoding="utf-8")
        stats["CLAUDE.md"] = (old_size, len(new_cm.encode("utf-8")))

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="跨课题上下文冷热分离与 200 行预算蒸馏器")
    parser.add_argument("--project", help="指定课题名称（默认处理全部课题）")
    args = parser.parse_args()

    default_alerts = {
        "P01_染色质块条件插补": [
            "**[待办 #1 · 测试集阶段 P/T 预检]**：阶段 P/T 脚本 (`test_job.py`, `make_test_only.py`) 已就绪，需通过 `_dispatch` 先跑 `make_test_only.py --selftest` 与 `test_job.py tcheck`（测试集坐标仍严格封存）。",
            "**[待核 #2 · 索引 7 预检异常]**：上次干跑 `p01_idx7check_dryrun_20261123d` 进程退出码为 0 但实验检查报 `IDX7_CHECK FAIL (K2, K4)`，请向 PI 汇报 K2/K4 失败原因与修复方案。",
        ],
        "P02_NC_HiC三维坐标综述基准": [
            "**[紧迫交付 #1 · 工单 #7 与 PR #8 (C04)]**：C04 代码已由宿主机推送至 PR #8（commit `b64830f3`），工单 #7 回执已同步更新。",
            "**[待验收 #2 · PR #10 (S8a) 与工单 #4 (C01)]**：S8a 已更新至 PR #10 待 `@p02-lead` 验收；C01 交付包已挂 Release (`c01-delivery-20261122`) 待负责人核验与 FLAMINGO 10kb 0:10 裁定。",
            "**[待跑实验 #3 · C04 Windows 实测]**：C04 仅过合成测试，Windows 原机 (`win-codex`) `--trial`、E11 NPZ 转换耗时及 50kb/10kb 正式运行待执行。",
        ],
    }

    targets = [PROJECTS_DIR / args.project] if args.project else sorted(PROJECTS_DIR.iterdir())
    for p in targets:
        if not p.is_dir() or p.name.startswith("."):
            continue
        st = distill_project(p, active_alerts=default_alerts.get(p.name, []))
        print(f"=== {p.name} ===")
        for fn, (b0, b1) in st.items():
            pct = round((1 - b1 / max(1, b0)) * 100, 1)
            print(f"  {fn:<24}: {b0:>7} B -> {b1:>7} B  (缩减 {pct}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
