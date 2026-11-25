#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/ppt_studio.py
Track B 专用：GLM 多轮对话迭代「原生可编辑 PPT」引擎

特性：
1. 严格写隔离：所有生成物仅允许写入当前课题的 04_组会与周报_Weekly/<主题目录>/，绝不碰主线。
2. 自动注入 academic-ppt-studio/SKILL.md 与课题 _control/00_CONTRACT.md（不宣称清单）。
3. 源码驱动局部迭代：以 build_pptx.py + chat_history.json 为真源，支持在同一份 .pptx 上多轮
   局部微调（如只改第 4 页布局），自动执行 build_pptx.py 并运行 check_pptx_quality.py 质检。
"""

import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(HUB_DIR))
import consult  # noqa: E402

SKILL_PATH = HUB_DIR / "skills" / "academic-ppt-studio" / "SKILL.md"
QUALITY_SCRIPT = HUB_DIR / "skills" / "academic-ppt-studio" / "scripts" / "check_pptx_quality.py"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child.resolve()), str(parent.resolve())]) == str(parent.resolve())
    except ValueError:
        return False


def _sanitize_slug(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip()).strip("_")
    return cleaned or "组会汇报"


def _tue_sun_week_start(d: datetime.date) -> datetime.date:
    """Return the Tuesday that starts the Tue-Sun weekly reporting cycle containing date d."""
    # weekday(): Mon=0, Tue=1, ..., Sun=6. Offset back to Tuesday (1).
    days_since_tue = (d.weekday() - 1) % 7
    return d - datetime.timedelta(days=days_since_tue)


def _resolve_work_dir(
    project_dir: Path,
    topic: Optional[str],
    explicit_dir: Optional[str],
    mode: str = "weekly",
    new_folder: bool = False,
) -> Path:
    weekly_root = project_dir / "04_组会与周报_Weekly"
    weekly_root.mkdir(parents=True, exist_ok=True)
    if explicit_dir:
        candidate = Path(explicit_dir).expanduser()
        if not candidate.is_absolute():
            candidate = weekly_root / candidate
        candidate = candidate.resolve()
        if not _is_within(candidate, weekly_root.resolve()) or candidate == weekly_root.resolve():
            raise PermissionError(f"安全拦截：PPT 输出目录必须位于 {weekly_root} 的子目录内（当前：{candidate}）")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    today = datetime.date.today()
    if mode == "weekly" and not new_folder:
        week_start = _tue_sun_week_start(today)
        week_end = week_start + datetime.timedelta(days=5)  # Sunday
        existing_in_week: List[Path] = []
        for sub in sorted(weekly_root.iterdir(), key=lambda p: p.stat().st_ctime):
            if not sub.is_dir():
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_", sub.name)
            if not m:
                continue
            try:
                sub_date = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if week_start <= sub_date <= week_end:
                existing_in_week.append(sub)
        if existing_in_week:
            chosen = existing_in_week[0].resolve()
            print(f"📁 本周（{week_start} 周二～{week_end} 周日）已存在周报目录，自动复用：{chosen.name}")
            return chosen

    slug = _sanitize_slug(topic or "组会汇报")[:48]
    folder_name = slug if re.match(r"^\d{4}-\d{2}-\d{2}_", slug) else f"{today.isoformat()}_{slug}"
    work_dir = (weekly_root / folder_name).resolve()
    if not _is_within(work_dir, weekly_root.resolve()):
        raise PermissionError(f"非法输出目录：{work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _load_extra_skills(skill_names: List[str]) -> str:
    chunks = []
    for name in skill_names:
        cleaned = name.strip()
        if not cleaned:
            continue
        p = HUB_DIR / "skills" / cleaned / "SKILL.md"
        if p.is_file():
            chunks.append(f"## [附加技能规范：{cleaned}]\n{p.read_text(encoding='utf-8')[:4000]}")
        else:
            print(f"⚠️ 未找到 Skill: {p}（已跳过）", file=sys.stderr)
    return "\n\n".join(chunks)


def _preview_table_header(file_path: Path) -> str:
    """Return compact 2-line header + sample row preview for TSV/CSV files."""
    try:
        lines: List[str] = []
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(3):
                raw = f.readline()
                if not raw:
                    break
                s = raw.strip()
                if s:
                    lines.append(s[:180] + ("..." if len(s) > 180 else ""))
                if len(lines) >= 2:
                    break
        return " | ".join(lines)
    except Exception:
        return ""


def _scan_project_assets(project_dir: Path, work_dir: Path) -> str:
    """List real figures (.png/.jpg/.svg) and datasets (.json/.csv/.jsonl/.tsv) with table header previews."""
    figs: List[str] = []
    data_files: List[str] = []
    table_previews: List[str] = []
    scan_roots = [work_dir, project_dir / "02_实验与验证"]
    skip_dirs = ("冻结存档-", "_prev_", "_废弃", "_旧版", ".matplotlib")
    for root in scan_roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if any(part.startswith(skip_dirs) or part.endswith("_tmp") or part == "tmp" for part in p.parts):
                continue
            if not p.is_file() or p.name == "chat_history.json":
                continue
            rel = os.path.relpath(p, project_dir)
            suf = p.suffix.lower()
            if suf in (".png", ".jpg", ".jpeg", ".svg") and len(figs) < 30:
                figs.append(rel)
            elif suf in (".json", ".csv", ".jsonl", ".tsv") and len(data_files) < 60:
                data_files.append(rel)
                if suf in (".tsv", ".csv") and len(table_previews) < 15:
                    prev = _preview_table_header(p)
                    if prev:
                        table_previews.append(f"  - `{rel}`: {prev}")
    style_path = HUB_DIR / "plotting" / "academic_style.py"
    lines = [
        f"- 课题根目录绝对路径（`build_pptx.py` 读取图片/数据时可用）: `{project_dir}`",
        f"- 绘图样式库路径: `{style_path}`" if style_path.is_file() else "- 绘图样式库: 默认 matplotlib",
        f"- 现成真实图片 ({len(figs)}): " + (", ".join(figs) if figs else "暂无（0 张）"),
        f"- 真实实验数据文件 ({len(data_files)}): " + (", ".join(data_files) if data_files else "暂无"),
    ]
    if table_previews:
        lines.append("- TSV/CSV 表头与首行样例（严格按此精确列名读取，勿猜）:\n" + "\n".join(table_previews))
    return "\n".join(lines)


def _build_system_prompt(
    project_dir: Path,
    work_dir: Path,
    pptx_path: Path,
    mode: str = "weekly",
    extra_skills: Optional[List[str]] = None,
) -> str:
    skill_txt = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.is_file() else ""
    contract_path = project_dir / "_control" / "00_CONTRACT.md"
    contract_txt = contract_path.read_text(encoding="utf-8")[:4000] if contract_path.is_file() else ""
    weekly_readme = project_dir / "04_组会与周报_Weekly" / "README_周报写入规范.md"
    readme_txt = weekly_readme.read_text(encoding="utf-8")[:2200] if (mode == "weekly" and weekly_readme.is_file()) else ""
    extra_skill_txt = _load_extra_skills(extra_skills or [])

    mode_rule = (
        "4. **周报实验叙事与真实图表铁律（仅周报模式）**：\n"
        "   - 必须围绕「做了什么实验 → 怎么规划 → 怎么跑 → 跑出什么真实结果 → 如何诊断优化 → 优化后结果/下步规划」组织页面；\n"
        "   - 结果页**必须**展示真实实验图：若下方资产清单有 `.png`/`.jpg` 真图，直接用 `slide.shapes.add_picture` 嵌入；若无现成图但有 `.json`/`.csv` 真数据，在 `build_pptx.py` 前半段用 `matplotlib` 严格读取该真实数据文件现场生成 `.png` 再嵌入；**若既无图也无数据，只能标注「待运行/暂无数据」，严禁虚构数组或编造假图**。\n"
        if mode == "weekly"
        else f"4. **当前模式为 `{mode}`（非周报通用模式）**：不强制套用周报六步结构，请严格按用户指令与附加 Skill 组织结构；任何引用到的实验图或数据仍必须来自真实文件，严禁虚构。\n"
    )

    kit_path = HUB_DIR / "skills" / "academic-ppt-studio" / "scripts" / "ppt_kit.py"
    kit_hint = (
        f"3. **防截断公共组件库（强烈推荐优先导入）**：请优先在 `build_pptx.py` 顶部加入\n"
        f"   `import sys; sys.path.insert(0, '{kit_path.parent}')`\n"
        "   `import ppt_kit as pk`\n"
        "   直接复用 `pk.new_prs()`, `pk.blank_slide(prs)`, `pk.add_header(slide, title, tag, subtitle)`, "
        "`pk.add_footer(slide, page_no, total, source)`, `pk.add_card(slide, x, y, w, h, title, accent)`, "
        "`pk.add_text(...)`, `pk.add_table(...)`, `pk.add_picture_fit(slide, img_path, x, y, max_w, max_h)`, `pk.add_notes(slide, text)` "
        "（无需手写几百行底层字体/矩形/表格样板函数，脚本控制在 200 行内最稳）。\n"
        if kit_path.is_file()
        else ""
    )

    return (
        "你是本科研课题的 Track B 组会汇报与可编辑 PPT 制作专家。\n"
        "【硬性要求】\n"
        "1. 必须严格产出**原生可编辑的 `python-pptx` 脚本**（全部使用原生文本框、原生表格、矢量形状与嵌入的真实实验图），严禁生成整页图片 PPT。\n"
        f"2. 脚本文件名固定为 `build_pptx.py`，接收命令行第 1 个参数（`sys.argv[1]`）作为输出 `.pptx` 路径，默认回退为 `{pptx_path.name}`。\n"
        f"{kit_hint}"
        "4. 当用户在后续轮次提出局部修改（例如只改第 3 页或第 5 页表格）时，**必须保留其余未提及页面的现有结构与内容**，只精准改写目标页并输出完整可运行的 `build_pptx.py`。\n"
        f"{mode_rule}"
        "5. 必须严格遵守下方 `academic-ppt-studio` 设计规范（13.333x7.5 宽屏、海军蓝/青绿配色、字体写入 `a:ea` 与 `a:cs`）以及课题 `00_CONTRACT.md` 不宣称禁词。\n"
        "6. 输出格式：如需新建或更新脚本，请使用 ```python:build_pptx.py ... ``` 代码块包裹完整代码；如需更新大纲或讲稿，可使用 ```markdown:01-周报大纲.md ... ``` 与 ```markdown:02-逐页演讲稿.md ... ```。\n\n"
        f"## [技能规范：academic-ppt-studio]\n{skill_txt}\n\n"
        + (f"{extra_skill_txt}\n\n" if extra_skill_txt else "")
        + f"## [课题红线与不宣称清单：00_CONTRACT.md]\n{contract_txt}\n\n"
        + (f"## [周报写入规范]\n{readme_txt}\n" if readme_txt else "")
    )


def _call_chat_history(
    runtime: Dict[str, Any],
    system_prompt: str,
    messages: List[Dict[str, str]],
    stream: bool = True,
) -> Dict[str, Any]:
    if not runtime["api_key"]:
        raise RuntimeError("缺少 GLM_API_KEY，请先在宿主机 ~/.config/research_hub/.env 填入。")

    urls = consult._candidate_endpoints(runtime["base_url"], "chat")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {runtime['api_key']}",
        "User-Agent": "ResearchHub-PPTStudio/1.0",
    }
    full_msgs = [{"role": "system", "content": system_prompt}, *messages]
    effort = str(runtime.get("reasoning_effort") or "default").lower()

    include_effort = True
    include_stream_opts = True
    started = datetime.datetime.now()
    last_err: Optional[Exception] = None

    print(f"\n▶ 正在呼叫 [{runtime['role']}] | 模型: {runtime['model']} | 思考档位: {effort} ...", flush=True)

    for attempt in range(3):
        body: Dict[str, Any] = {
            "model": runtime["model"],
            "messages": full_msgs,
            "stream": stream,
        }
        if runtime.get("temperature") is not None:
            body["temperature"] = float(runtime["temperature"])
        if stream and include_stream_opts:
            body["stream_options"] = {"include_usage": True}
        if include_effort and effort != "default":
            if effort == "off":
                body["thinking"] = {"type": "disabled"}
            else:
                body["reasoning_effort"] = "high" if effort == "xhigh" else effort

        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for idx, url in enumerate(urls):
            req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
            try:
                req_timeout = max(int(runtime.get("timeout") or 180), 360)
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if stream and "application/json" not in ctype:
                        reasoning, text, usage = consult._stream_sse_response(
                            resp, "chat", runtime["role"], runtime["model"], live_print=True
                        )
                    else:
                        raw = resp.read().decode("utf-8", errors="replace")
                        if "text/event-stream" in ctype or raw.lstrip().startswith("data:"):
                            reasoning, text, usage = consult._stream_sse_response(
                                io.BytesIO(raw.encode("utf-8")), "chat", runtime["role"], runtime["model"], live_print=True
                            )
                        else:
                            parsed = json.loads(raw)
                            usage = parsed.get("usage") or {}
                            reasoning, text = consult._extract_chat_json_parts(parsed)
                            if reasoning:
                                print(f"\n🧠 思考过程:\n{reasoning}\n", flush=True)
                            if text:
                                print(f"\n💬 回复:\n{text}\n", flush=True)

                elapsed = round((datetime.datetime.now() - started).total_seconds(), 2)
                print(
                    f"✅ 完成 | 耗时: {elapsed}s | 档位: {effort} | Token: {consult._format_usage_line(usage)}",
                    flush=True,
                )
                return {"reasoning": reasoning, "text": text, "usage": usage, "elapsed_s": elapsed}
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:400]
                if e.code == 404 and idx + 1 < len(urls):
                    last_err = e
                    continue
                if e.code == 400 and attempt < 2:
                    lower_err = err_body.lower()
                    if include_stream_opts and "stream_options" in lower_err:
                        include_stream_opts = False
                        last_err = e
                        break
                    if include_effort and any(k in lower_err for k in ("reasoning_effort", "thinking", "unsupported", "unknown")):
                        include_effort = False
                        last_err = e
                        print("⚠️ 该接口不支持自定义思考参数，已自动切回默认档位重试...", flush=True)
                        break
                raise RuntimeError(f"HTTP {e.code}: {err_body}")
            except Exception as e:
                last_err = e
                break

    raise RuntimeError(f"请求失败: {last_err}")


def _extract_named_blocks(reply_text: str) -> Dict[str, str]:
    """Extract ```lang:filename ... ``` blocks or fallback python block for build_pptx.py."""
    blocks: Dict[str, str] = {}
    pattern = re.compile(r"```([a-zA-Z0-9_+-]+):([^\n`]+)\n(.*?)```", re.DOTALL)
    for _, fname, content in pattern.findall(reply_text):
        clean_name = Path(fname.strip()).name
        if clean_name in ("build_pptx.py", "01-周报大纲.md", "02-逐页演讲稿.md"):
            blocks[clean_name] = content.strip() + "\n"

    if "build_pptx.py" not in blocks:
        py_match = re.findall(r"```python\n(.*?)```", reply_text, re.DOTALL)
        if py_match:
            longest = max(py_match, key=len)
            if "Presentation" in longest or "pptx" in longest:
                blocks["build_pptx.py"] = longest.strip() + "\n"
    return blocks


def _build_and_check(work_dir: Path, pptx_path: Path, auto_confirm: bool = False) -> Tuple[bool, str]:
    build_py = work_dir / "build_pptx.py"
    if not build_py.is_file():
        return False, "未找到 build_pptx.py"

    if not auto_confirm:
        try:
            ans = input(f"\n⚠️ 即将以无密钥隔离环境运行 {build_py.name}，是否确认执行？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no"):
            print("已取消执行 build_pptx.py（脚本已保存，可审阅后手动运行）。")
            return False, "用户取消执行"

    # Strip environment secrets so generated script never sees ~/.config/research_hub/.env or cc-switch.
    import site
    user_site = site.getusersitepackages()
    py_paths = [p for p in [user_site, os.environ.get("PYTHONPATH", "")] if p]
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(work_dir),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "PYTHONPATH": os.pathsep.join(py_paths),
    }
    run_res = subprocess.run(
        [sys.executable, str(build_py), str(pptx_path)],
        cwd=str(work_dir),
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if run_res.returncode != 0:
        err_msg = (run_res.stdout or "") + "\n" + (run_res.stderr or "")
        print(f"\n❌ build_pptx.py 运行失败:\n{err_msg.strip()}")
        return False, err_msg.strip()

    if not pptx_path.is_file():
        return False, f"build_pptx.py 执行成功但未生成 {pptx_path.name}"

    print(f"\n📊 已生成可编辑 PPTX: {pptx_path}")
    if QUALITY_SCRIPT.is_file():
        q_res = subprocess.run(
            [sys.executable, str(QUALITY_SCRIPT), str(pptx_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(q_res.stdout.strip())
        if q_res.stderr.strip():
            print(q_res.stderr.strip())
        return q_res.returncode == 0, q_res.stdout.strip()
    return True, "未找到质检脚本，已跳过质检"


def _load_history(history_path: Path) -> List[Dict[str, str]]:
    if not history_path.is_file():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict) and "role" in m and "content" in m]
    except Exception:
        pass
    return []


def _save_history(history_path: Path, history: List[Dict[str, str]]) -> None:
    # Cap stored turns to avoid unbounded context growth while preserving recent edits.
    trimmed = history[-12:]
    history_path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _collect_workdir_briefings(work_dir: Path) -> str:
    """Read Claude-prepared markdown briefings/outlines inside work_dir (skipping huge raw logs if concise briefings exist)."""
    all_md = [p for p in work_dir.glob("*.md") if p.is_file()]
    has_concise = any("全记录" not in p.name for p in all_md)
    md_files = sorted(
        [p for p in all_md if not (has_concise and "全记录" in p.name)],
        key=lambda p: (0 if ("重做" in p.name or "提示词" in p.name or "简报" in p.name or "方案" in p.name) else 1, p.name),
    )
    if not md_files:
        return ""
    chunks: List[str] = []
    budget = 16000
    used = 0
    for p in md_files:
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        limit = 6500
        snippet = text[:limit] + ("\n...(已截断)" if len(text) > limit else "")
        block = f"### [{p.name}]\n{snippet}"
        if used + len(block) > budget and chunks:
            break
        chunks.append(block)
        used += len(block)
    return "\n\n".join(chunks)


def _compose_user_turn(
    project_dir: Path,
    work_dir: Path,
    instruction: str,
    is_first_turn: bool,
    extra_projects: Optional[List[Path]] = None,
) -> str:
    build_py = work_dir / "build_pptx.py"
    parts = [f"当前操作目录：{work_dir.name}"]
    if is_first_turn or not build_py.is_file():
        briefings = _collect_workdir_briefings(work_dir)
        if briefings:
            parts.append(f"## 本周报目录内已准备好的简报 / 大纲 / 讲稿（Claude 产出，请严格据此制作）\n{briefings}")
        state_path = project_dir / "_control" / "01_PROJECT_STATE.md"
        state_txt = state_path.read_text(encoding="utf-8")[:5000] if state_path.is_file() else ""
        parts.append(f"## 当前课题最新进展（01_PROJECT_STATE.md）\n{state_txt}")
        parts.append(f"## 当前课题真实图片与数据资产清单\n{_scan_project_assets(project_dir, work_dir)}")
        for ep in extra_projects or []:
            ep_state = ep / "_control" / "01_PROJECT_STATE.md"
            ep_txt = ep_state.read_text(encoding="utf-8")[:3000] if ep_state.is_file() else ""
            parts.append(f"## 联动课题 [{ep.name}] 进展与资产\n{ep_txt}\n\n{_scan_project_assets(ep, work_dir)}")
    if build_py.is_file():
        cur_code = build_py.read_text(encoding="utf-8")
        parts.append(f"## 当前已有的 build_pptx.py（请在此基础上做局部增量修改，保留未提及页面）\n```python\n{cur_code}\n```")
    parts.append(f"## 本轮修改/制作要求\n{instruction}")
    return "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ppt",
        description="GLM 多轮可编辑 PPT 引擎：支持周报六步实验流、跨课题联动、自定义 Skill 与文献汇报",
    )
    parser.add_argument("--project", help="指定课题目录名（默认使用 models.toml 的 active_project）")
    parser.add_argument("--extra-projects", default="", help="逗号分隔的联动课题名（如 P01,P02，用于跨课题综合汇报）")
    parser.add_argument(
        "--mode",
        choices=["weekly", "custom", "multi", "literature"],
        default="weekly",
        help="PPT 模式：weekly(周报实验六步流,默认) | custom(自由主题) | multi(多课题综合) | literature(文献汇报)",
    )
    parser.add_argument("--skill", action="append", default=[], help="额外加载 00_全局中枢_Hub/skills/<名称>/SKILL.md（可多次传入）")
    parser.add_argument("--topic", default="组会汇报", help="汇报主题名（自动创建 04_组会与周报_Weekly/YYYY-MM-DD_<主题>/）")
    parser.add_argument("--dir", dest="explicit_dir", help="继续迭代已有的周报子目录（位于 04_组会与周报_Weekly/ 下）")
    parser.add_argument("--new-folder", action="store_true", help="强制新建目录（默认 weekly 模式下同一周二～周日自动复用已有目录）")
    parser.add_argument("--model", help="覆盖 GLM 模型（默认 models.toml 中 glm_heavy = glm-5.3）")
    parser.add_argument(
        "--effort",
        choices=["off", "low", "medium", "high", "xhigh"],
        default="off",
        help="指定思考档位（默认 off，防止 65k 思考耗尽输出长度导致代码截断）",
    )
    parser.add_argument("--ask", help="单轮指令模式（传入后执行一轮迭代即退出；不传则进入交互式多轮对话）")
    parser.add_argument("--auto-fix", type=int, default=1, help="build_pptx.py 报错后自动回喂报错让 GLM 自修的次数（默认 1，设为 0 关闭）")
    parser.add_argument("--reset", action="store_true", help="归档旧 chat_history.json / build_pptx.py，基于当前最新简报与图片重新开始")
    parser.add_argument("--yes", action="store_true", help="跳过执行 build_pptx.py 前的确认提示")
    parser.add_argument("--dry-run", action="store_true", help="仅预览路径、规范注入与状态，不调用 API")
    args = parser.parse_args()

    config = consult.load_toml(HUB_DIR / "models.toml")
    env_path = Path(str(config.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()
    dotenv = consult.load_dotenv(env_path)
    project_dir = consult.resolve_project_dir(args.project, config)
    extra_project_dirs: List[Path] = []
    for raw_ep in [s.strip() for s in args.extra_projects.split(",") if s.strip()]:
        ep_dir = consult.resolve_project_dir(raw_ep, config)
        if ep_dir != project_dir and ep_dir not in extra_project_dirs:
            extra_project_dirs.append(ep_dir)

    runtime = consult.resolve_role_runtime("glm_heavy", config, dotenv, args.model, args.effort)
    work_dir = _resolve_work_dir(project_dir, args.topic, args.explicit_dir, mode=args.mode, new_folder=args.new_folder)

    pptx_name = f"{work_dir.name}.pptx"
    pptx_path = work_dir / pptx_name
    history_path = work_dir / "chat_history.json"
    if args.reset and (history_path.exists() or (work_dir / "build_pptx.py").exists()):
        bak_dir = work_dir / f"_prev_{datetime.datetime.now().strftime('%H%M%S')}"
        bak_dir.mkdir(parents=True, exist_ok=True)
        for fn in ("chat_history.json", "build_pptx.py"):
            fp = work_dir / fn
            if fp.is_file():
                shutil.move(str(fp), str(bak_dir / fn))
        print(f"🧹 已归档旧对话与脚本至 {bak_dir.name}/，将基于当前最新材料重新开始。")
    history = _load_history(history_path)
    system_prompt = _build_system_prompt(project_dir, work_dir, pptx_path, mode=args.mode, extra_skills=args.skill)

    print("============================================================")
    print("  Academic PPT Studio × GLM 多轮可编辑迭代引擎 (Track B)")
    print("============================================================")
    print(f"当前课题: {project_dir.name}" + (f" + 联动 {[p.name for p in extra_project_dirs]}" if extra_project_dirs else ""))
    print(f"运行模式: {args.mode}" + (f" | 附加 Skill: {args.skill}" if args.skill else ""))
    print(f"隔离目录: {work_dir}")
    print(f"目标 PPT: {pptx_path.name} ({'已存在，可继续增量迭代' if pptx_path.exists() else '将新建'})")
    print(f"模型配置: {runtime['model']} | 思考档位: {runtime['reasoning_effort']} | 端点: {runtime['base_url']}")

    if args.dry_run:
        print(f"系统规范注入: {len(system_prompt)} 字 (模式={args.mode})")
        print("[PPT Studio] --dry-run 预览完成，未发起外部请求。")
        return 0

    def _run_one_turn(instruction: str, auto_fix_left: Optional[int] = None) -> int:
        if auto_fix_left is None:
            auto_fix_left = max(0, int(args.auto_fix))
        user_msg = _compose_user_turn(
            project_dir, work_dir, instruction, is_first_turn=(len(history) == 0), extra_projects=extra_project_dirs
        )
        turn_msgs = [*history, {"role": "user", "content": user_msg}]
        res = _call_chat_history(runtime, system_prompt, turn_msgs, stream=True)
        full_text = (res.get("text") or "").strip()
        if not full_text:
            print("\n⚠️ GLM 本轮返回空正文（可能仅输出思考或被截断），未写入对话历史；请重试或简化提示。", file=sys.stderr)
            return 1

        # Auto-continue if a code fence was truncated mid-output
        for cont_idx in range(2):
            if full_text.count("```") % 2 == 0:
                break
            print(f"\n🔁 检测到输出在代码块中途截断，自动续接第 {cont_idx + 1} 次...", flush=True)
            tail = full_text[-600:]
            cont_msgs = [
                *turn_msgs,
                {"role": "assistant", "content": full_text},
                {
                    "role": "user",
                    "content": f"你的上一条输出在代码块中途被截断。请紧接下面断点继续输出剩余代码直到闭合 ``` 为止，不要重复前面已写的代码：\n...\n{tail}",
                },
            ]
            cont_res = _call_chat_history(runtime, system_prompt, cont_msgs, stream=True)
            chunk = (cont_res.get("text") or "").lstrip("\n")
            if not chunk.strip():
                break
            full_text = full_text + "\n" + chunk

        # Keep history compact: strip any embedded full code/briefing echo, store concise instruction + summary
        reply_summary = re.sub(r"```[\s\S]*?```", "[已输出代码块并写入本地文件]", full_text).strip()
        if len(reply_summary) > 1500:
            reply_summary = reply_summary[:1500] + "\n...(已折叠)"
        history.append({"role": "user", "content": instruction})
        history.append({"role": "assistant", "content": reply_summary or "[已生成脚本]"})
        _save_history(history_path, history)

        blocks = _extract_named_blocks(full_text)
        if not blocks:
            print("\n⚠️ 本轮回复未包含 ```python:build_pptx.py``` 代码块，未修改本地文件。")
            return 0

        for fname, content in blocks.items():
            target_f = (work_dir / fname).resolve()
            if not _is_within(target_f, work_dir):
                raise PermissionError(f"禁止越权写入：{target_f}")
            if fname == "build_pptx.py" and target_f.is_file():
                shutil.copy2(target_f, work_dir / "build_pptx.bak.py")
            target_f.write_text(content, encoding="utf-8")
            print(f"💾 已写入: {target_f}")

        if "build_pptx.py" in blocks:
            ok, err_detail = _build_and_check(work_dir, pptx_path, auto_confirm=args.yes)
            if not ok:
                if err_detail != "用户取消执行" and auto_fix_left > 0:
                    print(f"\n🛠️ 检测到运行/质检未通过，自动把报错回喂 GLM 进行自修（剩余自修次数 {auto_fix_left}）...", flush=True)
                    fix_prompt = (
                        f"刚才生成的 `build_pptx.py` 执行或质检未通过，输出如下：\n```\n{err_detail[-2000:]}\n```\n"
                        "请直接定位原因（例如列名、数据类型、坐标或禁用词）并输出修复后的完整 `build_pptx.py`。"
                    )
                    return _run_one_turn(fix_prompt, auto_fix_left=auto_fix_left - 1)
                print("💡 提示：可直接输入修改要求（例如“修复上面的报错”），GLM 会基于当前 build_pptx.py 继续修复。")
        return 0

    if args.ask:
        return _run_one_turn(args.ask)

    print("\n输入你的制作或修改要求（例如：'生成8页本周进展PPT' 或 '第4页拆成左右两栏，其他页不动'）。")
    print("快捷命令: 'pb' 直接提交剪贴板多行文本(防Ghostty粘贴截断) | 'ml' 进入多行输入(单独一行 END 结束) | 'open' 打开PPT | 'q' 退出\n")
    while True:
        try:
            prompt = input("PPT> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break
        if not prompt:
            continue
        if prompt.lower() in ("q", "quit", "exit"):
            break
        if prompt.lower() == "open":
            if pptx_path.is_file():
                subprocess.run(["open", str(pptx_path)], check=False)
            else:
                print("尚未生成 .pptx 文件。")
            continue
        if prompt.lower() == "pb":
            pb_res = subprocess.run(["pbpaste"], capture_output=True, text=True)
            prompt = (pb_res.stdout or "").strip()
            if not prompt:
                print("⚠️ 剪贴板为空。")
                continue
            print(f"📋 已从剪贴板读取 {len(prompt)} 字，正在提交...")
        elif prompt.lower() == "ml":
            print("📝 多行模式：请粘贴内容，完成后在新起一行输入 END 回车提交：")
            ml_lines: List[str] = []
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() == "END":
                    break
                ml_lines.append(line)
            prompt = "\n".join(ml_lines).strip()
            if not prompt:
                continue
        try:
            _run_one_turn(prompt)
        except Exception as err:
            print(f"\n❌ 本轮执行出错: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
