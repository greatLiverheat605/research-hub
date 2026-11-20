#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/check_manuscript.py
论文草稿与科研报告自动化体检器（零第三方依赖，兼容 Python 3.9+）。

检查项目：
1. Markdown 相对链接完整性检查（解析真实路径，避免软链接偏移误报，消灭断链）
2. 未完成占位符扫描（TODO / TBD / FIXME / 待核 / 未核 / [?]）
3. 不宣称清单与禁用夸张词扫描（自动读取当前课题 _control/00_CONTRACT.md + 通用黑名单）
4. 去 AI 腔与排版体检（高频机械过渡词、主观第一人称、正文粗体堆砌）
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


HUB_DIR = Path(__file__).resolve().parent.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"

DEFAULT_BANNED_CLAIMS = [
    "颠覆认知",
    "真冠军",
    "喂错数据",
    "纹丝不动",
    "一图看懂",
    "绝对标准答案",
    "准确率",
    "正确率",
    "首次用成像",
    "首个统一基准",
    "恢复了真实无噪声",
    "恢复了真实三维结构",
    "胜过所有",
    "证据对连续 σ 稳健",
    "证据对连续σ稳健",
]

PLACEHOLDER_PATTERNS = [
    (r"\b(?:TODO|TBD|FIXME)\b", "英文未完成占位符"),
    (r"待核|未核", "待核/未核文献或数据标记"),
    (r"\[[?？]\]", "问号占位符"),
]

AI_FILLER_ZH = [
    "首先",
    "其次",
    "最后",
    "此外",
    "另外",
    "接下来",
    "值得注意的是",
    "需要指出的是",
    "重要的是",
    "必须强调的是",
]

AI_FILLER_EN = [
    "first and foremost",
    "to begin with",
    "moreover",
    "furthermore",
    "last but not least",
    "it is worth noting",
    "importantly",
]

FIRST_PERSON_RE = re.compile(
    r"(我认为|我觉得|我个人(?:认为|看法)|我的研究|\bin my opinion\b|\bi believe\b)",
    re.IGNORECASE,
)

DEFENSIVE_PATTERNS = [
    r"仅做了初步尝试",
    r"仅仅做了初步",
    r"只能提供有限参考",
    r"本文无意",
    r"我们无意",
    r"本文并不声称",
    r"我们并不认为",
    r"这并不意味着",
    r"虽然样本有限",
    r"仍有很大提升空间",
    r"\bnot intended to provide\b",
    r"\bwe do not attempt to\b",
    r"\bwe do not claim\b",
    r"\bthis paper does not claim\b",
    r"\bcould potentially\b",
    r"\bbeyond the scope\b",
    r"\bwhile preliminary\b",
]
DEFENSIVE_RE = re.compile("(" + "|".join(DEFENSIVE_PATTERNS) + ")", re.IGNORECASE)

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
NEGATION_CONTEXT_RE = re.compile(r"(不宣称|不得称|严禁|禁止|禁用|替代|不要|不称)")


