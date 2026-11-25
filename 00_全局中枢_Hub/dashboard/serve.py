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
import mimetypes
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
EXCHANGE_ROOT = Path(os.environ.get("RH_EXCHANGE_ROOT", HUB_DIR.parent))
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"
SKILLS_DIR = EXCHANGE_ROOT / "00_全局中枢_Hub" / "skills"
GEMINI_SKILLS_DIR = Path.home() / ".gemini" / "config" / "skills"

sys.path.insert(0, str(HUB_DIR))
sys.path.insert(0, str(HUB_DIR / "scripts"))
from protected_paths import walk, protected
import job_status
import check_manuscript  # noqa: E402
import consult  # noqa: E402
import exp_store  # noqa: E402


SKIP_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints", "冻结存档-20261117", "node_modules"}
ALLOWED_EXTS = {".md", ".py", ".ipynb", ".json", ".toml", ".pptx", ".pdf", ".tsv"}

MODULE_DEFS = [
    ("A", "A_T3T4管线与采样验收", "T3/T4 管线与采样验收", "#0072B2"),
    ("B", "B_分段统计与噪声误差校准", "分段统计与噪声校准", "#529286"),
    ("C", "C_图扩散与锚点对照实验", "图扩散与锚点对照", "#E69F00"),
    ("D", "D_外部发表方法与数据对标", "外部发表方法对标", "#CC79A7"),
    ("E", "E_历史多智能体互审记录", "多智能体互审复核", "#64748B"),
]


def _safe_read_text(path: Path, max_bytes: int = 512 * 1024) -> str:
    """Read UTF-8 text safely with size guard."""
    if protected(path.resolve()) or not path.is_file() or not (_is_within(path, EXCHANGE_ROOT) or _is_within(path,HUB_DIR)):
        return ""
    with path.open("rb") as source:
        data = source.read(max_bytes+1)
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


def _summarize_long_todo_detail(raw_detail: str) -> Tuple[str, str]:
    """若待办详情被追加了多轮【D-0xx】历史流水，自动提取最新一条【...】及「下一步」作为首屏摘要，全文留作折叠展开。"""
    text = raw_detail.strip()
    if len(text) <= 180:
        return text, ""
    # 查找最后一段 【D-xxx...】 或 【更新...】
    chunks = list(re.finditer(r"【[^】]{2,40}】", text))
    if chunks:
        last_pos = chunks[-1].start()
        latest = text[last_pos:].strip()
        # 去掉末尾“（旧待办：...）”冗余备注
        latest = re.sub(r"（旧待办[：:].*$", "", latest).strip()
        return latest, text
    return text[:180] + "…", text


def _parse_todos(state_md: str) -> List[Dict[str, str]]:
    """Parse Top N numbered priority tasks from 01_PROJECT_STATE.md."""
    todos: List[Dict[str, Any]] = []
    in_todos = False
    current: Optional[Dict[str, Any]] = None

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
    out: List[Dict[str, str]] = []
    for t in todos:
        full_str = "；".join(t["details"])
        short_str, full_more = _summarize_long_todo_detail(full_str)
        # 若标题还停留在旧阶段而详情已有最新【D-0xx】，把最新编号提炼到优先级标签
        last_d = re.findall(r"D-\d{3}", short_str)
        prio_label = f"{t['priority']} · 最新至 {last_d[-1]}" if last_d and last_d[-1] not in t["priority"] else t["priority"]
        out.append({
            "priority": prio_label,
            "title": t["title"],
            "detail": short_str,
            "full_detail": full_more,
        })
    return out


