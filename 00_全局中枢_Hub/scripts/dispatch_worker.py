#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/dispatch_worker.py
宿主机白名单任务总线执行器（_dispatch/）：
- 监听各课题 _dispatch/pending/*.json（支持 --once 单次扫盘 或 --watch 常驻监听，0 AI 消耗）
- 仅执行白名单内的安全动作（local_py / win_codex_py / gitea_pull / gitea_push_pr / gitea_pr_status / tailscale_cp）
- 严禁执行任意 shell 字符串；严禁向 Gitea 提交 >10MB 或 .zip/.npz/.pdb 大文件
- 结果写入 _dispatch/done/<job_id>_result.md 并触发 macOS 系统通知
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"

sys.path.insert(0, str(HUB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
import consult  # noqa: E402
import exp_store  # noqa: E402

BLOCKED_GIT_EXTS = {".zip", ".tar", ".gz", ".7z", ".rar", ".npz", ".npy", ".pdb", ".h5", ".hdf5", ".rds", ".bam"}
MAX_GIT_FILE_BYTES = 10 * 1024 * 1024  # 10 MB guard for 40GB Gitea server
ALLOWED_WIN_ROOTS = ("C:\\NCteam", "D:\\NCteam_env")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child.resolve()), str(parent.resolve())]) == str(parent.resolve())
    except ValueError:
        return False


def _notify_macos(title: str, subtitle: str, message: str) -> None:
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    script = f'display notification "{_esc(message)}" with title "{_esc(title)}" subtitle "{_esc(subtitle)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


def _safe_rel_path(project_dir: Path, rel: str, allowed_subdirs: Tuple[str, ...]) -> Path:
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("路径不能为空")
    p = (project_dir / rel).resolve()
    if not _is_within(p, project_dir.resolve()):
        raise PermissionError(f"越权路径（超出课题目录）：{rel}")
    if any(part.startswith("冻结存档-") for part in p.relative_to(project_dir.resolve()).parts):
        raise PermissionError(f"禁止触碰只读冻结存档：{rel}")
    top = p.relative_to(project_dir.resolve()).parts[0]
    if allowed_subdirs and top not in allowed_subdirs:
        raise PermissionError(f"路径必须位于 {allowed_subdirs} 下（当前：{rel}）")
    return p


def _load_gitea_cfg(project_dir: Path, dotenv: Dict[str, str]) -> Tuple[str, str, str, bool, List[str]]:
    """Return (repo_url, gitea_user, gitea_token, only_mine_default, role_keywords)."""
    cfg_file = project_dir / "05_Gitea协作同步区" / ".gitea_repo.json"
    if not cfg_file.is_file():
        raise PermissionError(f"安全拦截：课题 `{project_dir.name}` 未开通 Gitea 协作（仅 P02 等显式开通课题可用）")
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    if not data.get("enabled", False):
        raise PermissionError(f"安全拦截：课题 `{project_dir.name}` 的 Gitea 协作未启用 (enabled!=true)")
    repo_url = str(data.get("repo_url", "")).strip()
    only_mine_default = bool(data.get("only_mine_default", True))
    role_keywords = [str(k).strip() for k in (data.get("role_keywords") or []) if str(k).strip()]
    user = dotenv.get("GITEA_USER") or os.environ.get("GITEA_USER", "")
    token = dotenv.get("GITEA_TOKEN") or os.environ.get("GITEA_TOKEN", "")
    return repo_url, user.strip(), token.strip(), only_mine_default, role_keywords


def _is_my_gitea_item(item: Dict[str, Any], gitea_user: str, keywords: List[str]) -> bool:
    """True if issue/PR is authored by, assigned to, requested for review by, or tagged/titled for me (丙)."""
    u_low = gitea_user.lower()
    author = ((item.get("user") or {}).get("login") or "").lower()
    if u_low and author == u_low:
        return True
    assignees = [((a or {}).get("login") or "").lower() for a in (item.get("assignees") or [])]
    if item.get("assignee"):
        assignees.append(((item["assignee"] or {}).get("login") or "").lower())
    reviewers = [((r or {}).get("login") or "").lower() for r in (item.get("requested_reviewers") or [])]
    if u_low and (u_low in assignees or u_low in reviewers):
        return True
    title = str(item.get("title") or "")
    labels = " ".join(str((lb or {}).get("name") or "") for lb in (item.get("labels") or []))
    haystack = f"{title} {labels}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def _run_git_with_token(args: List[str], cwd: Optional[Path], user: str, token: str, timeout: int = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass_path: Optional[Path] = None
    try:
        if token:
            fd, tmp_name = tempfile.mkstemp(prefix="rh_askpass_", suffix=".sh")
            askpass_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
                f.write('case "$1" in\n')
                f.write(f'  *Username*) printf "%s\\n" "{user or "git"}" ;;\n')
                f.write(f'  *) printf "%s\\n" "{token}" ;;\n')
                f.write("esac\n")
            askpass_path.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass_path)
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        if askpass_path and askpass_path.exists():
            askpass_path.unlink(missing_ok=True)


def _gitea_api(repo_url: str, token: str, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    parsed = urllib.parse.urlparse(repo_url)
    path_parts = [x for x in parsed.path.strip("/").split("/") if x]
    if len(path_parts) < 2:
        raise ValueError(f"无法解析 Gitea 仓库路径：{repo_url}")
    owner = path_parts[-2]
    repo = path_parts[-1].removesuffix(".git")
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/repos/{owner}/{repo}/{endpoint.lstrip('/')}"
    headers = {"Accept": "application/json", "User-Agent": "ResearchHub-Dispatch/1.0"}
    if token:
        headers["Authorization"] = f"token {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:800]


def _handle_local_py(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    script_rel = str(job.get("script", ""))
    script_path = _safe_rel_path(project_dir, script_rel, ("03_代码与脚本", "02_实验与验证"))
    if not script_path.is_file():
        return False, f"找不到脚本：{script_rel}"
    args = [str(a) for a in (job.get("args") or [])]
    timeout = min(max(int(job.get("timeout_s", 600)), 10), 7200)
    res = subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = f"exit_code: {res.returncode}\n\n### stdout\n```\n{res.stdout[-4000:]}\n```\n\n### stderr\n```\n{res.stderr[-2000:]}\n```"
    return res.returncode == 0, out


def _handle_win_codex_py(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    ssh_host = str(job.get("ssh_host", "win-codex")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", ssh_host):
        return False, f"非法 ssh_host：{ssh_host}"
    win_cwd = str(job.get("win_cwd", "D:\\NCteam_env")).strip()
    if not any(win_cwd.lower().startswith(r.lower()) for r in ALLOWED_WIN_ROOTS):
        return False, f"安全拦截：win_cwd 必须位于 {ALLOWED_WIN_ROOTS} 内（当前：{win_cwd}）"

    logs: List[str] = []
    # Optional: upload script from project_dir to win_cwd
    upload_rel = job.get("upload_script")
    remote_script = str(job.get("remote_script", "")).strip()
    if upload_rel:
        local_src = _safe_rel_path(project_dir, str(upload_rel), ("03_代码与脚本",))
        if not local_src.is_file():
            return False, f"找不到本地待传脚本：{upload_rel}"
        target_name = remote_script or local_src.name
        if ".." in target_name or "/" in target_name or "\\" in target_name:
            return False, f"非法 remote_script 文件名：{target_name}"
        remote_target = f"{ssh_host}:{win_cwd}\\{target_name}"
        scp_up = subprocess.run(["scp", str(local_src), remote_target], capture_output=True, text=True, timeout=60)
        if scp_up.returncode != 0:
            return False, f"SCP 上传失败：\n{scp_up.stderr}"
        logs.append(f"已上传 `{upload_rel}` -> `{remote_target}`")
        remote_script = target_name

    if not remote_script or not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", remote_script):
        return False, f"remote_script 必须为纯 .py 文件名（当前：{remote_script}）"

    win_py = str(job.get("win_python", "python")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:\\\\-]+", win_py):
        return False, f"非法 win_python 路径：{win_py}"
    safe_args = []
    for a in (job.get("args") or []):
        s = str(a)
        if any(bad in s for bad in ("&", "|", ";", ">", "<", "`", "$(")):
            return False, f"参数包含危险字符：{s}"
        safe_args.append(s)

    arg_str = " ".join(f'"{x}"' if " " in x else x for x in safe_args)
    cmd = f'cd /d "{win_cwd}" && "{win_py}" "{remote_script}" {arg_str}'.strip()
    timeout = min(max(int(job.get("timeout_s", 1800)), 10), 7200)
    run_res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", ssh_host, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    logs.append(f"SSH 执行 `{cmd}` -> exit={run_res.returncode}")
    logs.append(f"### stdout\n```\n{run_res.stdout[-4000:]}\n```")
    if run_res.stderr.strip():
        logs.append(f"### stderr\n```\n{run_res.stderr[-2000:]}\n```")

    # Optional: pull output files back into project_dir/02_实验与验证/
    for item in (job.get("pull_files") or []):
        rem_rel = str(item.get("remote", "")).strip()
        loc_rel = str(item.get("local", "")).strip()
        if not rem_rel or ".." in rem_rel or any(bad in rem_rel for bad in ("&", "|", ";")):
            return False, f"非法远程拉取路径：{rem_rel}"
        local_dst = _safe_rel_path(project_dir, loc_rel, ("02_实验与验证",))
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        remote_src = f"{ssh_host}:{win_cwd}\\{rem_rel}"
        scp_dn = subprocess.run(["scp", remote_src, str(local_dst)], capture_output=True, text=True, timeout=180)
        if scp_dn.returncode != 0:
            logs.append(f"⚠️ 拉取失败 `{remote_src}`: {scp_dn.stderr.strip()}")
        else:
            logs.append(f"✅ 已取回 `{remote_src}` -> `{loc_rel}`")

    return run_res.returncode == 0, "\n\n".join(logs)


def _handle_remote_py(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    """通用远程算力抽象（支持 Linux/POSIX 服务器与 Windows 节点）。"""
    os_type = str(job.get("os_type", "")).strip().lower()
    ssh_host = str(job.get("ssh_host", "win-codex")).strip()
    if os_type == "windows" or (not os_type and ssh_host == "win-codex" and "remote_cwd" not in job):
        return _handle_win_codex_py(project_dir, job)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", ssh_host):
        return False, f"非法 ssh_host：{ssh_host}"
    remote_cwd = str(job.get("remote_cwd", "~/research_runs")).strip()
    if not re.fullmatch(r"[~A-Za-z0-9_./-]+", remote_cwd) or ".." in remote_cwd:
        return False, f"非法 remote_cwd：{remote_cwd}"

    logs: List[str] = []
    upload_rel = job.get("upload_script")
    remote_script = str(job.get("remote_script", "")).strip()
    if upload_rel:
        local_src = _safe_rel_path(project_dir, str(upload_rel), ("03_代码与脚本",))
        if not local_src.is_file():
            return False, f"找不到本地待传脚本：{upload_rel}"
        target_name = remote_script or local_src.name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", target_name):
            return False, f"非法 remote_script 文件名：{target_name}"
        subprocess.run(["ssh", "-o", "BatchMode=yes", ssh_host, f"mkdir -p {remote_cwd}"], capture_output=True, timeout=30)
        remote_target = f"{ssh_host}:{remote_cwd}/{target_name}"
        scp_up = subprocess.run(["scp", str(local_src), remote_target], capture_output=True, text=True, timeout=60)
        if scp_up.returncode != 0:
            return False, f"SCP 上传失败：\n{scp_up.stderr}"
        logs.append(f"已上传 `{upload_rel}` -> `{remote_target}`")
        remote_script = target_name

    if not remote_script or not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", remote_script):
        return False, f"remote_script 必须为纯 .py 文件名（当前：{remote_script}）"

    rem_py = str(job.get("remote_python", "python3")).strip()
    if not re.fullmatch(r"[~A-Za-z0-9_./-]+", rem_py):
        return False, f"非法 remote_python：{rem_py}"
    safe_args = []
    for a in (job.get("args") or []):
        s = str(a)
        if any(bad in s for bad in ("&", "|", ";", ">", "<", "`", "$(")):
            return False, f"参数包含危险字符：{s}"
        safe_args.append(s)

    arg_str = " ".join(f'"{x}"' if " " in x else x for x in safe_args)
    cmd = f"cd {remote_cwd} && {rem_py} {remote_script} {arg_str}".strip()
    timeout = min(max(int(job.get("timeout_s", 1800)), 10), 7200)
    run_res = subprocess.run(["ssh", "-o", "BatchMode=yes", ssh_host, cmd], capture_output=True, text=True, timeout=timeout)
    logs.append(f"SSH 执行 `{cmd}` -> exit={run_res.returncode}")
    logs.append(f"### stdout\n```\n{run_res.stdout[-4000:]}\n```")
    if run_res.stderr.strip():
        logs.append(f"### stderr\n```\n{run_res.stderr[-2000:]}\n```")

    for item in (job.get("pull_files") or []):
        rem_rel = str(item.get("remote", "")).strip()
        loc_rel = str(item.get("local", "")).strip()
        if not rem_rel or ".." in rem_rel or any(bad in rem_rel for bad in ("&", "|", ";")):
            return False, f"非法远程拉取路径：{rem_rel}"
        local_dst = _safe_rel_path(project_dir, loc_rel, ("02_实验与验证",))
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        remote_src = f"{ssh_host}:{remote_cwd}/{rem_rel}"
        scp_dn = subprocess.run(["scp", remote_src, str(local_dst)], capture_output=True, text=True, timeout=180)
        if scp_dn.returncode != 0:
            logs.append(f"⚠️ 拉取失败 `{remote_src}`: {scp_dn.stderr.strip()}")
        else:
            logs.append(f"✅ 已取回 `{remote_src}` -> `{loc_rel}`")
    return run_res.returncode == 0, "\n\n".join(logs)


def _handle_gitea_pull(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, user, token, only_mine_def, keywords = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url:
        return False, "未配置 05_Gitea协作同步区/.gitea_repo.json 中的 repo_url"
    if not token:
        return False, "请在宿主机 ~/.config/research_hub/.env 中填入 GITEA_USER 与 GITEA_TOKEN"

    only_mine = bool(job.get("only_mine", only_mine_def))
    sync_dir = project_dir / "05_Gitea协作同步区"
    sync_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = sync_dir / "repo"
    branch = str(job.get("branch", "main")).strip()

    if not (repo_dir / ".git").is_dir():
        res = _run_git_with_token(["clone", "-b", branch, repo_url, str(repo_dir)], cwd=sync_dir, user=user, token=token)
        if res.returncode != 0:
            return False, f"git clone 失败：\n{res.stderr}\n{res.stdout}"
    else:
        res_f = _run_git_with_token(["fetch", "--all", "--prune"], cwd=repo_dir, user=user, token=token)
        res_c = _run_git_with_token(["checkout", branch], cwd=repo_dir, user=user, token=token)
        res_p = _run_git_with_token(["pull", "--ff-only", "origin", branch], cwd=repo_dir, user=user, token=token)
        if res_f.returncode != 0 or res_c.returncode != 0 or res_p.returncode != 0:
            return False, f"git pull 失败：\n{res_f.stderr}\n{res_c.stderr}\n{res_p.stderr}"

    log_res = subprocess.run(["git", "log", "-n", "8", "--oneline"], cwd=str(repo_dir), capture_output=True, text=True)
    st_code, prs = _gitea_api(repo_url, token, "pulls?state=open&limit=30")
    pr_lines = []
    if st_code == 200 and isinstance(prs, list):
        filtered_prs = [pr for pr in prs if (not only_mine or _is_my_gitea_item(pr, user, keywords))]
        for pr in filtered_prs:
            pr_lines.append(f"- #{pr.get('number')} [{pr.get('state')}] {pr.get('title')} (by {((pr.get('user') or {}).get('login'))}, head={((pr.get('head') or {}).get('ref'))})")
    else:
        pr_lines.append(f"- PR 查询返回 HTTP {st_code}")

    ist_code, issues = _gitea_api(repo_url, token, "issues?state=open&type=issues&limit=30")
    issue_lines = []
    if ist_code == 200 and isinstance(issues, list):
        filtered_issues = [it for it in issues if (not only_mine or _is_my_gitea_item(it, user, keywords))]
        for it in filtered_issues:
            lbs = ",".join(str((lb or {}).get("name") or "") for lb in (it.get("labels") or []))
            issue_lines.append(f"- #{it.get('number')} {it.get('title')}" + (f" [{lbs}]" if lbs else ""))
    else:
        issue_lines.append(f"- 工单查询返回 HTTP {ist_code}")

    scope_note = f"仅与我（丙 / `{user or '未填USER'}`）相关" if only_mine else "全量工单与 PR"
    inbox_md = (
        f"# Gitea 同步收件箱（{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）\n\n"
        f"- 仓库: `{repo_url}` | 当前分支: `{branch}` | 筛选范围: **{scope_note}**\n\n"
        f"## 最近提交\n```\n{log_res.stdout.strip()}\n```\n\n"
        f"## 相关未关闭工单（Issues）\n" + ("\n".join(issue_lines) if issue_lines else "- 无") + "\n\n"
        f"## 相关未关闭 PR\n" + ("\n".join(pr_lines) if pr_lines else "- 无") + "\n"
    )
    (sync_dir / "INBOX_CHANGES.md").write_text(inbox_md, encoding="utf-8")
    return True, inbox_md


def _handle_gitea_push_pr(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, user, token, _, _ = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url or not token:
        return False, "缺少 Gitea repo_url 或 ~/.config/research_hub/.env 中的 GITEA_TOKEN"

    sync_dir = project_dir / "05_Gitea协作同步区"
    repo_dir = sync_dir / "repo"
    if not (repo_dir / ".git").is_dir():
        return False, "尚未初始化 05_Gitea协作同步区/repo，请先执行 gitea_pull"

    branch = str(job.get("branch", "")).strip()
    if not branch or branch in ("main", "master"):
        return False, f"安全拦截：禁止直接 push 主分支 `{branch}`，请使用特性分支（如 feat/bing-update）"

    # Guard against large binaries / archives bloating 40GB Gitea server
    for p in repo_dir.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() in BLOCKED_GIT_EXTS:
            return False, f"拦截：禁止向 Gitea 提交压缩包/大二进制 `{p.relative_to(repo_dir)}`，请改走 tailscale_cp 或 Release 临时中转"
        if p.stat().st_size > MAX_GIT_FILE_BYTES:
            return False, f"拦截：文件 `{p.relative_to(repo_dir)}` 超过 10 MB 限制（40G 硬盘保护）"

    subprocess.run(["git", "checkout", "-B", branch], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    st = subprocess.run(["git", "status", "--short"], cwd=str(repo_dir), capture_output=True, text=True)
    if not st.stdout.strip():
        return True, "工作区无新增改动，跳过 commit/push。"

    commit_msg = str(job.get("commit_message", "chore: sync updates from Claude workspace")).strip()
    c_res = subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(repo_dir), capture_output=True, text=True)
    if c_res.returncode != 0:
        return False, f"git commit 失败：\n{c_res.stderr}\n{c_res.stdout}"

    p_res = _run_git_with_token(["push", "-u", "origin", branch], cwd=repo_dir, user=user, token=token)
    if p_res.returncode != 0:
        return False, f"git push 失败：\n{p_res.stderr}\n{p_res.stdout}"

    out_lines = [f"✅ 已推送分支 `{branch}`：\n```\n{st.stdout.strip()}\n```"]
    if job.get("open_pr", True):
        base = str(job.get("base_branch", "main")).strip()
        pr_title = str(job.get("pr_title", commit_msg)).strip()
        pr_body = str(job.get("pr_body", "由宿主机 _dispatch 自动同步提交，请审阅合并。")).strip()
        code, pr_resp = _gitea_api(
            repo_url,
            token,
            "pulls",
            method="POST",
            payload={"title": pr_title, "body": pr_body, "head": branch, "base": base},
        )
        if code in (200, 201):
            out_lines.append(f"✅ 已创建 PR #{pr_resp.get('number')}: {pr_resp.get('html_url', '')}")
        else:
            out_lines.append(f"ℹ️ PR 创建返回 HTTP {code}: {pr_resp}")
    return True, "\n\n".join(out_lines)


def _handle_gitea_pr_status(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, user, token, only_mine_def, keywords = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url or not token:
        return False, "缺少 Gitea repo_url 或 GITEA_TOKEN"
    only_mine = bool(job.get("only_mine", only_mine_def))
    code, prs = _gitea_api(repo_url, token, "pulls?state=all&limit=30")
    if code != 200 or not isinstance(prs, list):
        return False, f"查询 PR 失败 HTTP {code}: {prs}"
    icode, issues = _gitea_api(repo_url, token, "issues?state=open&type=issues&limit=30")
    filtered_prs = [pr for pr in prs if (not only_mine or _is_my_gitea_item(pr, user, keywords))]
    filtered_issues = [it for it in (issues if isinstance(issues, list) else []) if (not only_mine or _is_my_gitea_item(it, user, keywords))]
    scope_note = f"仅与我（丙 / `{user or '未填USER'}`）相关" if only_mine else "全量"
    lines = [f"# Gitea 工单与 PR 审批状态汇总（{scope_note}）\n", "## 未关闭工单（Issues）"]
    if filtered_issues:
        for it in filtered_issues[:15]:
            lines.append(f"- **#{it.get('number')}** {it.get('title')}")
    else:
        lines.append("- 无")
    lines.append("\n## PR 审批状态")
    if filtered_prs:
        for pr in filtered_prs[:15]:
            lines.append(
                f"- **#{pr.get('number')}** [{pr.get('state')}] {pr.get('title')} "
                f"(merged={pr.get('merged')}, head={((pr.get('head') or {}).get('ref'))})"
            )
    else:
        lines.append("- 无")
    report = "\n".join(lines) + "\n"
    (project_dir / "05_Gitea协作同步区" / "PR_STATUS.md").write_text(report, encoding="utf-8")
    return True, report


def _resolve_body_text(project_dir: Path, job: Dict[str, Any]) -> str:
    body = str(job.get("body") or "")
    body_file = str(job.get("body_file") or "").strip()
    if body_file:
        p = _safe_rel_path(project_dir, body_file, ("01_论文与协议", "02_实验与验证", "04_组会与周报_Weekly", "05_Gitea协作同步区", "_bus", "_dispatch"))
        if not p.is_file():
            raise FileNotFoundError(f"找不到工单正文文件：{body_file}")
        body = p.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("工单正文 body 或 body_file 不能为空")
    return body


def _handle_gitea_create_issue(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, _, token, _, _ = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url or not token:
        return False, "缺少 Gitea repo_url 或 GITEA_TOKEN"
    title = str(job.get("title") or "").strip()
    if not title:
        return False, "创建工单缺少必填字段 title"
    body = _resolve_body_text(project_dir, job)
    payload: Dict[str, Any] = {"title": title, "body": body}
    if job.get("assignees"):
        payload["assignees"] = [str(a) for a in job["assignees"]]
    code, resp = _gitea_api(repo_url, token, "issues", method="POST", payload=payload)
    if code in (200, 201) and isinstance(resp, dict):
        return True, f"✅ 已创建 Gitea 工单 **#{resp.get('number')}**: {resp.get('title')}\n- 链接: {resp.get('html_url', '')}"
    return False, f"创建工单失败 HTTP {code}: {resp}"


def _handle_gitea_comment_issue(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, _, token, _, _ = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url or not token:
        return False, "缺少 Gitea repo_url 或 GITEA_TOKEN"
    num = int(job.get("issue_number") or 0)
    if num <= 0:
        return False, "回复工单缺少正整数 issue_number"
    body = _resolve_body_text(project_dir, job)
    code, resp = _gitea_api(repo_url, token, f"issues/{num}/comments", method="POST", payload={"body": body})
    if code in (200, 201) and isinstance(resp, dict):
        return True, f"✅ 已在工单 **#{num}** 追加评论\n- 链接: {resp.get('html_url', '')}"
    return False, f"追加评论失败 HTTP {code}: {resp}"


def _handle_tailscale_cp(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    rel = str(job.get("file", "")).strip()
    peer = str(job.get("peer", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", peer):
        return False, f"非法 Tailscale 节点名：{peer}"
    src = _safe_rel_path(project_dir, rel, ("02_实验与验证", "04_组会与周报_Weekly", "05_Gitea协作同步区"))
    if not src.is_file():
        return False, f"找不到待传文件：{rel}"
    ts_bin = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not Path(ts_bin).is_file():
        return False, "本机未找到 tailscale CLI"
    res = subprocess.run([ts_bin, "file", "cp", str(src), f"{peer}:"], capture_output=True, text=True, timeout=180)
    return res.returncode == 0, f"tailscale file cp -> {peer}:\nstdout: {res.stdout}\nstderr: {res.stderr}"


def process_project(project_dir: Path, dotenv: Dict[str, str]) -> int:
    disp = project_dir / "_dispatch"
    pending_dir = disp / "pending"
    running_dir = disp / "running"
    done_dir = disp / "done"
    archive_dir = disp / "archive"
    for d in (pending_dir, running_dir, done_dir, archive_dir):
        d.mkdir(parents=True, exist_ok=True)

    jobs = sorted(pending_dir.glob("*.json"))
    count = 0
    for jf in jobs:
        run_path = running_dir / jf.name
        shutil.move(str(jf), str(run_path))
        started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok = False
        detail = ""
        action = "unknown"
        job_id = jf.stem
        try:
            job = json.loads(run_path.read_text(encoding="utf-8"))
            job_id = str(job.get("job_id") or jf.stem)
            action = str(job.get("action") or "").strip()
            if action == "local_py":
                ok, detail = _handle_local_py(project_dir, job)
            elif action == "win_codex_py":
                ok, detail = _handle_win_codex_py(project_dir, job)
            elif action == "remote_py":
                ok, detail = _handle_remote_py(project_dir, job)
            elif action == "gitea_pull":
                ok, detail = _handle_gitea_pull(project_dir, job, dotenv)
            elif action == "gitea_push_pr":
                ok, detail = _handle_gitea_push_pr(project_dir, job, dotenv)
            elif action == "gitea_pr_status":
                ok, detail = _handle_gitea_pr_status(project_dir, job, dotenv)
            elif action == "gitea_create_issue":
                ok, detail = _handle_gitea_create_issue(project_dir, job, dotenv)
            elif action == "gitea_comment_issue":
                ok, detail = _handle_gitea_comment_issue(project_dir, job, dotenv)
            elif action == "tailscale_cp":
                ok, detail = _handle_tailscale_cp(project_dir, job)
            else:
                ok, detail = False, f"拒绝执行：未知或不在白名单内的 action=`{action}`"
        except Exception as exc:
            ok, detail = False, f"执行异常：{type(exc).__name__}: {exc}"

        finished = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_str = "SUCCESS" if ok else "FAILED"
        result_md = (
            f"# 任务回执：{job_id}\n\n"
            f"- **课题**：`{project_dir.name}`\n"
            f"- **白名单动作**：`{action}`\n"
            f"- **状态**：**{status_str}**\n"
            f"- **开始 / 结束**：{started} → {finished}\n\n"
            f"## 执行详情\n{detail}\n"
        )
        result_file = done_dir / f"{job_id}_result.md"
        result_file.write_text(result_md, encoding="utf-8")
        try:
            exp_store.record_dispatch_run(
                project_dir=project_dir,
                job_id=job_id,
                action=action,
                status=status_str,
                started_at=started,
                finished_at=finished,
                job_payload=job if "job" in locals() and isinstance(job, dict) else {},
                detail=detail,
            )
        except Exception as exc:
            print(f"[Dispatch] 写入 SQLite 实验库失败（已忽略）: {exc}", flush=True)
        shutil.move(str(run_path), str(archive_dir / jf.name))
        print(f"[Dispatch] {project_dir.name} | {job_id} ({action}) -> {status_str} | 回执: {result_file.name}", flush=True)
        _notify_macos(
            f"ResearchHub 任务{ '完成' if ok else '失败' }",
            f"{project_dir.name} · {job_id} ({action})",
            "请切回 Claude 输入「接续」读取 _dispatch/done/ 回执",
        )
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="宿主机白名单任务总线执行器（_dispatch/）")
    parser.add_argument("--project", help="仅监听指定课题目录（默认扫描 01_在研课题_Projects 下全部课题）")
    parser.add_argument("--watch", action="store_true", help="常驻轮询监听 _dispatch/pending/（每 2 秒）")
    parser.add_argument("--interval", type=float, default=2.0, help="watch 轮询间隔秒数（默认 2.0）")
    args = parser.parse_args()

    config = consult.load_toml(HUB_DIR / "models.toml")
    env_path = Path(str(config.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()

    def _targets() -> List[Path]:
        if args.project:
            return [consult.resolve_project_dir(args.project, config)]
        return [p for p in sorted(PROJECTS_DIR.iterdir()) if p.is_dir() and not p.name.startswith(".")]

    if not args.watch:
        dotenv = consult.load_dotenv(env_path)
        total = sum(process_project(p, dotenv) for p in _targets())
        print(f"[Dispatch] 单次扫描完成，共处理 {total} 个任务。")
        return 0

    print(f"[Dispatch] 已启动白名单常驻监听（间隔 {args.interval}s，0 AI 消耗）。按 Ctrl+C 退出。", flush=True)
    try:
        while True:
            dotenv = consult.load_dotenv(env_path)
            for p in _targets():
                process_project(p, dotenv)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        print("\n[Dispatch] 已停止监听。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
