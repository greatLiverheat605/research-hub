#!/usr/bin/env python3
"""
00_全局中枢_Hub/consult.py
宿主机 Pebrel 终端手动点火会诊脚本：
- 自动识别当前课题目录（或通过 --project 指定 / 回退至 models.toml 中的 active_project）
- 读取课题的 _control/00_CONTRACT.md、_control/02_DECISION_LEDGER.md 与 _bus/ask.md
- 按 models.toml 中可热插拔的模型配置，调用 DeepSeek / GPT(cc-switch) / GLM API
- 将带时间戳与 Token 统计的专家意见写入 _bus/reply.md，并归档至 _bus/archive/

零第三方依赖，兼容 macOS 系统原生 Python 3.9+。
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


HUB_DIR = Path(__file__).resolve().parent
EXCHANGE_ROOT = Path(os.environ.get("RH_EXCHANGE_ROOT", HUB_DIR.parent))
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"


# ---------------------------------------------------------------------------
# 1. Pure-Python TOML Loader (compatible with Python 3.9+)
# ---------------------------------------------------------------------------

def _strip_inline_comment(line: str) -> str:
    """Remove trailing TOML comment outside of quotes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:idx].rstrip()
    return line.rstrip()


def _unescape_toml_basic_string(inner: str) -> str:
    """Unescape TOML basic string escapes without mangling non-ASCII UTF-8 characters."""
    escape_map = {
        "\\": "\\",
        '"': '"',
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
    }

    def _replace(match: re.Match) -> str:
        seq = match.group(1)
        if seq in escape_map:
            return escape_map[seq]
        if seq.startswith("u") and len(seq) == 5:
            return chr(int(seq[1:], 16))
        if seq.startswith("U") and len(seq) == 9:
            return chr(int(seq[1:], 16))
        return match.group(0)

    return re.sub(r'\\(["\\nrtbf]|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})', _replace, inner)


def _parse_toml_scalar(raw: str) -> Any:
    """Parse a single-line TOML scalar value."""
    val = raw.strip()
    if val == "true":
        return True
    if val == "false":
        return False
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        inner = val[1:-1]
        if val.startswith('"'):
            return _unescape_toml_basic_string(inner)
        return inner
    if re.fullmatch(r"[+-]?\d+", val):
        return int(val)
    if re.fullmatch(r"[+-]?\d+\.\d+", val):
        return float(val)
    return val


