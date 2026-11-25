#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/scout_bus.py
宿主机 Antigravity（官方桌面端 Gemini）与虚拟机 Claude 之间的 100% 合规纯文件调研总线：
- 不使用任何非官方逆向 API、不抓取 OAuth Token、不注入 UI 自动化脚本（100% 符合 Google 与 Anthropic 官方使用政策）
- 支持虚拟机 Claude 通过 `_dispatch/pending/` (`action: "scout_request"`) 或直接写 `_bus/scout_ask.md` 发起深度文献/发散脑暴请求
- 支持宿主机 Antigravity 一键列出待办 (`scout_bus.py list`)、一键写回回执并自动勾选更新 `_control/01_PROJECT_STATE.md` 首屏收件箱 (`scout_bus.py complete`)
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = Path(os.environ.get("RH_EXCHANGE_ROOT", HUB_DIR.parent))
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"
QUEUE_FILE = HUB_DIR / "_scout_queue.json"


def _load_queue() -> List[Dict[str, Any]]:
    if not QUEUE_FILE.is_file():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_queue(items: List[Dict[str, Any]]) -> None:
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, QUEUE_FILE)


def _update_project_inbox(project_dir: Path, bullet_line: str, remove_pattern: str = "") -> None:
    state_file = project_dir / "_control" / "01_PROJECT_STATE.md"
    if not state_file.is_file():
        return
    text = state_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    if remove_pattern:
        lines = [ln for ln in lines if remove_pattern not in ln]
    inserted = False
    for idx, ln in enumerate(lines):
        if "待汇报与未闭环事项收件箱" in ln:
            insert_pos = idx + 1
            while insert_pos < len(lines) and not lines[insert_pos].strip():
                insert_pos += 1
            if insert_pos < len(lines) and "当前无阻塞告警" in lines[insert_pos]:
                lines[insert_pos] = bullet_line
            else:
                lines.insert(insert_pos, bullet_line)
            inserted = True
            break
    if not inserted:
        lines.insert(0, f"## 🚨 待汇报与未闭环事项收件箱\n{bullet_line}\n")
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, state_file)


def enqueue_request(
    project_dir: Path,
    job_id: str,
    topic: str,
    context_txt: str,
    questions: List[str],
    target_file: str = "01_论文与协议/文献事实卡片.md",
) -> Path:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bus_dir = project_dir / "_bus"
    bus_dir.mkdir(parents=True, exist_ok=True)
    ask_file = bus_dir / "scout_ask.md"

    q_lines = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions)) if questions else "1. （请见背景说明）"
    content = (
        f"# _bus/scout_ask.md —— 宿主机深度文献与发散调研请求单\n\n"
        f"- **课题**：`{project_dir.name}`\n"
        f"- **工单编号**：`{job_id}`\n"
        f"- **发起时间**：{now}\n"
        f"- **调研主题**：{topic}\n"
        f"- **期望交付路径**：`{target_file}` 及 `_bus/scout_reply.md`\n\n"
        f"---\n\n"
        f"## 1. 背景与上下文\n{context_txt or '（无额外背景）'}\n\n"
        f"## 2. 需要宿主机深度检索 / 发散探索的具体问题\n{q_lines}\n"
    )
    tmp = ask_file.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, ask_file)

    queue = [item for item in _load_queue() if not (item.get("project") == project_dir.name and item.get("status") == "PENDING")]
    queue.append({
        "project": project_dir.name,
        "job_id": job_id,
        "topic": topic,
        "target_file": target_file,
        "ask_file": str(ask_file),
        "created_at": now,
        "status": "PENDING",
    })
    _save_queue(queue)

    _update_project_inbox(
        project_dir,
        f"- [ ] **⏳ 待宿主机深度调研（`{job_id}` · {topic}）**：请求已写入 `_bus/scout_ask.md`，待 PI 在宿主机 Antigravity 运行「处理调研单」。",
        remove_pattern="待宿主机深度调研",
    )
    return ask_file