def _read_active_project_from_models() -> str:
    models_toml = HUB_DIR / "models.toml"
    if not models_toml.exists():
        return ""
    m = re.search(r'(?m)^active_project\s*=\s*"([^"]+)"', models_toml.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else ""


def resolve_project_dir(project_arg: Optional[str]) -> Optional[Path]:
    """Resolve active project directory from --project, cwd, or Hub models.toml."""
    if project_arg:
        cand = Path(project_arg).expanduser()
        if cand.is_dir():
            return cand.resolve()
        sub = PROJECTS_DIR / project_arg
        if sub.is_dir():
            return sub.resolve()
        return None

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if parent.parent == PROJECTS_DIR and (parent / "_control").is_dir():
            return parent

    active = _read_active_project_from_models()
    if active and (PROJECTS_DIR / active).is_dir():
        return (PROJECTS_DIR / active).resolve()
    return None


def extract_contract_banned(contract_path: Optional[Path]) -> List[str]:
    """Extract quoted prohibited phrases from 00_CONTRACT.md if present."""
    phrases = list(DEFAULT_BANNED_CLAIMS)
    if not contract_path or not contract_path.exists():
        return phrases
    text = contract_path.read_text(encoding="utf-8")
    in_noclaim = False
    for line in text.splitlines():
        if "不宣称" in line:
            in_noclaim = True
        if in_noclaim and ("严禁" in line or "不宣称" in line or "不得称" in line or line.strip().startswith("-")):
            for q in re.findall(r"[“\"]([^”\"]{2,24})[”\"]", line):
                if q not in phrases:
                    phrases.append(q)
    return phrases


def strip_fenced_code_keep_lines(lines: List[str]) -> List[Tuple[int, str]]:
    """Return (1-based_line_no, line_text) skipping fenced ``` blocks."""
    kept: List[Tuple[int, str]] = []
    in_fence = False
    for idx, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append((idx, line))
    return kept


def check_file(md_path: Path, banned_phrases: List[str]) -> Dict[str, List[str]]:
    """Run all manuscript checks on a single markdown file."""
    real_path = md_path.resolve()
    base_dir = real_path.parent
    raw_lines = real_path.read_text(encoding="utf-8").splitlines()
    active_lines = strip_fenced_code_keep_lines(raw_lines)

    broken_links: List[str] = []
    placeholders: List[str] = []
    banned_hits: List[str] = []
    banned_context_notes: List[str] = []
    ai_fillers: List[str] = []
    first_person_hits: List[str] = []
    defensive_hits: List[str] = []
    bold_prose_lines: List[str] = []

    zh_filler_re = re.compile("(" + "|".join(re.escape(w) for w in AI_FILLER_ZH) + ")")
    en_filler_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in AI_FILLER_EN) + r")\b", re.IGNORECASE)

    for ln, text in active_lines:
        # 1. Markdown relative links
        for match in LINK_RE.finditer(text):
            raw_target = match.group(1).strip()
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:", "#", "file://")):
                continue
            target_part = raw_target.split()[0].strip("<>").split("#", 1)[0]
            if not target_part:
                continue
            resolved_target = base_dir / target_part
            if not resolved_target.exists():
                broken_links.append(f"L{ln}: `{raw_target}` -> 未找到 `{resolved_target}`")

        # 2. Placeholders
        for pat, desc in PLACEHOLDER_PATTERNS:
            if re.search(pat, text):
                placeholders.append(f"L{ln} [{desc}]: {text.strip()[:90]}")

        # 3. Banned no-claim phrases
        for phrase in banned_phrases:
            hit_idx = text.find(phrase)
            if hit_idx != -1:
                prefix_window = text[max(0, hit_idx - 25) : hit_idx]
                if re.search(r"不宣称|不声称|严禁|禁止|不能说|不得|不可|避免使用|非[：:\s]", prefix_window):
                    banned_context_notes.append(f"L{ln} [否定边界声明「{phrase}」]: {text.strip()[:90]}")
                else:
                    banned_hits.append(f"L{ln} [命中禁用宣称「{phrase}」]: {text.strip()[:90]}")

        # 4. De-AI-ification & style checks
        m_zh = zh_filler_re.search(text)
        if m_zh:
            ai_fillers.append(f"L{ln} [中文套话「{m_zh.group(1)}」]: {text.strip()[:80]}")
        m_en = en_filler_re.search(text)
        if m_en:
            ai_fillers.append(f"L{ln} [英文套话「{m_en.group(1)}」]: {text.strip()[:80]}")
        m_fp = FIRST_PERSON_RE.search(text)
        if m_fp:
            first_person_hits.append(f"L{ln} [主观第一人称「{m_fp.group(1)}」]: {text.strip()[:80]}")

        m_def = DEFENSIVE_RE.search(text)
        if m_def and not NEGATION_CONTEXT_RE.search(text):
            defensive_hits.append(f"L{ln} [防御性措辞「{m_def.group(1)}」]: {text.strip()[:85]}")

        stripped = text.strip()
        if (
            ("**" in stripped or "__" in stripped)
            and not stripped.startswith(("#", "|", ">", "-", "*", "+"))
            and not re.match(r"^\d+\.", stripped)
        ):
            bold_prose_lines.append(f"L{ln}: {stripped[:70]}")

    return {
        "broken_links": broken_links,
        "placeholders": placeholders,
        "banned_hits": banned_hits,
        "banned_context_notes": banned_context_notes,
        "ai_fillers": ai_fillers,
        "first_person_hits": first_person_hits,
        "defensive_hits": defensive_hits,
        "bold_prose_lines": bold_prose_lines,
    }