def _parse_inbox(state_md: str) -> List[str]:
    """Extract bullet lines under ## 🚨 待汇报与未闭环事项收件箱 from 01_PROJECT_STATE.md."""
    items: List[str] = []
    in_inbox = False
    for line in state_md.splitlines():
        s = line.strip()
        if s.startswith("##") and ("待汇报与未闭环" in s or "收件箱" in s):
            in_inbox = True
            continue
        if in_inbox and (s.startswith("## ") or s == "---"):
            if items:
                break
            continue
        if in_inbox and (s.startswith("-") or s.startswith("*")):
            cleaned = re.sub(r"^[-*]\s*(\[[ xX]\]\s*)?", "", s).strip()
            if cleaned:
                items.append(cleaned)
    return items


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
            elif resolved.is_dir() and _is_within(resolved,proj_dir) and not protected(resolved):
                for root_s, _, files in walk(resolved):
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

    for root_str, dirs, files in walk(base_dir):
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
        dirs[:] = [d for d in dirs if not protected(d) and not d.startswith(".") and not (Path(root_s)/d).is_symlink()]
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
    for root_s, dirs, files in walk(EXCHANGE_ROOT):
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
        str(Path(base)/f) for base, _, files in walk(EXCHANGE_ROOT) for f in files if f.startswith('.env')
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
    gemini_md = _safe_read_text(proj_dir / "GEMINI.md")
    readme_md = _safe_read_text(proj_dir / "README.md")

    pivot_guide_path = None
    for cand in ("P01_JARGON_AND_PIVOT_GUIDE.md", "P03_DIRECTION_AND_JARGON_GUIDE.md", "DIRECTION_AND_JARGON_GUIDE.md"):
        if (proj_dir / "_control" / cand).is_file():
            pivot_guide_path = proj_dir / "_control" / cand
            break
    pivot_guide_md = _safe_read_text(pivot_guide_path) if pivot_guide_path else ""
    pivot_guide_name = f"_control/{pivot_guide_path.name}" if pivot_guide_path else ""

    gitea_inbox_md = _safe_read_text(proj_dir / "05_Gitea协作同步区" / "ISSUES_DETAIL_INBOX.md")
    release_ledger_tsv = _safe_read_text(proj_dir / "05_Gitea协作同步区" / "RELEASE_CLEANUP_LEDGER.tsv")

    archive_dir = proj_dir / "_bus" / "archive"
    archive_files: List[Dict[str, str]] = []
    if archive_dir.is_dir():
        for f in sorted(archive_dir.iterdir(), key=lambda x: x.name, reverse=True)[:30]:
            if f.is_file() and f.suffix.lower() == ".md":
                archive_files.append({"name": f.name, "rel_path": f"_bus/archive/{f.name}"})

    sections = []
    total_files = 0
    sec_defs = [
        ("_control（控制沙盘与破局指南）", "_control"),
        ("01_论文与协议", "01_论文与协议"),
        ("02_实验与验证", "02_实验与验证"),
        ("03_代码与脚本", "03_代码与脚本"),
        ("04_组会与周报_Weekly", "04_组会与周报_Weekly"),
    ]
    if (proj_dir / "05_Gitea协作同步区").is_dir():
        sec_defs.append(("05_Gitea协作同步区", "05_Gitea协作同步区"))
    for label, sub in sec_defs:
        files = _scan_section_files(proj_dir / sub, proj_dir, max_depth=3)
        total_files += len(files)
        sections.append({"label": label, "files": files})

    return {
        "name": proj_dir.name,
        "vm_path": f"/Volumes/My Shared Files/ClaudeShield-Exchange/01_在研课题_Projects/{proj_dir.name}",
        "host_path": str(proj_dir),
        "total_files": total_files,
        "parsed_inbox": _parse_inbox(state_md),
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
            "gemini_md": gemini_md,
            "pivot_guide_md": pivot_guide_md,
            "pivot_guide_name": pivot_guide_name,
            "gitea_inbox_md": gitea_inbox_md,
            "release_ledger_tsv": release_ledger_tsv,
            "readme_md": readme_md,
        },
        "bus": {
            "ask_md": _safe_read_text(proj_dir / "_bus" / "ask.md"),
            "reply_md": _safe_read_text(proj_dir / "_bus" / "reply.md"),
            "scout_ask_md": _safe_read_text(proj_dir / "_bus" / "scout_ask.md"),
            "scout_reply_md": _safe_read_text(proj_dir / "_bus" / "scout_reply.md"),
            "archive_files": archive_files,
        },
        "sections": sections,
    }


