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


def _resolve_work_dir(project_dir: Path, topic: Optional[str], explicit_dir: Optional[str]) -> Path:
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

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    slug = _sanitize_slug(topic or "组会汇报")
    folder_name = slug if re.match(r"^\d{4}-\d{2}-\d{2}_", slug) else f"{today}_{slug}"
    work_dir = (weekly_root / folder_name).resolve()
    if not _is_within(work_dir, weekly_root.resolve()):
        raise PermissionError(f"非法输出目录：{work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _build_system_prompt(project_dir: Path, work_dir: Path, pptx_path: Path) -> str:
    skill_txt = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.is_file() else ""
    contract_path = project_dir / "_control" / "00_CONTRACT.md"
    contract_txt = contract_path.read_text(encoding="utf-8")[:4000] if contract_path.is_file() else ""
    weekly_readme = project_dir / "04_组会与周报_Weekly" / "README_周报写入规范.md"
    readme_txt = weekly_readme.read_text(encoding="utf-8")[:2000] if weekly_readme.is_file() else ""

    return (
        "你是本科研课题的 Track B 组会汇报与可编辑 PPT 制作专家。\n"
        "【硬性要求】\n"
        "1. 必须严格产出**原生可编辑的 `python-pptx` 脚本**（全部使用原生文本框、原生表格、矢量形状），严禁生成整页图片 PPT。\n"
        f"2. 脚本文件名固定为 `build_pptx.py`，接收命令行第 1 个参数（`sys.argv[1]`）作为输出 `.pptx` 路径，默认回退为 `{pptx_path.name}`。\n"
        "3. 当用户在后续轮次提出局部修改（例如只改第 3 页或第 5 页表格）时，**必须保留其余未提及页面的现有结构与内容**，只精准改写目标页并输出完整可运行的 `build_pptx.py`。\n"
        "4. 必须严格遵守下方 `academic-ppt-studio` 设计规范（13.333x7.5 宽屏、海军蓝/青绿配色、字体写入 `a:ea` 与 `a:cs`）以及课题 `00_CONTRACT.md` 不宣称禁词。\n"
        "5. 输出格式：如需新建或更新脚本，请使用 ```python:build_pptx.py ... ``` 代码块包裹完整代码；如需更新大纲或讲稿，可使用 ```markdown:01-周报大纲.md ... ``` 与 ```markdown:02-逐页演讲稿.md ... ```。\n\n"
        f"## [技能规范：academic-ppt-studio]\n{skill_txt}\n\n"
        f"## [课题红线与不宣称清单：00_CONTRACT.md]\n{contract_txt}\n\n"
        f"## [周报写入规范]\n{readme_txt}\n"
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
                with urllib.request.urlopen(req, timeout=runtime["timeout"]) as resp:
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
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(work_dir),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
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


def _compose_user_turn(project_dir: Path, work_dir: Path, instruction: str, is_first_turn: bool) -> str:
    build_py = work_dir / "build_pptx.py"
    parts = [f"当前操作目录：{work_dir.name}"]
    if is_first_turn:
        state_path = project_dir / "_control" / "01_PROJECT_STATE.md"
        state_txt = state_path.read_text(encoding="utf-8")[:5000] if state_path.is_file() else ""
        parts.append(f"## 当前课题最新进展（01_PROJECT_STATE.md）\n{state_txt}")
    if build_py.is_file():
        cur_code = build_py.read_text(encoding="utf-8")
        parts.append(f"## 当前已有的 build_pptx.py（请在此基础上做局部增量修改，保留未提及页面）\n```python\n{cur_code}\n```")
    parts.append(f"## 本轮修改/制作要求\n{instruction}")
    return "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ppt",
        description="GLM 多轮可编辑 PPT 引擎：自动调用 academic-ppt-studio，在同一份 build_pptx.py/.pptx 上对话迭代并自动质检",
    )
    parser.add_argument("--project", help="指定课题目录名（默认使用 models.toml 的 active_project）")
    parser.add_argument("--topic", default="组会汇报", help="汇报主题名（自动创建 04_组会与周报_Weekly/YYYY-MM-DD_<主题>/）")
    parser.add_argument("--dir", dest="explicit_dir", help="继续迭代已有的周报子目录（位于 04_组会与周报_Weekly/ 下）")
    parser.add_argument("--model", help="覆盖 GLM 模型（默认 models.toml 中 glm_heavy = glm-5.3）")
    parser.add_argument("--effort", choices=["off", "low", "medium", "high", "xhigh"], help="指定思考档位")
    parser.add_argument("--ask", help="单轮指令模式（传入后执行一轮迭代即退出；不传则进入交互式多轮对话）")
    parser.add_argument("--yes", action="store_true", help="跳过执行 build_pptx.py 前的确认提示")
    parser.add_argument("--dry-run", action="store_true", help="仅预览路径、规范注入与状态，不调用 API")
    args = parser.parse_args()

    config = consult.load_toml(HUB_DIR / "models.toml")
    env_path = Path(str(config.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()
    dotenv = consult.load_dotenv(env_path)
    project_dir = consult.resolve_project_dir(args.project, config)
    runtime = consult.resolve_role_runtime("glm_heavy", config, dotenv, args.model, args.effort)
    work_dir = _resolve_work_dir(project_dir, args.topic, args.explicit_dir)

    pptx_name = f"{work_dir.name}.pptx"
    pptx_path = work_dir / pptx_name
    history_path = work_dir / "chat_history.json"
    history = _load_history(history_path)
    system_prompt = _build_system_prompt(project_dir, work_dir, pptx_path)

    print("============================================================")
    print("  Academic PPT Studio × GLM 多轮可编辑迭代引擎 (Track B)")
    print("============================================================")
    print(f"当前课题: {project_dir.name}")
    print(f"隔离目录: {work_dir}")
    print(f"目标 PPT: {pptx_path.name} ({'已存在，可继续增量迭代' if pptx_path.exists() else '将新建'})")
    print(f"模型配置: {runtime['model']} | 思考档位: {runtime['reasoning_effort']} | 端点: {runtime['base_url']}")

    if args.dry_run:
        print(f"系统规范注入: {len(system_prompt)} 字 (含 academic-ppt-studio + 00_CONTRACT)")
        print("[PPT Studio] --dry-run 预览完成，未发起外部请求。")
        return 0

    def _run_one_turn(instruction: str) -> int:
        user_msg = _compose_user_turn(project_dir, work_dir, instruction, is_first_turn=(len(history) == 0))
        turn_msgs = [*history, {"role": "user", "content": user_msg}]
        res = _call_chat_history(runtime, system_prompt, turn_msgs, stream=True)
        history.append({"role": "user", "content": instruction})
        history.append({"role": "assistant", "content": res["text"]})
        _save_history(history_path, history)

        blocks = _extract_named_blocks(res["text"])
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
            ok, _ = _build_and_check(work_dir, pptx_path, auto_confirm=args.yes)
            if not ok:
                print("💡 提示：可直接输入修改要求（例如“修复上面的报错”），GLM 会基于当前 build_pptx.py 继续修复。")
        return 0

    if args.ask:
        return _run_one_turn(args.ask)

    print("\n输入你的制作或修改要求（例如：'生成8页本周进展PPT' 或 '第4页拆成左右两栏，其他页不动'）。")
    print("输入 'q' / 'exit' 退出，'open' 用本机默认应用打开当前 PPTX。\n")
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
        try:
            _run_one_turn(prompt)
        except Exception as err:
            print(f"\n❌ 本轮执行出错: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