def _print_group(title: str, items: List[str], limit: int = 12) -> None:
    if not items:
        return
    print(f"  - {title} ({len(items)} 项):")
    for item in items[:limit]:
        print(f"      {item}")
    if len(items) > limit:
        print(f"      ... 其余 {len(items) - limit} 项已省略")


def main() -> int:
    parser = argparse.ArgumentParser(description="论文草稿与研究报告自动化体检器（断链 / 占位符 / 禁用词 / 去AI腔）")
    parser.add_argument("--file", action="append", help="指定待检查的 Markdown 文件路径（可多次传入）")
    parser.add_argument("--project", help="指定课题名称（默认自动识别或读取 active_project）")
    args = parser.parse_args()

    project_dir = resolve_project_dir(args.project)
    contract_path = (project_dir / "_control" / "00_CONTRACT.md") if project_dir else None
    banned_phrases = extract_contract_banned(contract_path)

    target_files: List[Path] = []
    if args.file:
        for f in args.file:
            p = Path(f).expanduser()
            if not p.exists():
                print(f"[ERROR] 找不到文件：{p}", file=sys.stderr)
                return 2
            target_files.append(p)
    else:
        if not project_dir:
            print("[ERROR] 无法识别当前课题目录，请使用 --file 或 --project 指定。", file=sys.stderr)
            return 2
        for candidate in [
            project_dir / "01_论文与协议" / "论文初稿.md",
            project_dir / "01_论文与协议" / "研究论文目标与执行路线.md",
        ]:
            if candidate.exists():
                target_files.append(candidate)

    if not target_files:
        print("[WARN] 当前课题未找到默认论文草稿，请通过 --file 指定具体 Markdown 文件。")
        return 0

    total_errors = 0
    total_warnings = 0

    print(f"=== 科研稿件自动化体检 (课题: {project_dir.name if project_dir else '自定义文件'}) ===")
    for fpath in target_files:
        res = check_file(fpath, banned_phrases)
        err_cnt = len(res["broken_links"]) + len(res["placeholders"]) + len(res["banned_hits"])
        warn_cnt = (
            len(res["ai_fillers"])
            + len(res["first_person_hits"])
            + len(res["defensive_hits"])
            + (1 if len(res["bold_prose_lines"]) > 10 else 0)
        )
        total_errors += err_cnt
        total_warnings += warn_cnt

        status = "FAIL" if err_cnt > 0 else ("WARN" if warn_cnt > 0 else "PASS")
        print(f"\n[{status}] {fpath.name} (实际路径: {fpath.resolve()})")
        _print_group("❌ 断链 (Broken Links)", res["broken_links"])
        _print_group("❌ 未清零占位符 (Placeholders)", res["placeholders"])
        _print_group("❌ 命中不宣称禁用词 (No-Claim Hits)", res["banned_hits"])
        _print_group("ℹ️ 禁用词的否定边界声明 (仅确认上下文)", res["banned_context_notes"], limit=5)
        _print_group("⚠️ AI 腔高频过渡词 (AI Fillers)", res["ai_fillers"])
        _print_group("⚠️ 主观第一人称 (First Person)", res["first_person_hits"])
        _print_group("⚠️ 防御性写作/自我贬低句式 (Defensive Phrasing，可用 anti-defensive-writing 改为正向范围)", res["defensive_hits"])
        if res["bold_prose_lines"]:
            print(f"  - ⚠️ 正文段落含加粗标记共 {len(res['bold_prose_lines'])} 处（投稿前建议精简）")
        if err_cnt == 0 and warn_cnt == 0:
            print("  ✅ 断链 / 占位符 / 不宣称词 / AI 套话 / 防御措辞检查全部通过。")

    print(f"\n=== 总结：阻断项 {total_errors} 个 | 建议精修提醒 {total_warnings} 项 ===")
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
