#!/usr/bin/env python3
"""
00_全局中枢_Hub/dashboard/serve.py
全局多课题科研总控大屏本地服务（零第三方依赖，兼容 macOS Python 3.9+）。

特性与安全边界：
- 仅监听 127.0.0.1（默认端口 8765），校验 Host / Origin 头防 DNS 重绑定与跨站调用；
- 100% 零后台扫描、零额外 AI Token 消耗：结构化解析各课题 _control/ 与实验模块供前端图表/卡片展示；
- 提供 /api/checks 六维自动化健康与学术门禁检查接口；
- 严格脱敏：/api/overview 仅展示模型名与密钥就绪状态，绝不返回任何 API Key 明文。
"""

import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DASHBOARD_DIR = Path(__file__).resolve().parent
HUB_DIR = DASHBOARD_DIR.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"
SKILLS_DIR = HUB_DIR / "skills"
GEMINI_SKILLS_DIR = Path.home() / ".gemini" / "config" / "skills"

sys.path.insert(0, str(HUB_DIR))
sys.path.insert(0, str(HUB_DIR / "scripts"))
import check_manuscript  # noqa: E402
import consult  # noqa: E402


SKIP_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints", "冻结存档-20261117", "node_modules"}
ALLOWED_EXTS = {".md", ".py", ".ipynb", ".json", ".toml", ".pptx", ".pdf"}

MODULE_DEFS = [
    ("A", "A_T3T4管线与采样验收", "T3/T4 管线与采样验收", "#0072B2"),
    ("B", "B_分段统计与噪声误差校准", "分段统计与噪声校准", "#529286"),
    ("C", "C_图扩散与锚点对照实验", "图扩散与锚点对照", "#E69F00"),
    ("D", "D_外部发表方法与数据对标", "外部发表方法对标", "#CC79A7"),
    ("E", "E_历史多智能体互审记录", "多智能体互审复核", "#64748B"),
]


def _safe_read_text(path: Path, max_bytes: int = 512 * 1024) -> str:
    """Read UTF-8 text safely with size guard."""
    if not path.is_file():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        head = data[:max_bytes].decode("utf-8", errors="replace")
        return head + f"\n\n...（文件过大，仅展示前 {max_bytes // 1024} KB）"
    return data.decode("utf-8", errors="replace")


def _is_within(child: Path, parent: Path) -> bool:
    """Return True if resolved child is located inside resolved parent."""
    try:
        return os.path.commonpath([str(child.resolve()), str(parent.resolve())]) == str(parent.resolve())
    except ValueError:
        return False


def _validate_project_name(name: str) -> Optional[Path]:
    """Validate project directory name and return resolved path inside PROJECTS_DIR."""
    cleaned = name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
        return None
    target = PROJECTS_DIR / cleaned
    if target.is_dir() and _is_within(target, PROJECTS_DIR):
        return target
    return None


def _clean_md_inline(text: str) -> str:
    """Strip markdown bold/code wrappers for concise card display."""
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    return cleaned.strip()


def _parse_milestones(state_md: str) -> List[Dict[str, str]]:
    """Parse milestone table rows from 01_PROJECT_STATE.md."""
    items: List[Dict[str, str]] = []
    for line in state_md.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 3:
            continue
        if cells[0] in ("模块 / 实验", "---") or set(cells[0]) <= {"-", ":"}:
            continue
        raw_status = cells[1]
        if "✅" in raw_status or "锁定" in raw_status or "跑完" in raw_status or "完成" in raw_status:
            badge = "done"
        elif "⚠️" in raw_status or "纠偏" in raw_status:
            badge = "warn"
        elif "⏸" in raw_status or "未完成" in raw_status:
            badge = "paused"
        else:
            badge = "progress"
        items.append({
            "title": _clean_md_inline(cells[0]),
            "status_text": _clean_md_inline(raw_status),
            "status_badge": badge,
            "summary": _clean_md_inline(cells[2]),
        })
    return items