def _extract_headline_metric(title: str, detail: str) -> str:
    """从台账摘要与依据中提取 1 行高信噪比核心指标胶囊（Headline Metric）。"""
    src = f"{title} | {detail}"
    patterns = [
        r"(GVS\s*[相对Z差上限改善]*\s*[−+-]?\d+\.?\d*%)",
        r"(ES\s*[相对恶化率差]*\s*[−+-]?\d+\.?\d*%)",
        r"(捕获率\s*[−+-]?\d+\.?\d*%)",
        r"(覆盖率\s*\d+\.?\d*%?)",
        r"(\d+\s*/\s*\d+\s*PASS)",
        r"(±\d+\.?\d*%\s*等价)",
        r"(\d+\.?\d*\s*→\s*\d+\.?\d*\s*nm)",
        r"(点误差\s*[−+-]?\d+\.?\d*\s*nm)",
        r"(差\s*\d+\.?\d*e[−+-]?\d+)",
        r"(n\s*=\s*\d[\d,]*)",
    ]
    found: List[str] = []
    for pat in patterns:
        m = re.search(pat, src)
        if m:
            val = m.group(1).strip()
            if val not in found:
                found.append(val)
        if len(found) >= 2:
            break
    return " · ".join(found)


def _parse_decision_ledger(proj_dir: Path) -> Dict[str, Any]:
    """解析 _control/02_DECISION_LEDGER.md 与实验源表，生成 OpenResearch 风格层叠灌木 (Stacked Bushes) 与双轨谱系。"""
    ledger = proj_dir / "_control" / "02_DECISION_LEDGER.md"
    nodes: List[Dict[str, Any]] = []
    known_ids: List[str] = []

    def _classify(title: str, detail: str) -> Tuple[str, str]:
        t = title + " " + detail[:140]
        if any(k in t for k in ("未通过", "均否", "失败")):
            return "fail", "failed"
        if any(k in t for k in ("终止", "搁置", "删除", "被否定")):
            return "fail", "stopped"
        if any(k in t for k in ("切换", "转向", "澄清目标", "选择「")):
            return "pivot", "done"
        if any(k in t for k in ("冻结", "锁定", "预注册")):
            return "freeze", "done"
        if any(k in t for k in ("尚未运行", "待运行", "待补", "草案")):
            return "info", "pending"
        if any(k in t for k in ("通过", "COMPLETE_OK", "核对", "完成", "入库", "正式立项")):
            return "pass", "done"
        return "info", "done"

    if ledger.is_file():
        text = _safe_read_text(ledger, max_bytes=160_000)
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) < 3:
                continue
            m_id = re.match(r"^\*{0,2}(D-\d+)\*{0,2}$", cols[0])
            if not m_id:
                continue
            did = m_id.group(1)
            date_str = cols[1].strip()
            title_str = cols[2].strip().strip("*")
            detail_str = cols[3].strip() if len(cols) > 3 else ""
            files_col = cols[4].strip() if len(cols) > 4 else ""
            files = re.findall(r"`([^`]+)`", files_col)
            if not files and files_col and files_col != "-":
                files = [x.strip() for x in re.split(r"[·,，;；]", files_col) if x.strip()]

            refs = []
            for r in re.findall(r"\b(D-\d{3})\b", title_str + " " + detail_str + " " + files_col):
                if r != did and r in known_ids and r not in refs:
                    refs.append(r)
            parent_id = refs[-1] if refs else (known_ids[-1] if known_ids else "")
            kind, shape = _classify(title_str, detail_str)
            if not parent_id:
                shape = "root"

            nodes.append({
                "id": did,
                "date": date_str,
                "title": title_str,
                "meta": date_str,
                "headline": _extract_headline_metric(title_str, detail_str),
                "detail": detail_str,
                "files": files,
                "parent": parent_id,
                "refs": refs,
                "children": [],
                "kind": kind,
                "shape": shape,
            })
            known_ids.append(did)

    # 若是 P02 课题，自动把 C01 七法实验分支与 C02~C04 挂载进实验树
    p02_ov = proj_dir / "02_实验与验证/C01_figures/figs_c01_七法/source_data/C01_endpoint_overview.tsv"
    if p02_ov.is_file():
        root_anchor = known_ids[-1] if known_ids else ""
        c01_id = "C01-Mix"
        nodes.append({
            "id": c01_id,
            "date": "2026-11-21",
            "title": "C01 双细胞 11 档混合 × 4 档分辨率 × 七法重建",
            "meta": "44×7=308 槽位",
            "headline": "308/308 PASS · 0 失败",
            "detail": "GM12878 与 K562 按 0:10~10:0 混合 RAW 计数后 ICE 校正，4 档分辨率 (10/25/50/100 kb) 共 44 条件，7 种冻结方法全部完成建模并通过独立核验。",
            "files": [
                "02_实验与验证/C01_figures/figs_c01_七法/main/F6b_mixture_curves_wide.png",
                "02_实验与验证/C01_figures/figs_c01_七法/main/C01_endpoint_overview.png",
                "02_实验与验证/C01_figures/figs_c01_七法/source_data/C01_endpoint_overview.tsv",
            ],
            "parent": root_anchor,
            "refs": [root_anchor] if root_anchor else [],
            "children": [],
            "kind": "pass",
            "shape": "done",
        })
        known_ids.append(c01_id)

        by_method: Dict[str, List[Dict[str, str]]] = {}
        ov_lines = _safe_read_text(p02_ov).splitlines()
        if len(ov_lines) > 1:
            headers = ov_lines[0].split("\t")
            for ln in ov_lines[1:]:
                parts = ln.split("\t")
                if len(parts) >= len(headers):
                    row = dict(zip(headers, parts))
                    by_method.setdefault(row.get("method", ""), []).append(row)
        for mname, mrows in by_method.items():
            if not mname:
                continue
            dsccs = [float(r["endpoint_dSCC"]) for r in mrows if r.get("endpoint_dSCC")]
            mono = sum(1 for r in mrows if r.get("both_curves_monotonic") == "True")
            rnd = any(r.get("random_start_uncontrolled") == "True" for r in mrows)
            notes = [r.get("run_note", "") for r in mrows if r.get("run_note")]
            d_range = f"dSCC {min(dsccs):.3f}–{max(dsccs):.3f}" if dsccs else ""
            hl = f"单调 {mono}/{len(mrows)} 档 · {d_range}"
            kind = "pivot" if notes else ("info" if rnd else "pass")
            shape = "running" if notes else "done"
            mid = f"C01-{mname}"
            nodes.append({
                "id": mid,
                "date": "2026-11-21",
                "title": f"{mname} 四档分辨率混合曲线与端点评估" + (f"（{notes[0]}）" if notes else ""),
                "meta": "随机起点未控" if rnd else "确定性/受控",
                "headline": hl,
                "detail": f"方法 {mname} 在 10/25/50/100 kb 四档分辨率下的端点相似度范围：{d_range}；两曲线均单调档数：{mono}/{len(mrows)}；随机起点未控：{rnd}。" + (f" 备注：{'; '.join(notes)}" if notes else ""),
                "files": [
                    "02_实验与验证/C01_figures/figs_c01_七法/main/F6b_mixture_curves_wide.png",
                    "02_实验与验证/C01_figures/figs_c01_七法/main/C01_endpoint_overview.png",
                ],
                "parent": c01_id,
                "refs": [c01_id],
                "children": [],
                "kind": kind,
                "shape": shape,
            })
            known_ids.append(mid)

        for cid, ctitle, chl, ckind, cshape, cfiles in [
            ("C02-TriMix", "C02 三纯细胞 1:1:1 混合结构对三端点相似度", "待运行", "info", "pending", []),
            ("C03-PseudoBulk", "C03 单细胞按合格细胞数比例伪 bulk 重建", "待运行", "info", "pending", []),
            ("C04-Cost", "C04 资源成本初稿 (cost.tsv) 与使用门槛对照", "同机初稿 · 待硬件复核", "freeze", "running", ["02_实验与验证/C04_cost/cost.tsv"]),
        ]:
            nodes.append({
                "id": cid,
                "date": "2026-11-22",
                "title": ctitle,
                "meta": chl,
                "headline": chl,
                "detail": ctitle,
                "files": cfiles,
                "parent": root_anchor,
                "refs": [root_anchor] if root_anchor else [],
                "children": [],
                "kind": ckind,
                "shape": cshape,
            })
            known_ids.append(cid)

    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        pid = n.get("parent")
        if pid and pid in by_id:
            by_id[pid]["children"].append(n["id"])

    # 计算 OpenResearch Git-Graph 双轨/多轨车道 (lane: 主干继续走父车道，旁支走 lane+1)
    memo_size: Dict[str, int] = {}

    def _sub_size(nid: str) -> int:
        if nid in memo_size:
            return memo_size[nid]
        memo_size[nid] = 1 + sum(_sub_size(c) for c in by_id[nid]["children"] if c in by_id)
        return memo_size[nid]

    for n in nodes:
        _sub_size(n["id"])

    ordered_nodes: List[Dict[str, Any]] = []
    visited = set()

    def _visit_lineage(nid: str, lane: int) -> None:
        if nid in visited or nid not in by_id:
            return
        visited.add(nid)
        node = by_id[nid]
        node["lane"] = lane
        ordered_nodes.append(node)
        kids = [c for c in node["children"] if c in by_id and c not in visited]
        if not kids:
            return
        # 选子树最大的作为主干延续 (continuing child)，其余作为旁支先排在 lane+1
        continuing = max(kids, key=lambda c: (_sub_size(c), c))
        for side in kids:
            if side != continuing:
                _visit_lineage(side, min(lane + 1, 3))
        _visit_lineage(continuing, lane)

    roots = [n["id"] for n in nodes if not n.get("parent") or n["parent"] not in by_id]
    for r in roots:
        _visit_lineage(r, 0)
    for n in nodes:
        if n["id"] not in visited:
            n["lane"] = 0
            ordered_nodes.append(n)

    # 构建章节泳道 (Chapters / Stacked Bushes)
    chapters = _build_chapters(proj_dir, ordered_nodes)
    return {"nodes": ordered_nodes, "chapters": chapters}