def list_requests() -> List[Dict[str, Any]]:
    queue = _load_queue()
    # Also auto-discover any _bus/scout_ask.md written directly by Claude
    known_projects = {item.get("project") for item in queue if item.get("status") == "PENDING"}
    if PROJECTS_DIR.is_dir():
        for p in sorted(PROJECTS_DIR.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            ask_p = p / "_bus" / "scout_ask.md"
            reply_p = p / "_bus" / "scout_reply.md"
            if ask_p.is_file() and p.name not in known_projects:
                if not reply_p.is_file() or ask_p.stat().st_mtime > reply_p.stat().st_mtime:
                    queue.append({
                        "project": p.name,
                        "job_id": "manual_scout",
                        "topic": "直接写入的 _bus/scout_ask.md 调研请求",
                        "target_file": "_bus/scout_reply.md",
                        "ask_file": str(ask_p),
                        "created_at": datetime.datetime.fromtimestamp(ask_p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "PENDING",
                    })
    _save_queue(queue)
    return [item for item in queue if item.get("status") == "PENDING"]


def complete_request(project_name: str, summary: str, reply_content: str, deliverable_rel: str = "") -> Path:
    project_dir = PROJECTS_DIR / project_name
    if not project_dir.is_dir():
        matches = [p for p in PROJECTS_DIR.iterdir() if p.is_dir() and project_name.lower() in p.name.lower()]
        if not matches:
            raise FileNotFoundError(f"未找到课题：{project_name}")
        project_dir = matches[0]

    now = datetime.datetime.now()
    ts_human = now.strftime("%Y-%m-%d %H:%M:%S")
    ts_slug = now.strftime("%Y%m%d-%H%M%S")

    bus_dir = project_dir / "_bus"
    archive_dir = bus_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    reply_file = bus_dir / "scout_reply.md"

    header = (
        f"# _bus/scout_reply.md —— 宿主机深度文献与发散调研回执\n\n"
        f"> **课题**：`{project_dir.name}` | **完成时间**：{ts_human}\n"
        f"> **核心摘要**：{summary}\n"
    )
    if deliverable_rel:
        header += f"> **同步产出文件**：`{deliverable_rel}`\n"
    full_text = f"{header}\n---\n\n{reply_content.strip()}\n"

    tmp = reply_file.with_suffix(".tmp")
    tmp.write_text(full_text, encoding="utf-8")
    os.replace(tmp, reply_file)

    (archive_dir / f"{ts_slug}_scout_reply.md").write_text(full_text, encoding="utf-8")
    ask_file = bus_dir / "scout_ask.md"
    if ask_file.is_file():
        (archive_dir / f"{ts_slug}_scout_ask.md").write_text(ask_file.read_text(encoding="utf-8"), encoding="utf-8")

    queue = _load_queue()
    for item in queue:
        if item.get("project") == project_dir.name and item.get("status") == "PENDING":
            item["status"] = "DONE"
            item["completed_at"] = ts_human
            item["summary"] = summary
    _save_queue(queue)

    deliv_note = f" 及 `{deliverable_rel}`" if deliverable_rel else ""
    _update_project_inbox(
        project_dir,
        f"- [ ] **✅ 宿主机深度调研回执已就绪（{ts_human}）**：{summary}。请立即读取 `_bus/scout_reply.md`{deliv_note} 并用大白话向 PI 汇报！",
        remove_pattern="宿主机深度调研",
    )
    return reply_file


def main() -> int:
    parser = argparse.ArgumentParser(description="宿主机 Antigravity 与课题舱之间的纯文件调研总线")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有待宿主机 Antigravity 处理的调研请求 (_bus/scout_ask.md)")

    p_enq = sub.add_parser("enqueue", help="手动或由脚本登记一条调研请求")
    p_enq.add_argument("--project", required=True)
    p_enq.add_argument("--topic", required=True)
    p_enq.add_argument("--context", default="")
    p_enq.add_argument("--question", action="append", default=[])
    p_enq.add_argument("--target-file", default="01_论文与协议/文献事实卡片.md")

    p_done = sub.add_parser("complete", help="将宿主机调研结果写回 _bus/scout_reply.md 并自动更新 01_PROJECT_STATE.md 首屏收件箱")
    p_done.add_argument("--project", required=True)
    p_done.add_argument("--summary", required=True, help="一句话完成摘要（写入 01_PROJECT_STATE.md 首屏提醒）")
    p_done.add_argument("--reply-file", help="已写好的 Markdown 回复文件路径（与 --reply-text 二选一）")
    p_done.add_argument("--reply-text", help="直接传入 Markdown 回复正文")
    p_done.add_argument("--deliverable", default="", help="课题内关联交付物相对路径（例如 01_论文与协议/文献事实卡片_2026-11-25.md）")

    args = parser.parse_args()
    if args.cmd == "list":
        pending = list_requests()
        print(json.dumps(pending, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "enqueue":
        p_dir = PROJECTS_DIR / args.project
        out = enqueue_request(p_dir, "manual_scout", args.topic, args.context, args.question, args.target_file)
        print(f"ENQUEUED: {out}")
        return 0
    if args.cmd == "complete":
        if args.reply_file:
            body = Path(args.reply_file).read_text(encoding="utf-8")
        elif args.reply_text:
            body = args.reply_text
        else:
            raise ValueError("必须提供 --reply-file 或 --reply-text")
        out = complete_request(args.project, args.summary, body, args.deliverable)
        print(f"COMPLETED: {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