def load_toml(path: Path) -> Dict[str, Any]:
    """Load TOML using stdlib tomllib (3.11+) or a lightweight built-in parser (3.9+)."""
    try:
        import tomllib  # type: ignore
        with path.open("rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass

    data: Dict[str, Any] = {}
    current = data
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()
        i += 1
        if not raw_line or raw_line.startswith("#"):
            continue
        if raw_line.startswith("[") and raw_line.endswith("]"):
            sec_path = [p.strip() for p in raw_line[1:-1].split(".") if p.strip()]
            target = data
            for part in sec_path:
                target = target.setdefault(part, {})
            current = target
            continue
        if "=" not in raw_line:
            continue
        key, rhs = raw_line.split("=", 1)
        key = key.strip()
        rhs = rhs.strip()

        # Handle multiline triple-quoted strings (""" or ''')
        for delim in ('"""', "'''"):
            if rhs.startswith(delim):
                after = rhs[3:]
                if delim in after:
                    end_idx = after.index(delim)
                    current[key] = after[:end_idx].strip("\n")
                    break
                buf = [after] if after else []
                while i < len(lines):
                    next_line = lines[i]
                    i += 1
                    if delim in next_line:
                        end_idx = next_line.index(delim)
                        buf.append(next_line[:end_idx])
                        break
                    buf.append(next_line)
                current[key] = "\n".join(buf).strip("\n")
                break
        else:
            current[key] = _parse_toml_scalar(_strip_inline_comment(rhs))

    return data


# ---------------------------------------------------------------------------
# 2. Environment & cc-switch Credential Resolution (Host-side Only)
# ---------------------------------------------------------------------------

def load_dotenv(env_path: Path) -> Dict[str, str]:
    """Load simple KEY=VALUE lines from host-only .env file without polluting stdout."""
    loaded: Dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if v:
            loaded[k] = v
    return loaded


def resolve_cc_switch_active(db_path: Path, app_type: str = "codex") -> Dict[str, Optional[str]]:
    """Read active provider metadata & key in memory from local cc-switch SQLite DB."""
    info: Dict[str, Optional[str]] = {
        "provider_name": None,
        "base_url": None,
        "api_key": None,
        "model": None,
        "wire_api": None,
        "reasoning_effort": None,
    }
    if not db_path.exists():
        return info
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT name, settings_config FROM providers WHERE is_current=1 AND app_type=? LIMIT 1",
            (app_type,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return info

    if not row:
        return info
    name, cfg_raw = row
    info["provider_name"] = name
    try:
        cfg = json.loads(cfg_raw or "{}")
    except json.JSONDecodeError:
        return info

    auth = cfg.get("auth") or {}
    if isinstance(auth, dict) and auth.get("OPENAI_API_KEY"):
        info["api_key"] = str(auth["OPENAI_API_KEY"]).strip()

    toml_str = cfg.get("config") or ""
    if isinstance(toml_str, str):
        m_model = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', toml_str)
        m_base = re.search(r'(?m)^\s*base_url\s*=\s*"([^"]+)"', toml_str)
        m_wire = re.search(r'(?m)^\s*wire_api\s*=\s*"([^"]+)"', toml_str)
        m_eff = re.search(r'(?m)^\s*model_reasoning_effort\s*=\s*"([^"]+)"', toml_str)
        if m_model:
            info["model"] = m_model.group(1)
        if m_base:
            info["base_url"] = m_base.group(1)
        if m_wire:
            info["wire_api"] = m_wire.group(1)
        if m_eff:
            info["reasoning_effort"] = m_eff.group(1)
    return info


# ---------------------------------------------------------------------------
# 3. Project & Context Resolution
# ---------------------------------------------------------------------------

def resolve_project_dir(project_arg: Optional[str], config: Dict[str, Any]) -> Path:
    """Resolve target project directory from --project, cwd, or active_project."""
    if project_arg:
        candidate = Path(project_arg).expanduser()
        if candidate.is_dir() and (candidate / "_bus").is_dir():
            return candidate.resolve()
        under_projects = PROJECTS_DIR / project_arg
        if under_projects.is_dir():
            return under_projects.resolve()
        raise FileNotFoundError(f"找不到指定课题目录：{project_arg}（应在 {PROJECTS_DIR} 下）")

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if parent.parent == PROJECTS_DIR and (parent / "_bus").is_dir():
            return parent

    active = config.get("general", {}).get("active_project", "")
    if active:
        fallback = PROJECTS_DIR / active
        if fallback.is_dir():
            return fallback.resolve()

    raise FileNotFoundError(
        f"无法自动识别当前课题目录，且默认 active_project='{active}' 不存在。请使用 --project 指定课题名。"
    )


def _read_truncated(path: Path, limit: int) -> str:
    """Read UTF-8 text safely with character cap."""
    if not path.exists():
        return f"（未找到文件：{path.name}）"
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > limit:
        return text[:limit] + f"\n\n...（后文已截断，原长 {len(text)} 字符）"
    return text


def _read_ledger_tail(path: Path, limit: int) -> str:
    """Preserve table header and keep the newest trailing decision rows within limit."""
    if not path.exists():
        return f"（未找到文件：{path.name}）"
    text = path.read_text(encoding="utf-8").strip()
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    header_end = 0
    for idx, line in enumerate(lines[:25]):
        if line.strip().startswith("|") and "---" in line:
            header_end = idx + 1
            break
    header = "\n".join(lines[:header_end]) if header_end else ""
    budget = max(500, limit - len(header) - 80)
    tail_part = text[-budget:]
    if "\n" in tail_part:
        tail_part = tail_part.split("\n", 1)[1]
    return f"{header}\n\n...（中间较早台账已省略，以下为最新决策记录）\n\n{tail_part}"


def build_user_payload(project_dir: Path, config: Dict[str, Any], pivot: bool = False) -> Tuple[str, Dict[str, int]]:
    """Build combined prompt from 00_CONTRACT, 02_DECISION_LEDGER (unless pivot=True), and _bus/ask.md."""
    general = config.get("general", {})
    max_contract = int(general.get("max_contract_chars", 6000))
    max_ledger = int(general.get("max_ledger_chars", 6000))

    contract_path = project_dir / "_control" / "00_CONTRACT.md"
    ledger_path = project_dir / "_control" / "02_DECISION_LEDGER.md"
    ask_path = project_dir / "_bus" / "ask.md"

    if not ask_path.exists():
        raise FileNotFoundError(f"未找到会诊请求单：{ask_path}")

    contract_txt = _read_truncated(contract_path, max_contract)
    ledger_txt = "" if pivot else _read_ledger_tail(ledger_path, max_ledger)
    ask_txt = ask_path.read_text(encoding="utf-8").strip()

    if pivot:
        combined = (
            f"# 课题名称：{project_dir.name}（🚨 方向破局与去锚定重审模式 --pivot）\n\n"
            f"## [破局会诊最高准则：解除历史台账锁定]\n"
            f"当前课题可能陷入了「在既有假设上不断打数学/工程补丁的死循环（Patchwork Loop）」。\n"
            f"本次会诊已**刻意屏蔽历史决策台账 (`02_DECISION_LEDGER.md`)**，以免你被过去的沉没成本锚定。\n"
            f"请完全从第一性原理出发，严格按以下三部分结构回答：\n"
            f"1. **【黑话与术语白话对照表】**：把当前问题中涉及的所有内部代号、检验门禁或数学缩写，列成三列表格：`术语/代号 | 一句话人话含义（日常类比） | 它现在为什么卡住了/是不是伪需求`。\n"
            f"2. **【死循环病根诊断】**：直白指出当前方向是不是把简单问题复杂化了？哪些约束是作茧自缚？\n"
            f"3. **【Kill / Simplify / Pivot 三选一拍板卡】**：\n"
            f"   - **方案 A（Simplify 降维减法）**：保留核心科学问题，直接砍掉哪些繁琐假设/门禁，退回什么最简可跑通形态？\n"
            f"   - **方案 B（Pivot 换方向/换故事）**：利用现有数据和代码资产，转向哪个更容易发文章、逻辑更顺的科学故事？\n"
            f"   - **方案 C（Hard Stop 最后一搏）**：如果再试最后一次当前路线，设什么「一票否决指标（Kill Signal）」一旦不过就彻底放弃？\n\n"
            f"## [参考背景] 原立项契约 (00_CONTRACT.md，允许批评与修改)\n{contract_txt}\n\n"
            f"## [本次核心破局请求] (_bus/ask.md)\n{ask_txt}\n"
        )
    else:
        combined = (
            f"# 课题名称：{project_dir.name}\n\n"
            f"## [参考约束一] 科研红线契约 (00_CONTRACT.md)\n{contract_txt}\n\n"
            f"## [参考约束二] 已冻结决策台账 (02_DECISION_LEDGER.md)\n{ledger_txt}\n\n"
            f"## [本次核心会诊请求] (_bus/ask.md)\n{ask_txt}\n"
        )
    stats = {
        "contract_chars": len(contract_txt),
        "ledger_chars": len(ledger_txt),
        "ask_chars": len(ask_txt),
        "total_chars": len(combined),
    }
    return combined, stats


# ---------------------------------------------------------------------------
# 4. Role Configuration & HTTP Calling (Chat Completions + Responses API)
# ---------------------------------------------------------------------------

def resolve_role_runtime(
    role_name: str,
    config: Dict[str, Any],
    dotenv: Dict[str, str],
    model_override: Optional[str],
    effort_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge models.toml role settings, host .env, optional cc-switch active profile, and CLI overrides."""
    roles = config.get("roles", {})
    if role_name not in roles:
        available = ", ".join(roles.keys())
        raise KeyError(f"未知角色 '{role_name}'。可用角色：{available}")

    rcfg = dict(roles[role_name])
    general = config.get("general", {})

    key_env = str(rcfg.get("api_key_env", "")).strip()
    api_key = os.environ.get(key_env) or dotenv.get(key_env) or ""
    base_url = str(rcfg.get("base_url", "")).strip()
    wire_api = str(rcfg.get("wire_api", "chat")).strip()
    model = str(rcfg.get("model", "")).strip()
    effort = rcfg.get("reasoning_effort")
    key_source = f"env:{key_env}" if api_key else "MISSING"

    base_url_override_env = {
        "advisor": "OPENAI_BASE_URL",
        "redteam": "DEEPSEEK_BASE_URL",
        "glm_heavy": "GLM_BASE_URL",
        "glm_fast": "GLM_BASE_URL",
    }.get(role_name)
    if base_url_override_env:
        override_val = os.environ.get(base_url_override_env) or dotenv.get(base_url_override_env)
        if override_val and override_val.strip():
            base_url = override_val.strip()

    if rcfg.get("use_cc_switch_active") and not api_key:
        db_path = Path(str(general.get("cc_switch_db", "~/.cc-switch/cc-switch.db"))).expanduser()
        cc_app = str(rcfg.get("cc_switch_app_type", "codex"))
        cc = resolve_cc_switch_active(db_path, cc_app)
        if cc.get("api_key"):
            api_key = str(cc["api_key"])
            key_source = f"cc-switch:{cc.get('provider_name') or cc_app}"
            if cc.get("base_url"):
                base_url = str(cc["base_url"])
            if cc.get("wire_api"):
                wire_api = str(cc["wire_api"])
            if (not model or model == "auto") and cc.get("model"):
                model = str(cc["model"])
            if not effort and cc.get("reasoning_effort"):
                effort = str(cc["reasoning_effort"])

    model_source = "toml"
    if model == "auto":
        model = "gpt-6.2-astra"
        model_source = "fallback(cc-switch未返回model)"
    elif rcfg.get("use_cc_switch_active") and key_source.startswith("cc-switch:"):
        model_source = "cc-switch"
    if model_override:
        model = model_override.strip()
        model_source = "cli-override"
    if effort_override:
        effort = effort_override.strip().lower()
    if not effort:
        effort = "default"

    return {
        "role": role_name,
        "description": rcfg.get("description", role_name),
        "provider": rcfg.get("provider", "custom"),
        "wire_api": wire_api,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "model_source": model_source,
        "api_key": api_key,
        "key_source": key_source,
        "temperature": rcfg.get("temperature"),
        "reasoning_effort": effort,
        "system_prompt": str(rcfg.get("system_prompt", "")).strip(),
        "timeout": int(general.get("timeout_seconds", 180)),
    }


def _candidate_endpoints(base_url: str, wire_api: str) -> List[str]:
    """Generate ordered endpoint URLs, preferring /responses for Codex relays and /v1 for chat."""
    clean = base_url.rstrip("/")
    suffix = "/responses" if wire_api == "responses" else "/chat/completions"
    if clean.endswith(suffix):
        return [clean]
    if re.search(r"/v\d+$", clean):
        return [clean + suffix]
    parsed_path = urllib.parse.urlsplit(clean).path.rstrip("/")
    if not parsed_path:
        if wire_api == "responses":
            return [clean + suffix, clean + "/v1" + suffix]
        return [clean + "/v1" + suffix, clean + suffix]
    return [clean + suffix, clean + "/v1" + suffix]


def _extract_sse_response(raw_text: str, wire_api: str) -> Tuple[str, Dict[str, Any]]:
    """Parse Server-Sent Events stream if a relay responds with SSE chunks."""
    chunks: List[str] = []
    final_usage: Dict[str, Any] = {}
    for line in raw_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload_str = line[5:].strip()
        if not payload_str or payload_str == "[DONE]":
            continue
        try:
            evt = json.loads(payload_str)
        except json.JSONDecodeError:
            continue
        if wire_api == "responses":
            etype = evt.get("type", "")
            if etype == "response.output_text.delta" and isinstance(evt.get("delta"), str):
                chunks.append(evt["delta"])
            elif etype == "response.completed":
                resp_obj = evt.get("response") or {}
                final_usage = resp_obj.get("usage") or {}
                if not chunks:
                    text = _extract_responses_json_text(resp_obj)
                    if text:
                        chunks.append(text)
        else:
            choices = evt.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if isinstance(delta.get("content"), str):
                    chunks.append(delta["content"])
            if evt.get("usage"):
                final_usage = evt["usage"]
    return "".join(chunks).strip(), final_usage


def _extract_responses_json_text(obj: Dict[str, Any]) -> str:
    """Extract assistant output text from OpenAI Responses API JSON object."""
    if isinstance(obj.get("output_text"), str) and obj["output_text"].strip():
        return obj["output_text"].strip()
    parts: List[str] = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(str(c["text"]))
    return "\n".join(parts).strip()


def _extract_chat_json_parts(obj: Dict[str, Any]) -> Tuple[str, str]:
    """Extract (reasoning, content) from Chat Completions JSON."""
    choices = obj.get("choices") or []
    if not choices:
        return "", json.dumps(obj, ensure_ascii=False, indent=2)
    msg = choices[0].get("message") or {}
    content = str(msg.get("content") or "").strip()
    reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return reasoning, content


def _extract_responses_reasoning_text(obj: Dict[str, Any]) -> str:
    """Extract reasoning summary text from OpenAI Responses API JSON object."""
    parts: List[str] = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    parts.append(str(s["text"]))
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    parts.append(str(c["text"]))
    return "\n".join(parts).strip()


def _format_usage_line(usage: Dict[str, Any]) -> str:
    if not usage:
        return "无用量回传"
    keys_order = [
        ("prompt_tokens", "输入"),
        ("input_tokens", "输入"),
        ("completion_tokens", "输出"),
        ("output_tokens", "输出"),
        ("total_tokens", "总计"),
    ]
    shown = []
    seen_labels = set()
    for k, label in keys_order:
        v = usage.get(k)
        if isinstance(v, (int, float)) and label not in seen_labels:
            shown.append(f"{label}={int(v)}")
            seen_labels.add(label)
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
        shown.append(f"思考={int(details['reasoning_tokens'])}")
    if not shown:
        shown = [f"{k}={v}" for k, v in usage.items() if isinstance(v, (int, float))]
    return ", ".join(shown) if shown else "无用量回传"


def _build_request_body(runtime: Dict[str, Any], user_prompt: str, stream: bool, include_effort: bool = True, include_stream_opts: bool = True) -> Dict[str, Any]:
    wire_api = runtime["wire_api"]
    effort = str(runtime.get("reasoning_effort") or "default").lower()

    if wire_api == "responses":
        body: Dict[str, Any] = {
            "model": runtime["model"],
            "instructions": runtime["system_prompt"],
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}],
            "store": False,
            "stream": True,
        }
        if include_effort and effort not in ("default", "off"):
            wire_effort = "max" if effort == "ultra" else effort
            body["reasoning"] = {"effort": wire_effort}
        return body

    body = {
        "model": runtime["model"],
        "messages": [
            {"role": "system", "content": runtime["system_prompt"]},
            {"role": "user", "content": user_prompt},
        ],
        "stream": stream,
    }
    if stream and include_stream_opts:
        body["stream_options"] = {"include_usage": True}
    if runtime.get("temperature") is not None:
        body["temperature"] = float(runtime["temperature"])
    if include_effort and effort != "default":
        if effort == "off":
            body["thinking"] = {"type": "disabled"}
        else:
            body["reasoning_effort"] = "high" if effort == "xhigh" else effort
    return body


def _stream_sse_response(
    resp: Any,
    wire_api: str,
    role_label: str,
    model_name: str,
    live_print: bool,
) -> Tuple[str, str, Dict[str, Any]]:
    """Read SSE stream line-by-line, printing thinking and answer deltas in real time."""
    reasoning_parts: List[str] = []
    text_parts: List[str] = []
    usage: Dict[str, Any] = {}
    phase = None
    is_tty = sys.stdout.isatty()

    def _emit_phase(new_phase: str) -> None:
        nonlocal phase
        if phase == new_phase:
            return
        if live_print:
            if phase is not None:
                sys.stdout.write("\n")
            if new_phase == "thinking":
                hdr = f"\n🧠 [{role_label} | {model_name}] 思考过程:\n"
                sys.stdout.write(f"\033[2;36m{hdr}\033[0m" if is_tty else hdr)
            else:
                hdr = f"\n💬 [{role_label} | {model_name}] 正式回复:\n"
                sys.stdout.write(f"\033[1;32m{hdr}\033[0m" if is_tty else hdr)
            sys.stdout.flush()
        phase = new_phase

    def _emit_delta(kind: str, delta: str) -> None:
        if not delta:
            return
        _emit_phase(kind)
        if kind == "thinking":
            reasoning_parts.append(delta)
        else:
            text_parts.append(delta)
        if live_print:
            if kind == "thinking" and is_tty:
                sys.stdout.write(f"\033[2m{delta}\033[0m")
            else:
                sys.stdout.write(delta)
            sys.stdout.flush()

    while True:
        raw_line = resp.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload_str = line[5:].strip()
        if not payload_str or payload_str == "[DONE]":
            continue
        try:
            evt = json.loads(payload_str)
        except json.JSONDecodeError:
            continue

        if wire_api == "responses":
            etype = evt.get("type", "")
            if etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") and isinstance(evt.get("delta"), str):
                _emit_delta("thinking", evt["delta"])
            elif etype == "response.output_text.delta" and isinstance(evt.get("delta"), str):
                _emit_delta("answer", evt["delta"])
            elif etype == "response.completed":
                resp_obj = evt.get("response") or {}
                usage = resp_obj.get("usage") or usage
                if not reasoning_parts:
                    r_txt = _extract_responses_reasoning_text(resp_obj)
                    if r_txt:
                        _emit_delta("thinking", r_txt)
                if not text_parts:
                    a_txt = _extract_responses_json_text(resp_obj)
                    if a_txt:
                        _emit_delta("answer", a_txt)
        else:
            choices = evt.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                r_chunk = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(r_chunk, str) and r_chunk:
                    _emit_delta("thinking", r_chunk)
                if isinstance(delta.get("content"), str) and delta["content"]:
                    _emit_delta("answer", delta["content"])
            if evt.get("usage"):
                usage = evt["usage"]

    if live_print and phase is not None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    return "".join(reasoning_parts).strip(), "".join(text_parts).strip(), usage


def validate_allowed_host(base_url: str) -> None:
    """Enforce host-only ~/.config/research_hub/allowed_hosts before transmitting API keys."""
    wl_path = Path.home() / ".config" / "research_hub" / "allowed_hosts"
    if not wl_path.is_file():
        return
    allowed = {
        line.strip().lower()
        for line in wl_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    if host not in allowed:
        raise PermissionError(
            f"安全拦截：目标域名 '{host}' 不在宿主机白名单 {wl_path} 中，已拒绝发送密钥。"
        )


def call_role_api(runtime: Dict[str, Any], user_prompt: str, stream: bool = True) -> Dict[str, Any]:
    """Execute a consultation API call (streaming SSE by default) and return response + metrics."""
    if not runtime["api_key"]:
        raise RuntimeError(
            f"角色 [{runtime['role']}] 缺少 API Key（请填写 ~/.config/research_hub/.env 中的 "
            f"{runtime.get('key_source')} 或确认本机 cc-switch 状态）。"
        )
    validate_allowed_host(runtime["base_url"])

    wire_api = runtime["wire_api"]
    urls = _candidate_endpoints(runtime["base_url"], wire_api)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {runtime['api_key']}",
        "User-Agent": "ResearchHub-Consult/2.0",
    }

    if stream:
        print(f"\n▶ 正在呼叫 [{runtime['role']}] | 模型: {runtime['model']} | 思考档位: {runtime['reasoning_effort']} ...", flush=True)

    started = datetime.datetime.now()
    last_err: Optional[Exception] = None
    include_effort = True
    include_stream_opts = True

    for attempt in range(3):
        body = _build_request_body(runtime, user_prompt, stream=stream, include_effort=include_effort, include_stream_opts=include_stream_opts)
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")

        for idx, url in enumerate(urls):
            req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=runtime["timeout"]) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if stream and ("text/event-stream" in ctype or wire_api == "chat" or wire_api == "responses"):
                        if "application/json" in ctype:
                            raw = resp.read().decode("utf-8", errors="replace")
                            parsed = json.loads(raw)
                            usage = parsed.get("usage") or {}
                            if wire_api == "responses":
                                reasoning = _extract_responses_reasoning_text(parsed)
                                text = _extract_responses_json_text(parsed)
                            else:
                                reasoning, text = _extract_chat_json_parts(parsed)
                            if reasoning:
                                print(f"\n🧠 [{runtime['role']} | {runtime['model']}] 思考过程:\n{reasoning}\n", flush=True)
                            if text:
                                print(f"\n💬 [{runtime['role']} | {runtime['model']}] 正式回复:\n{text}\n", flush=True)
                        else:
                            reasoning, text, usage = _stream_sse_response(
                                resp, wire_api, runtime["role"], runtime["model"], live_print=True
                            )
                    else:
                        raw = resp.read().decode("utf-8", errors="replace")
                        if "text/event-stream" in ctype or raw.lstrip().startswith("data:") or raw.lstrip().startswith("event:"):
                            import io
                            reasoning, text, usage = _stream_sse_response(
                                io.BytesIO(raw.encode("utf-8")), wire_api, runtime["role"], runtime["model"], live_print=False
                            )
                        else:
                            parsed = json.loads(raw)
                            usage = parsed.get("usage") or {}
                            if wire_api == "responses":
                                reasoning = _extract_responses_reasoning_text(parsed)
                                text = _extract_responses_json_text(parsed)
                            else:
                                reasoning, text = _extract_chat_json_parts(parsed)

                elapsed = round((datetime.datetime.now() - started).total_seconds(), 2)
                if stream:
                    print(
                        f"✅ [{runtime['role']}] 完成 | 耗时: {elapsed}s | 档位: {runtime['reasoning_effort']} | "
                        f"Token: {_format_usage_line(usage)}",
                        flush=True,
                    )
                return {
                    "role": runtime["role"],
                    "model": runtime["model"],
                    "effort": runtime["reasoning_effort"],
                    "endpoint": url,
                    "elapsed_s": elapsed,
                    "usage": usage,
                    "reasoning": reasoning,
                    "text": text,
                    "error": None,
                }
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:400]
                if e.code in (404, 502, 503, 504) and idx + 1 < len(urls):
                    last_err = e
                    continue
                if e.code == 400 and attempt < 2:
                    lower_err = err_body.lower()
                    retried = False
                    if include_stream_opts and "stream_options" in lower_err:
                        include_stream_opts = False
                        retried = True
                    elif include_effort and any(k in lower_err for k in ("reasoning_effort", "reasoning.effort", "effort", "thinking", "unsupported", "unknown", "invalid")):
                        cur_eff = str(runtime.get("reasoning_effort") or "").lower()
                        if cur_eff in ("ultra", "max"):
                            runtime["reasoning_effort"] = "xhigh"
                            retried = True
                            print(f"⚠️ [{runtime['role']}] 接口不支持 {cur_eff} 档位，自动平滑降级至 xhigh 重试...", flush=True)
                        else:
                            include_effort = False
                            retried = True
                            print(f"⚠️ [{runtime['role']}] 该接口不支持自定义思考参数，自动降回模型默认档位重试...", flush=True)
                    if retried:
                        last_err = e
                        break
                return {
                    "role": runtime["role"],
                    "model": runtime["model"],
                    "effort": runtime["reasoning_effort"],
                    "endpoint": url,
                    "elapsed_s": round((datetime.datetime.now() - started).total_seconds(), 2),
                    "usage": {},
                    "reasoning": "",
                    "text": "",
                    "error": f"HTTP {e.code}: {err_body}",
                }
            except Exception as e:
                last_err = e
                break
        else:
            break

    return {
        "role": runtime["role"],
        "model": runtime["model"],
        "effort": runtime["reasoning_effort"],
        "endpoint": urls[-1],
        "elapsed_s": round((datetime.datetime.now() - started).total_seconds(), 2),
        "usage": {},
        "reasoning": "",
        "text": "",
        "error": f"请求失败: {last_err}",
    }


# ---------------------------------------------------------------------------
# 5. Output Formatting & CLI Entrypoint
# ---------------------------------------------------------------------------

def write_reply_and_archive(project_dir: Path, results: List[Dict[str, Any]], pivot: bool = False) -> Tuple[Path, Path]:
    """Write aggregated response to _bus/reply.md and save a timestamped archive copy."""
    now = datetime.datetime.now()
    ts_human = now.strftime("%Y-%m-%d %H:%M:%S")
    ts_slug = now.strftime("%Y%m%d-%H%M%S")

    if pivot:
        chief_hint = (
            "> **主控提示（🚨 方向破局模式 `--pivot`）**：本次会诊已**解除 `_control/02_DECISION_LEDGER.md` 历史台账锁定**！"
            "严禁用旧台账驳回新方向。请先向 PI 呈现【术语白话对照表】与【Simplify 降维 / Pivot 换方向 / Hard Stop 一票否决】三选一白话拍板卡。"
        )
    else:
        chief_hint = (
            "> **主控提示**：请 Claude 对照 `_control/02_DECISION_LEDGER.md` 进行参谋长过滤，驳回越权建议，仅向 PI 呈报可拍板的 A/B 选项。"
        )

    lines = [
        "# _bus/reply.md —— 外部专家会诊回执",
        "",
        f"> **课题**：`{project_dir.name}` | **生成时间**：{ts_human}",
        chief_hint,
        "",
        "---",
        "",
    ]

    for res in results:
        usage = res.get("usage") or {}
        usage_str = _format_usage_line(usage)
        effort_str = res.get("effort") or "default"
        lines.append(f"## 专家意见：`{res['role']}` (`{res['model']}` · 思考档位 `{effort_str}`)")
        lines.append(f"- **接口**：`{res['endpoint']}` | **耗时**：`{res['elapsed_s']}s` | **Token 统计**：{usage_str}")
        lines.append("")
        if res.get("error"):
            lines.append(f"> ❌ **调用失败**：`{res['error']}`")
        else:
            reasoning = (res.get("reasoning") or "").strip()
            if reasoning:
                lines.append(f"<details>\n<summary>🧠 推理过程（Reasoning，点击展开 · {len(reasoning)} 字）</summary>\n\n{reasoning}\n\n</details>\n")
            lines.append(res["text"])
        lines.extend(["", "---", ""])

    report = "\n".join(lines)
    reply_path = project_dir / "_bus" / "reply.md"
    archive_dir = project_dir / "_bus" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    roles_tag = "+".join(r["role"] for r in results)
    archive_reply = archive_dir / f"{ts_slug}_{roles_tag}_reply.md"
    archive_ask = archive_dir / f"{ts_slug}_{roles_tag}_ask.md"

    reply_path.write_text(report, encoding="utf-8")
    archive_reply.write_text(report, encoding="utf-8")
    ask_src = project_dir / "_bus" / "ask.md"
    if ask_src.exists():
        archive_ask.write_text(ask_src.read_text(encoding="utf-8"), encoding="utf-8")

    return reply_path, archive_reply


def main() -> int:
    parser = argparse.ArgumentParser(
        description="宿主机 Ghostty/Pebrel 手动点火会诊工具：读取课题 _bus/ask.md，实时流式展示思考过程并写入 _bus/reply.md"
    )
    parser.add_argument(
        "target",
        choices=["redteam", "advisor", "all", "glm_heavy", "glm_fast"],
        help="会诊目标角色：redteam(DeepSeek) | advisor(GPT/cc-switch) | all(双盲) | glm_heavy(GLM 5.3) | glm_fast(GLM 5.3 Flash)",
    )
    parser.add_argument("--project", help="指定课题文件夹名（默认自动识别当前目录或使用 active_project）")
    parser.add_argument("--model", help="临时覆盖 models.toml 中的模型名（例如 --model deepseek-v4-pro）")
    parser.add_argument(
        "--effort",
        choices=["off", "low", "medium", "high", "xhigh", "max", "ultra"],
        help="临时指定思考档位：off(关闭思考) | low | medium | high | xhigh | max | ultra",
    )
    parser.add_argument("--no-stream", action="store_true", help="关闭实时流式输出，改为静默并发等待全部返回")
    parser.add_argument("--eli5", action="store_true", help="要求专家每条专业结论后附带通俗大白话（ELI5）解释")
    parser.add_argument(
        "--pivot",
        action="store_true",
        help="方向破局模式：屏蔽 02_DECISION_LEDGER.md 历史台账死锁锚定，强制输出【术语白话对照表】与【降维/换方向/一票否决】三选一卡片",
    )
    parser.add_argument("--render", action="store_true", help="结束后调用 glow 在终端排版渲染 _bus/reply.md")
    parser.add_argument("--dry-run", action="store_true", help="仅预览上下文大小、目标端点、模型与思考档位，不发起网络请求")
    args = parser.parse_args()

    models_path = HUB_DIR / "models.toml"
    config = load_toml(models_path)
    env_path = Path(str(config.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()
    dotenv = load_dotenv(env_path)

    project_dir = resolve_project_dir(args.project, config)
    user_prompt, stats = build_user_payload(project_dir, config, pivot=args.pivot)
    if args.eli5 or args.pivot:
        user_prompt += (
            "\n\n## [表达要求：ELI5 通俗解释与黑话拆解]\n"
            "请先给每条专业审稿/数学结论配上一句通俗易懂的大白话或生活类比（ELI5），"
            "严禁直接甩出未解释的缩写代号，再用严谨科研术语给出量化依据，方便 PI 快速直观拍板。\n"
        )

    target_roles = ["redteam", "advisor"] if args.target == "all" else [args.target]
    model_overrides: Dict[str, Optional[str]] = {}
    if args.model:
        if "=" in args.model:
            r_key, m_val = args.model.split("=", 1)
            model_overrides[r_key.strip()] = m_val.strip()
        elif len(target_roles) > 1:
            print(
                "[ERROR] 多角色（all）并发时禁止使用裸 --model，请改用 `--model redteam=deepseek-v4-pro` 形式指定目标角色。",
                file=sys.stderr,
            )
            return 2
        else:
            model_overrides[target_roles[0]] = args.model.strip()

    runtimes = [resolve_role_runtime(r, config, dotenv, model_overrides.get(r), args.effort) for r in target_roles]
    if args.pivot:
        for rt in runtimes:
            sp = rt.get("system_prompt", "")
            sp = re.sub(r"不要推翻已经在决策台账中冻结的前提[^\n]*", "", sp)
            rt["system_prompt"] = (
                sp.strip()
                + "\n\n【🚨 方向破局最高授权 (--pivot)】：本次会诊已完全解除《决策台账（02_DECISION_LEDGER）》的一切历史锁定！"
                "允许并鼓励你从第一性原理出发，大胆推翻或砍掉过去作茧自缚的复杂数学补丁，用通俗白话协助 PI 降维做减法或切换科学方向。"
            )

    print(f"[ResearchHub] 目标课题：{project_dir.name}")
    print(
        f"[ResearchHub] 负载统计：合同 {stats['contract_chars']} 字 | 台账 {stats['ledger_chars']} 字 | "
        f"请求单 {stats['ask_chars']} 字 | 总计 {stats['total_chars']} 字"
    )
    for rt in runtimes:
        print(
            f"  -> 角色 [{rt['role']}] | 模型: {rt['model']} ({rt['model_source']}) | 思考档位: {rt['reasoning_effort']} | 协议: {rt['wire_api']} | "
            f"端点: {rt['base_url']} | 密钥来源: {rt['key_source']}"
        )

    if args.dry_run:
        print("[ResearchHub] --dry-run 预览完成，未发起任何外部 API 请求。")
        return 0

    missing = [rt["role"] for rt in runtimes if not rt["api_key"]]
    if missing:
        print(f"[ERROR] 以下角色缺少可用 API Key：{', '.join(missing)}", file=sys.stderr)
        print(f"请先在宿主机编辑 {env_path} 填入对应密钥后再运行。", file=sys.stderr)
        return 1

    stream_mode = not args.no_stream
    if stream_mode:
        print("[ResearchHub] 正在实时流式呼叫外部专家（思考过程将实时显示，结束后自动折叠归档）...", flush=True)
        results = [call_role_api(rt, user_prompt, stream=True) for rt in runtimes]
    else:
        print("[ResearchHub] 正在静默并发呼叫外部专家，请稍候...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
            results = list(pool.map(lambda rt: call_role_api(rt, user_prompt, stream=False), runtimes))

    reply_path, archive_path = write_reply_and_archive(project_dir, results, pivot=args.pivot)
    print("\n================ 本轮会诊汇总 ================")
    for res in results:
        status = "OK" if not res["error"] else f"FAILED ({res['error']})"
        usage_str = _format_usage_line(res.get("usage") or {})
        print(f"  <- [{res['role']}] {res['model']} (档位:{res.get('effort', 'default')}, 耗时:{res['elapsed_s']}s, {usage_str}): {status}")

    print(f"[ResearchHub] 已写入（思考过程已折叠为 <details>）：{reply_path}")
    print(f"[ResearchHub] 已归档：{archive_path}")
    if args.render:
        glow_bin = shutil.which("glow")
        if glow_bin:
            print("\n---------------- Markdown 排版渲染 (glow) ----------------")
            subprocess.run([glow_bin, str(reply_path)], check=False)
        else:
            print("[ResearchHub] 未检测到 glow，跳过终端渲染（可执行 brew install glow 安装）。")
    print("下一步：请在虚拟机 Claude Desktop 中回复「会诊回执已到，请读 _bus/reply.md 并结合台账向我汇报选项」。")
    return 0 if all(not r["error"] for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