def _parse_todos(state_md: str) -> List[Dict[str, str]]:
    """Parse Top N numbered priority tasks from 01_PROJECT_STATE.md."""
    todos: List[Dict[str, str]] = []
    in_todos = False
    current: Optional[Dict[str, str]] = None

    for line in state_md.splitlines():
        if line.strip().startswith("##") and ("待推进" in line or "Top" in line or "阻塞" in line):
            in_todos = True
            continue
        if in_todos and line.strip().startswith("## "):
            break
        if not in_todos:
            continue

        m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m:
            if current:
                todos.append(current)
            raw_title = _clean_md_inline(m.group(2))
            prio = f"P{m.group(1)}"
            pm = re.match(r"【([^】]+)】\s*(.*)", raw_title)
            if pm:
                prio = pm.group(1)
                raw_title = pm.group(2)
            current = {"priority": prio, "title": raw_title, "details": []}
        elif current and line.strip().startswith("-"):
            current["details"].append(_clean_md_inline(line.strip().lstrip("- ")))

    if current:
        todos.append(current)
    return [
        {"priority": t["priority"], "title": t["title"], "detail": "；".join(t["details"])}
        for t in todos
    ]


def _parse_decisions(ledger_md: str) -> List[Dict[str, str]]:
    """Parse decision rows from 02_DECISION_LEDGER.md."""
    rows: List[Dict[str, str]] = []
    for line in ledger_md.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 5:
            continue
        did = _clean_md_inline(cells[0])
        if not re.match(r"^D-\d+$", did):
            continue
        rows.append({
            "id": did,
            "date": _clean_md_inline(cells[1]),
            "title": _clean_md_inline(cells[2]),
            "rationale": _clean_md_inline(cells[3]),
            "file_ref": _clean_md_inline(cells[4]),
        })
    return rows


def _parse_redlines(contract_md: str) -> Dict[str, Any]:
    """Extract key quantitative thresholds and no-claim pills from 00_CONTRACT.md."""
    noclaims: List[str] = []
    for line in contract_md.splitlines():
        s = line.strip()
        if s.startswith("- ❌") or s.startswith("- **严禁"):
            noclaims.append(_clean_md_inline(s.lstrip("- ❌").strip()))
        elif s.startswith("-") and "不宣称" in s:
            noclaims.append(_clean_md_inline(s.lstrip("- ").strip()))

    banned_pills = [
        "检出结构性非高斯",
        "恢复真实无噪声构象",
        "解释天然缺失机制",
        "胜过所有 σ",
        "提前拆封真实测试集",
        "同族分段作公平对手",
    ]
    thresholds = [
        {"label": "数据划分 (Train/Cal/Test)", "value": "2,922 / 974 / 975"},
        {"label": "块长与抽样预算", "value": "连续 8 位点 · 256 次采样"},
        {"label": "Bootstrap 检验", "value": "9,999 次配对 trace 单侧 95%"},
        {"label": "主胜出四重门禁", "value": "双对手 GVS 差<0 且 ES 恶化<2%"},
    ]
    return {"thresholds": thresholds, "noclaims": noclaims[:8], "banned_pills": banned_pills}


def _compute_module_stats(proj_dir: Path) -> List[Dict[str, Any]]:
    """Count experiments and files across 02_实验与验证/A..E categories."""
    exp_root = proj_dir / "02_实验与验证"
    stats: List[Dict[str, Any]] = []
    for code, dir_name, short_name, color in MODULE_DEFS:
        mod_dir = exp_root / dir_name
        if not mod_dir.is_dir():
            continue
        entries = [e for e in mod_dir.iterdir() if not e.name.startswith(".")]
        md_count = 0
        for e in entries:
            try:
                resolved = e.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.suffix.lower() == ".md":
                md_count += 1
            elif resolved.is_dir():
                for root_s, _, files in os.walk(str(resolved)):
                    if any(sk in root_s for sk in SKIP_DIRS):
                        continue
                    md_count += sum(1 for fn in files if fn.lower().endswith(".md"))
        stats.append({
            "code": code,
            "dir_name": dir_name,
            "short_name": short_name,
            "color": color,
            "exp_count": len(entries),
            "md_count": md_count,
        })
    return stats