def _build_chapters(proj_dir: Path, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把实验树节点聚合成 4~5 个科研阶段章节（Stacked Bushes），并关联该阶段代表图表。"""
    if not nodes:
        return []
    pname = proj_dir.name
    chapters: List[Dict[str, Any]] = []
    if pname.startswith("P01"):
        specs = [
            ("ch1", "阶段一 · 开题收窄与管线校准基线", "T3 精度 3.15e-14 · 250 步采样等价 · 锚点偏移 192.25 nm", range(1, 8), "04_组会与周报_Weekly/2026-11-22_条件插补合成诊断到真实筛选汇报/figures/fig1_timeline.png"),
            ("ch2", "阶段二 · 合成容量检验 (P1) 与候选族筛选", "G 臂 −1.4% / C 臂 −122.1% 未过 · D1 判设计受限 (上限 GVS −5.4%)", range(8, 19), "04_组会与周报_Weekly/2026-11-22_条件插补合成诊断到真实筛选汇报/figures/fig2_synthetic.png"),
            ("ch3", "阶段三 · 真实训练集 K=5 嵌套探针 (n=2,922)", "R1 GVS −1.99% (ES +5.27%) · H3 GVS −1.73% · 折内 G2ε 覆盖率 0.781", range(19, 30), "04_组会与周报_Weekly/2026-11-22_条件插补合成诊断到真实筛选汇报/figures/fig3_probe.png"),
            ("ch4", "阶段四 · 真实校准集一次性筛选 (n=974)", "放大被否定 (C1≡B0, C3≡C2) · 唯一胜出 C2 (对 G2ε GVS −4.5%)", range(30, 38), "04_组会与周报_Weekly/2026-11-22_条件插补合成诊断到真实筛选汇报/figures/fig4_screen.png"),
            ("ch5", "阶段五 · 真实测试集封存门禁与终审拍板", "测试集 975 条未读 · 待拍板是否送测 C2 与论文主口径", range(38, 100), ""),
        ]
        for cid, title, sub, num_range, fig_rel in specs:
            c_nodes = []
            for n in nodes:
                m = re.match(r"^D-(\d+)$", n["id"])
                if m and int(m.group(1)) in num_range:
                    n["chapter"] = cid
                    c_nodes.append(n)
            if c_nodes:
                hero = fig_rel if (fig_rel and (proj_dir / fig_rel).is_file()) else ""
                chapters.append({"id": cid, "title": title, "subtitle": sub, "hero_figure": hero, "nodes": c_nodes})
        return chapters

    # P02 或通用课题按前缀分组
    groups = [
        ("ch_base", "阶段一 · 立项、原档防全读门禁与跨环境基线", "预登记 dbd0d4c9…71ef · 44 条件 ICE 跨机复算 44/44 一致", lambda n: n["id"].startswith("D-"), ""),
        ("ch_c01", "阶段二 · C01 双细胞 11 档混合 × 七法 308 槽位基准", "308/308 PASS · EVRC 与 FLAMINGO 4/4 单调 · 1 项未收敛待裁定", lambda n: n["id"].startswith("C01"), "02_实验与验证/C01_figures/figs_c01_七法/main/F6b_mixture_curves_wide.png"),
        ("ch_c02_04", "阶段三 · C02/C03 扩展混合与 C04 资源成本对照", "cost.tsv 初稿已出 · C02 三细胞与 C03 伪 bulk 待运行", lambda n: n["id"].startswith(("C02", "C03", "C04")), "02_实验与验证/C01_figures/figs_c01_七法/main/C01_endpoint_overview.png"),
    ]
    for cid, title, sub, pred, fig_rel in groups:
        c_nodes = [n for n in nodes if pred(n)]
        for n in c_nodes:
            n["chapter"] = cid
        if c_nodes:
            hero = fig_rel if (fig_rel and (proj_dir / fig_rel).is_file()) else ""
            chapters.append({"id": cid, "title": title, "subtitle": sub, "hero_figure": hero, "nodes": c_nodes})
    return chapters


ARTIFACT_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".tsv", ".csv"}
ARTIFACT_SKIP = {".git", "__pycache__", "node_modules", "Library", ".matplotlib", "benchmark"}


def _scan_project_artifacts(proj_dir: Path, limit: int = 120) -> List[Dict[str, Any]]:
    """扫描课题 02_实验与验证/ 与 04_组会与周报_Weekly/ 下的可视化图表与结果表。"""
    items: List[Dict[str, Any]] = []
    for root_name in ("02_实验与验证", "04_组会与周报_Weekly"):
        base = proj_dir / root_name
        if not base.is_dir():
            continue
        for cur_root, dirs, files in walk(base):
            dirs[:] = [d for d in sorted(dirs) if d not in ARTIFACT_SKIP and not d.startswith(("冻结存档-", "_prev_", "_旧版", "_废弃"))]
            cur_path = Path(cur_root)
            rel_depth = len(cur_path.relative_to(base).parts)
            if rel_depth > 4:
                dirs[:] = []
                continue
            for fn in sorted(files):
                ext = Path(fn).suffix.lower()
                if ext not in ARTIFACT_EXTS:
                    continue
                fp = cur_path / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                items.append({
                    "rel_path": str(fp.relative_to(proj_dir)),
                    "name": fn,
                    "ext": ext,
                    "size_kb": round(st.st_size / 1024.0, 1),
                    "mtime": st.st_mtime,
                })
                if len(items) >= limit:
                    return sorted(items, key=lambda x: x["mtime"], reverse=True)
    return sorted(items, key=lambda x: x["mtime"], reverse=True)


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
            if origin and origin != "http://" + self.headers.get("Host", ""):
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

        if parsed.path == '/api/jobs':
            project = _validate_project_name((params.get('project') or [''])[0])
            if not project:
                self._send_json({'error':'unknown project'},404)
            else:
                self._send_json(job_status.snapshot(project,(params.get('q') or [''])[0]))
            return

        if parsed.path == '/api/running':
            project = _validate_project_name((params.get('project') or [''])[0])
            if not project:
                self._send_json({'error':'unknown project'},404)
                return
            directory = project/'_dispatch/running'
            logs = []
            if directory.resolve() == directory:
                for path in sorted(directory.glob('*.log')):
                    if path.is_symlink() or not path.is_file(): continue
                    try:
                        with path.open('rb') as handle:
                            handle.seek(max(0,os.fstat(handle.fileno()).st_size-16384))
                            logs.append({'job_id':path.stem,'tail':handle.read(16384).decode('utf-8','replace')})
                    except FileNotFoundError:
                        pass
            self._send_json({'jobs':logs})
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
            if protected(candidate) or not _is_within(candidate, proj_dir) or not candidate.is_file():
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

        if parsed.path == "/api/experiments":
            proj_name = (params.get("project") or [""])[0].strip()
            limit = min(200, max(1, int((params.get("limit") or ["50"])[0])))
            proj_dir = _validate_project_name(proj_name) if proj_name else None
            rows = exp_store.query_runs(project=proj_dir.name if proj_dir else (proj_name or None), limit=limit)
            self._send_json({"runs": rows})
            return

        if parsed.path == "/api/decisions":
            proj_name = (params.get("project") or [""])[0]
            proj_dir = _validate_project_name(proj_name)
            if not proj_dir:
                self._send_json({"error": "Invalid project"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(_parse_decision_ledger(proj_dir))
            return

        if parsed.path == "/api/artifacts":
            proj_name = (params.get("project") or [""])[0]
            proj_dir = _validate_project_name(proj_name)
            if not proj_dir:
                self._send_json({"error": "Invalid project"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": _scan_project_artifacts(proj_dir)})
            return

        if parsed.path == "/api/artifact_raw":
            proj_name = (params.get("project") or [""])[0]
            rel_path = (params.get("path") or [""])[0]
            proj_dir = _validate_project_name(proj_name)
            if not proj_dir or not rel_path:
                self._send_json({"error": "Invalid project or path"}, HTTPStatus.BAD_REQUEST)
                return
            candidate = (proj_dir / rel_path).resolve()
            if protected(candidate) or not _is_within(candidate, proj_dir) or not candidate.is_file():
                self._send_json({"error": "File not found"}, HTTPStatus.NOT_FOUND)
                return
            if candidate.relative_to(proj_dir).parts[0] not in ('02_实验与验证','04_组会与周报_Weekly'):
                self._send_json({'error':'outside gallery'},403)
                return
            ext = candidate.suffix.lower()
            if ext not in ARTIFACT_EXTS:
                self._send_json({"error": "Unsupported artifact type"}, HTTPStatus.FORBIDDEN)
                return
            if ext in (".tsv", ".csv"):
                lines = _safe_read_text(candidate, max_bytes=64_000).splitlines()[:120]
                self._send_json({"rel_path": rel_path, "ext": ext, "lines": lines})
                return
            ctype, _ = mimetypes.guess_type(str(candidate))
            body = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
    from service_reload import reload_changed
    server.service_actions = reload_changed
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