def _parse_skill_index(skill_index_md: str) -> List[Dict[str, str]]:
    """Parse SKILL_INDEX.md table into structured skill cards."""
    skills: List[Dict[str, str]] = []
    for line in skill_index_md.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 4 or cells[0] in ("类别", "---") or set(cells[0]) <= {"-", ":"}:
            continue
        skills.append({
            "category": _clean_md_inline(cells[0]),
            "name": _clean_md_inline(cells[1]),
            "trigger": _clean_md_inline(cells[2]),
            "path": _clean_md_inline(cells[3]),
        })
    return skills


def _summarize_project(proj_dir: Path) -> Dict[str, Any]:
    """Build lightweight summary metadata for one project directory."""
    state_path = proj_dir / "_control" / "01_PROJECT_STATE.md"
    ledger_path = proj_dir / "_control" / "02_DECISION_LEDGER.md"
    ask_path = proj_dir / "_bus" / "ask.md"

    stage_summary = "阶段待更新"
    if state_path.is_file():
        for line in _safe_read_text(state_path, 8192).splitlines():
            if "当前阶段" in line:
                stage_summary = re.sub(r"[>*`#]+", "", line).strip()
                break

    ledger_count = 0
    if ledger_path.is_file():
        ledger_count = len(re.findall(r"(?m)^\|\s*\*\*D-\d+\*\*", _safe_read_text(ledger_path, 65536)))

    weekly_root = proj_dir / "04_组会与周报_Weekly"
    weekly_sessions = 0
    if weekly_root.is_dir():
        weekly_sessions = sum(1 for p in weekly_root.iterdir() if p.is_dir() and not p.name.startswith("."))

    mtime = proj_dir.stat().st_mtime
    for check_p in (state_path, ledger_path, ask_path):
        if check_p.is_file():
            mtime = max(mtime, check_p.stat().st_mtime)

    return {
        "name": proj_dir.name,
        "stage": stage_summary,
        "ledger_count": ledger_count,
        "weekly_sessions": weekly_sessions,
        "updated_at": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _scan_section_files(base_dir: Path, project_dir: Path, max_depth: int = 3) -> List[Dict[str, Any]]:
    """Collect readable files within a project section (following category symlinks safely)."""
    items: List[Dict[str, Any]] = []
    if not base_dir.exists():
        return items

    seen_real: set = set()
    base_depth = len(base_dir.parts)

    for root_str, dirs, files in os.walk(str(base_dir), followlinks=True):
        root_p = Path(root_str)
        cur_depth = len(root_p.parts) - base_depth
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        if cur_depth >= max_depth:
            dirs[:] = []

        for fname in sorted(files):
            if fname.startswith("."):
                continue
            fpath = root_p / fname
            if fpath.suffix.lower() not in ALLOWED_EXTS:
                continue
            try:
                real_p = fpath.resolve()
            except OSError:
                continue
            if not _is_within(real_p, project_dir):
                continue
            key = str(fpath)
            if key in seen_real:
                continue
            seen_real.add(key)
            rel_to_proj = os.path.relpath(str(fpath), str(project_dir))
            try:
                st = fpath.stat()
                mtime_str = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
                size_kb = round(st.st_size / 1024.0, 1)
            except OSError:
                mtime_str = ""
                size_kb = 0.0
            items.append({
                "name": fname,
                "rel_path": rel_to_proj,
                "folder": os.path.relpath(str(root_p), str(base_dir)),
                "ext": fpath.suffix.lower(),
                "mtime": mtime_str,
                "size_kb": size_kb,
            })
    return items


def run_six_point_checks(proj_dir: Path) -> Dict[str, Any]:
    """Execute 6 automated structural, security, and academic compliance checks."""
    checks: List[Dict[str, Any]] = []

    # 1. Freeze Archive Seal (check 冻结存档-* and **/冻结件)
    frozen_targets: List[Path] = []
    for root_s, dirs, _ in os.walk(str(proj_dir)):
        for d in dirs:
            if d.startswith("冻结存档-") or d == "冻结件":
                frozen_targets.append(Path(root_s) / d)
    if frozen_targets:
        writable_items = []
        for fdir in frozen_targets:
            mode = stat.S_IMODE(fdir.stat().st_mode)
            if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                writable_items.append(f"{fdir.name} ({oct(mode)})")
        is_ro = len(writable_items) == 0
        checks.append({
            "id": "freeze_seal",
            "title": "只读冻结存档封印",
            "category": "安全与数据防篡改",
            "status": "pass" if is_ro else "warn",
            "metric_text": f"{len(frozen_targets)} 处只读锁定" if is_ro else f"{len(writable_items)} 处仍可写",
            "details": [f"已锁定目录：{', '.join(f.name for f in frozen_targets)}"] if is_ro else [f"未锁只读：{', '.join(writable_items)}"],
        })
    else:
        checks.append({
            "id": "freeze_seal",
            "title": "只读冻结存档封印",
            "category": "安全与数据防篡改",
            "status": "pass",
            "metric_text": "无冻结存档（纯新课题）",
            "details": ["当前课题无历史冻结存档。"],
        })

    # 2. Symlink Integrity (active exchange root + Antigravity skills)
    total_links = 0
    broken_links: List[str] = []
    for root_s, dirs, files in os.walk(str(EXCHANGE_ROOT)):
        dirs[:] = [d for d in dirs if d not in ("冻结存档-20261117", "99_历史归档_Archive")]
        for entry in dirs + files:
            p = Path(root_s) / entry
            if p.is_symlink():
                total_links += 1
                if not p.exists():
                    broken_links.append(str(p))
    if GEMINI_SKILLS_DIR.is_dir():
        for p in GEMINI_SKILLS_DIR.iterdir():
            if p.is_symlink():
                total_links += 1
                if not p.exists():
                    broken_links.append(str(p))

    checks.append({
        "id": "symlink_integrity",
        "title": "跨区与实验分类软链接",
        "category": "目录结构健康",
        "status": "pass" if not broken_links else "fail",
        "metric_text": f"{total_links} 条全通 · 0 断链" if not broken_links else f"发现 {len(broken_links)} 条断链",
        "details": broken_links if broken_links else [f"覆盖交换区兼容入口、P01 A~E 分类索引与 Antigravity 技能链接，共 {total_links} 条全部有效。"],
    })

    # 3. Secret Isolation Check
    models_cfg = consult.load_toml(HUB_DIR / "models.toml") if (HUB_DIR / "models.toml").is_file() else {}
    env_path = Path(str(models_cfg.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()
    env_outside = not _is_within(env_path, EXCHANGE_ROOT)
    env_mode = stat.S_IMODE(env_path.stat().st_mode) if env_path.exists() else 0
    env_ok = env_path.exists() and env_outside and (env_mode == 0o600)
    leaked_env_files = [
        str(p) for p in EXCHANGE_ROOT.rglob(".env*")
        if "冻结存档-20261117" not in str(p) and "99_历史归档_Archive" not in str(p) and p.is_file()
    ]
    sec_ok = env_ok and not leaked_env_files
    sec_details = [f"宿主机密钥路径：{env_path}（权限 {oct(env_mode)}，虚拟机不可见）"]
    if leaked_env_files:
        sec_details.extend([f"警告：交换区内发现疑似环境文件 {x}" for x in leaked_env_files])
    checks.append({
        "id": "secret_isolation",
        "title": "宿主机密钥权限与隔离",
        "category": "安全与防泄露",
        "status": "pass" if sec_ok else "warn",
        "metric_text": "权限 600 · 宿主机隔离" if sec_ok else "待检查权限或位置",
        "details": sec_details,
    })

    # 4, 5, 6. Manuscript Checks using check_manuscript.py helpers
    contract_p = proj_dir / "_control" / "00_CONTRACT.md"
    banned = check_manuscript.extract_contract_banned(contract_p if contract_p.exists() else None)
    targets = [
        p for p in [
            proj_dir / "01_论文与协议" / "论文初稿.md",
            proj_dir / "01_论文与协议" / "研究论文目标与执行路线.md",
        ] if p.exists()
    ]
    ms_broken: List[str] = []
    ms_placeholders: List[str] = []
    ms_banned: List[str] = []
    ms_style_warns: List[str] = []

    for tf in targets:
        res = check_manuscript.check_file(tf, banned)
        ms_broken.extend(f"[{tf.name}] {x}" for x in res["broken_links"])
        ms_placeholders.extend(f"[{tf.name}] {x}" for x in res["placeholders"])
        ms_banned.extend(f"[{tf.name}] {x}" for x in res["banned_hits"])
        ms_style_warns.extend(f"[{tf.name}] {x}" for x in res["ai_fillers"])
        ms_style_warns.extend(f"[{tf.name}] {x}" for x in res["first_person_hits"])
        ms_style_warns.extend(f"[{tf.name}] {x}" for x in res["defensive_hits"])

    if not targets:
        contract_txt = _safe_read_text(contract_p, 8192)
        is_template_only = ("（在此" in contract_txt) or (not contract_txt.strip())
        checks.append({
            "id": "ms_links",
            "title": "文稿与立项契约就绪度",
            "category": "学术交付质量",
            "status": "warn" if is_template_only else "pass",
            "metric_text": "契约仍为模板 · 暂无主稿" if is_template_only else "立项契约已填 · 暂无主稿",
            "details": ["_control/00_CONTRACT.md 仍包含模板占位符，需先由 Claude 落盘科研契约。"] if is_template_only else ["尚未生成 论文初稿.md，当前契约已就绪。"],
        })
    else:
        link_fail = len(ms_broken) + len(ms_placeholders)
        checks.append({
            "id": "ms_links",
            "title": "文稿断链与占位符",
            "category": "学术交付质量",
            "status": "pass" if link_fail == 0 else "fail",
            "metric_text": f"{len(targets)} 份核心稿 · 0 断链 · 0 待核" if link_fail == 0 else f"{link_fail} 项异常",
            "details": (ms_broken + ms_placeholders) if link_fail else [f"已扫描 {', '.join(t.name for t in targets)}，所有相对引用均真实可达，无 TODO/待核 遗留。"],
        })

    checks.append({
        "id": "noclaim_gate",
        "title": "不宣称红线合规检查",
        "category": "学术严谨性门禁",
        "status": "pass" if not ms_banned else "fail",
        "metric_text": "0 违规夸大宣称" if not ms_banned else f"命中 {len(ms_banned)} 处禁词",
        "details": ms_banned if ms_banned else ["已对照 00_CONTRACT.md 不宣称清单逐行扫描，未发现越界夸大表述。"],
    })

    checks.append({
        "id": "prose_style",
        "title": "去 AI 腔与去防御措辞",
        "category": "文风与可发表性",
        "status": "pass" if not ms_style_warns else "warn",
        "metric_text": "文风干净 (0 处提醒)" if not ms_style_warns else f"{len(ms_style_warns)} 处可精修过渡词",
        "details": ms_style_warns if ms_style_warns else ["未检测到高频 AI 机械过渡词、主观第一人称或自我贬低防御句式。"],
    })

    pass_cnt = sum(1 for c in checks if c["status"] == "pass")
    warn_cnt = sum(1 for c in checks if c["status"] == "warn")
    fail_cnt = sum(1 for c in checks if c["status"] == "fail")
    score = round((pass_cnt + 0.6 * warn_cnt) / max(len(checks), 1) * 100)

    return {
        "project": proj_dir.name,
        "checked_at": datetime.datetime.now().strftime("%H:%M:%S"),
        "score": score,
        "summary": {"pass": pass_cnt, "warn": warn_cnt, "fail": fail_cnt, "total": len(checks)},
        "checks": checks,
    }


def build_overview_payload() -> Dict[str, Any]:
    """Assemble global hub metadata, project list, sanitized model status, manual & skill index."""
    models_path = HUB_DIR / "models.toml"
    config = consult.load_toml(models_path) if models_path.is_file() else {}
    general = config.get("general", {})
    env_path = Path(str(general.get("env_file", "~/.config/research_hub/.env"))).expanduser()
    dotenv = consult.load_dotenv(env_path)

    projects: List[Dict[str, Any]] = []
    if PROJECTS_DIR.is_dir():
        for p in sorted(PROJECTS_DIR.iterdir(), key=lambda x: x.name):
            if p.is_dir() and not p.name.startswith("."):
                projects.append(_summarize_project(p))

    roles_status: List[Dict[str, Any]] = []
    for role_name in config.get("roles", {}):
        rt = consult.resolve_role_runtime(role_name, config, dotenv, None)
        roles_status.append({
            "role": rt["role"],
            "description": rt["description"],
            "model": rt["model"],
            "wire_api": rt["wire_api"],
            "base_url": rt["base_url"],
            "ready": bool(rt["api_key"]),
            "key_source": rt["key_source"],
        })

    skill_index_md = _safe_read_text(SKILLS_DIR / "SKILL_INDEX.md")
    parsed_skills = _parse_skill_index(skill_index_md)
    if not parsed_skills and SKILLS_DIR.is_dir():
        parsed_skills = [
            {"category": "通用科研", "name": sd.name, "trigger": "按需读取", "path": f"{sd.name}/SKILL.md"}
            for sd in sorted(SKILLS_DIR.iterdir(), key=lambda x: x.name)
            if sd.is_dir() and (sd / "SKILL.md").is_file()
        ]

    return {
        "app": "research_hub_dashboard",
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exchange_root_host": str(EXCHANGE_ROOT),
        "exchange_root_vm": "/Volumes/My Shared Files/ClaudeShield-Exchange",
        "active_project": general.get("active_project", ""),
        "env_file": str(env_path),
        "projects": projects,
        "roles": roles_status,
        "skills": parsed_skills,
        "workspace_index_md": _safe_read_text(HUB_DIR / "WORKSPACE_INDEX.md"),
        "operation_manual_md": _safe_read_text(HUB_DIR / "OPERATION_MANUAL.md"),
        "skill_index_md": skill_index_md,
    }


def build_project_payload(proj_dir: Path) -> Dict[str, Any]:
    """Assemble structured visual data, control panels, bus messages, and categorized files."""
    state_md = _safe_read_text(proj_dir / "_control" / "01_PROJECT_STATE.md")
    ledger_md = _safe_read_text(proj_dir / "_control" / "02_DECISION_LEDGER.md")
    contract_md = _safe_read_text(proj_dir / "_control" / "00_CONTRACT.md")
    claude_md = _safe_read_text(proj_dir / "CLAUDE.md")
    readme_md = _safe_read_text(proj_dir / "README.md")

    archive_dir = proj_dir / "_bus" / "archive"
    archive_files: List[Dict[str, str]] = []
    if archive_dir.is_dir():
        for f in sorted(archive_dir.iterdir(), key=lambda x: x.name, reverse=True)[:30]:
            if f.is_file() and f.suffix.lower() == ".md":
                archive_files.append({"name": f.name, "rel_path": f"_bus/archive/{f.name}"})

    sections = []
    total_files = 0
    for label, sub in [
        ("01_论文与协议", "01_论文与协议"),
        ("02_实验与验证", "02_实验与验证"),
        ("03_代码与脚本", "03_代码与脚本"),
        ("04_组会与周报_Weekly", "04_组会与周报_Weekly"),
    ]:
        files = _scan_section_files(proj_dir / sub, proj_dir, max_depth=3)
        total_files += len(files)
        sections.append({"label": label, "files": files})

    return {
        "name": proj_dir.name,
        "vm_path": f"/Volumes/My Shared Files/ClaudeShield-Exchange/01_在研课题_Projects/{proj_dir.name}",
        "host_path": str(proj_dir),
        "total_files": total_files,
        "parsed_milestones": _parse_milestones(state_md),
        "parsed_todos": _parse_todos(state_md),
        "parsed_decisions": _parse_decisions(ledger_md),
        "parsed_redlines": _parse_redlines(contract_md),
        "module_stats": _compute_module_stats(proj_dir),
        "control": {
            "state_md": state_md,
            "ledger_md": ledger_md,
            "contract_md": contract_md,
            "claude_md": claude_md,
            "readme_md": readme_md,
        },
        "bus": {
            "ask_md": _safe_read_text(proj_dir / "_bus" / "ask.md"),
            "reply_md": _safe_read_text(proj_dir / "_bus" / "reply.md"),
            "archive_files": archive_files,
        },
        "sections": sections,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """Localhost-only HTTP handler for the multi-project research dashboard."""

    server_version = "ResearchHubDashboard/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _is_trusted_request(self, require_same_origin: bool = False) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        if require_same_origin:
            origin = self.headers.get("Origin") or ""
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                return False
        return True

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_path: Path) -> None:
        body = html_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._is_trusted_request():
            self._send_json({"error": "Forbidden host"}, HTTPStatus.FORBIDDEN)
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._send_html(DASHBOARD_DIR / "index.html")
            return

        if parsed.path == "/api/overview":
            self._send_json(build_overview_payload())
            return

        if parsed.path == "/api/project":
            name = (params.get("name") or [""])[0]
            proj_dir = _validate_project_name(name)
            if not proj_dir:
                self._send_json({"error": "Invalid or unknown project"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(build_project_payload(proj_dir))
            return

        if parsed.path == "/api/checks":
            name = (params.get("project") or [""])[0]
            proj_dir = _validate_project_name(name)
            if not proj_dir:
                self._send_json({"error": "Invalid or unknown project"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(run_six_point_checks(proj_dir))
            return

        if parsed.path == "/api/doc":
            proj_name = (params.get("project") or [""])[0]
            rel_path = (params.get("path") or [""])[0]
            proj_dir = _validate_project_name(proj_name)
            if not proj_dir or not rel_path:
                self._send_json({"error": "Invalid project or path"}, HTTPStatus.BAD_REQUEST)
                return
            candidate = proj_dir / rel_path
            if not _is_within(candidate, proj_dir) or not candidate.is_file():
                self._send_json({"error": "File not found or out of project scope"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "rel_path": rel_path,
                "real_path": str(candidate.resolve()),
                "content": _safe_read_text(candidate),
            })
            return

        if parsed.path == "/api/skill":
            skill_name = (params.get("name") or [""])[0].strip()
            if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
                self._send_json({"error": "Invalid skill name"}, HTTPStatus.BAD_REQUEST)
                return
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            if not _is_within(skill_file, SKILLS_DIR) or not skill_file.is_file():
                self._send_json({"error": "Skill not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "name": skill_name,
                "path": str(skill_file),
                "content": _safe_read_text(skill_file),
            })
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._is_trusted_request(require_same_origin=True):
            self._send_json({"error": "Forbidden"}, HTTPStatus.FORBIDDEN)
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        proj_name = (params.get("project") or [""])[0]
        proj_dir = _validate_project_name(proj_name)
        if not proj_dir:
            self._send_json({"error": "Invalid project"}, HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/checkms":
            cmd = [sys.executable, str(HUB_DIR / "scripts" / "check_manuscript.py"), "--project", proj_dir.name]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            self._send_json({"returncode": proc.returncode, "output": (proc.stdout or "") + (proc.stderr or "")})
            return

        if parsed.path == "/api/dryrun":
            target = (params.get("target") or ["all"])[0]
            if target not in ("redteam", "advisor", "all", "glm_heavy", "glm_fast"):
                self._send_json({"error": "Invalid consult target"}, HTTPStatus.BAD_REQUEST)
                return
            cmd = [sys.executable, str(HUB_DIR / "consult.py"), target, "--project", proj_dir.name, "--dry-run"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            self._send_json({"returncode": proc.returncode, "output": (proc.stdout or "") + (proc.stderr or "")})
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="全局多课题科研总控大屏本地 HTTP 服务")
    parser.add_argument("--port", type=int, default=8765, help="本地监听端口（默认 8765）")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"[ResearchHub Dashboard v2] Listening on http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
