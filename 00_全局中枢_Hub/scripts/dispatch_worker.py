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
import fcntl
import threading
import shlex
from pathlib import PureWindowsPath
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = Path(os.environ.get("RH_EXCHANGE_ROOT", HUB_DIR.parent))
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"

sys.path.insert(0, str(HUB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
import run_stream
from service_reload import reload_changed
import colab_jobs
import issue_inbox
from protected_paths import protected, walk
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
    if protected(p) or p == project_dir.resolve():
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
    if data.get("enabled") is not True:
        raise PermissionError(f"安全拦截：课题 `{project_dir.name}` 的 Gitea 协作未启用 (enabled!=true)")
    repo_url = str(data.get("repo_url", "")).strip()
    trust = json.loads((Path.home()/'.config/research_hub/dispatch_trust.json').read_text())
    if repo_url != trust.get('repos',{}).get(project_dir.name):
        raise PermissionError('仓库不匹配宿主机授权地址')
    if (project_dir/"05_Gitea协作同步区").resolve().parent != project_dir.resolve(): raise PermissionError("Gitea 同步区越界")
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
    haystack = f"{title} {labels} {item.get('body') or ''}".lower()
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
                env['RH_GIT_USER'] = user or 'git'
                env['RH_GIT_PASSWORD'] = token
                f.write('  *Username*) echo "$RH_GIT_USER" ;;\n')
                f.write('  *) echo "$RH_GIT_PASSWORD" ;;\n')
                f.write("esac\n")
            askpass_path.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass_path)
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "http.followRedirects=false", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        if askpass_path and askpass_path.exists():
            askpass_path.unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def sync_details(project, dotenv, pr_report=None):
    repo, user, token, _, words = _load_gitea_cfg(project, dotenv)
    if not token: raise ValueError('缺少 GITEA_TOKEN')
    folder = project/'05_Gitea协作同步区'
    if folder.resolve().parent != project.resolve(): raise PermissionError('同步区越界')
    return issue_inbox.sync(folder,lambda endpoint:_gitea_api(repo,token,endpoint),
        lambda item:_is_my_gitea_item(item,user,words + ['丙','C01','C02','C03','C04',user or 'query']),user,
        lambda numbers:_notify_macos('ResearchHub 工单更新',project.name,numbers+' 有新评论或回执'),
        Path.home()/'.local/state/researchhub/issues'/project.name,
        export=project/'02_实验与验证'/'_inbox_copy', pr_report=pr_report)


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
        with urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({})).open(req, timeout=20) as resp:
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
    if script_path.suffix != '.py': raise ValueError('仅接受 .py 脚本')
    res = run_stream.local(project_dir,script_path,args,job['_log'],timeout)
    out = f"exit_code: {res.returncode}\n\n### stdout\n```\n{res.stdout[-4000:]}\n```\n\n### stderr\n```\n{res.stderr[-2000:]}\n```"
    return res.returncode == 0, out


def _handle_win_codex_py(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    ssh_host = str(job.get("ssh_host", "win-codex")).strip()
    if ssh_host != "win-codex":
        return False, f"非法 ssh_host：{ssh_host}"
    win_cwd = str(job.get("win_cwd", "D:\\NCteam_env")).strip()
    if not re.fullmatch(r"[A-Za-z]:[\\\w .-]+",win_cwd) or ".." in PureWindowsPath(win_cwd).parts or not any(PureWindowsPath(win_cwd).is_relative_to(r) for r in ALLOWED_WIN_ROOTS):
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
        if not re.fullmatch(r"[\w][\w.-]*\.py",target_name):
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
    if not re.fullmatch(r"(?:[A-Za-z]:[\\\w.-]+\\)?python(?:3(?:\.\d+)?)?(?:\.exe)?", win_py, re.I):
        return False, f"非法 win_python 路径：{win_py}"
    safe_args = []
    for a in (job.get("args") or []):
        s = str(a)
        if not re.fullmatch(r"[\w ./\\:=,+-]*",s):
            return False, f"参数包含危险字符：{s}"
        safe_args.append(s)

    # The configured Windows workstation uses PowerShell as its OpenSSH shell.
    # Variables are already constrained above. Single-quote each literal argument.
    tokens = ' '.join("'" + value.replace("'", "''") + "'" for value in [win_py,'-u',remote_script,*safe_args])
    cmd = f"Set-Location -ErrorAction Stop -LiteralPath '{win_cwd}'; & {tokens}; exit $LASTEXITCODE"
    timeout = min(max(int(job.get("timeout_s", 1800)), 10), 7200)
    run_res = run_stream.stream(['ssh','-o','BatchMode=yes',ssh_host,cmd],job['_log'],timeout)
    logs.append(f"SSH 执行 `{cmd}` -> exit={run_res.returncode}")
    logs.append(f"### stdout\n```\n{run_res.stdout[-4000:]}\n```")
    if run_res.stderr.strip():
        logs.append(f"### stderr\n```\n{run_res.stderr[-2000:]}\n```")

    # Optional: pull output files back into project_dir/02_实验与验证/
    for item in (job.get("pull_files") or []):
        rem_rel = str(item.get("remote", "")).strip()
        loc_rel = str(item.get("local", "")).strip()
        if not re.fullmatch(r"[\w][\w./\\ -]*", rem_rel) or ".." in rem_rel:
            return False, f"非法远程拉取路径：{rem_rel}"
        local_dst = _safe_rel_path(project_dir, loc_rel, ("02_实验与验证",))
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        remote_src = f"{ssh_host}:{win_cwd}\\{rem_rel}"
        scp_dn = subprocess.run(["scp", remote_src, str(local_dst)], capture_output=True, text=True, timeout=180)
        if scp_dn.returncode != 0:
            logs.append(f"⚠️ 拉取失败 `{remote_src}`: {scp_dn.stderr.strip()}")
        else:
            logs.append(f"✅ 已取回 `{remote_src}` -> `{loc_rel}`")

    return run_res.returncode == 0 and not any("拉取失败" in line for line in logs), "\n\n".join(logs)


def _handle_remote_py(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    """通用远程算力抽象（支持 Linux/POSIX 服务器与 Windows 节点）。"""
    os_type = str(job.get("os_type", "")).strip().lower()
    ssh_host = str(job.get("ssh_host", "win-codex")).strip()
    if os_type == "windows" or (not os_type and ssh_host == "win-codex" and "remote_cwd" not in job):
        return _handle_win_codex_py(project_dir, job)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ssh_host) or ssh_host not in json.loads((Path.home()/".config/research_hub/dispatch_trust.json").read_text()).get("posix_hosts",[]):
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
    if not re.fullmatch(r"(?:[~/\w.-]+/)?python(?:3(?:\.\d+)?)?", rem_py):
        return False, f"非法 remote_python：{rem_py}"
    safe_args = []
    for a in (job.get("args") or []):
        s = str(a)
        if any(bad in s for bad in ("&", "|", ";", ">", "<", "`", "$(")):
            return False, f"参数包含危险字符：{s}"
        safe_args.append(s)

    arg_str = " ".join(f'"{x}"' if " " in x else x for x in safe_args)
    cmd = f"cd {remote_cwd} && {rem_py} -u {remote_script} {shlex.join(safe_args)}".strip()
    timeout = min(max(int(job.get("timeout_s", 1800)), 10), 7200)
    run_res = run_stream.stream(["ssh", "-o", "BatchMode=yes", ssh_host, cmd],job["_log"],timeout)
    logs.append(f"SSH 执行 `{cmd}` -> exit={run_res.returncode}")
    logs.append(f"### stdout\n```\n{run_res.stdout[-4000:]}\n```")
    if run_res.stderr.strip():
        logs.append(f"### stderr\n```\n{run_res.stderr[-2000:]}\n```")

    for item in (job.get("pull_files") or []):
        rem_rel = str(item.get("remote", "")).strip()
        loc_rel = str(item.get("local", "")).strip()
        if not re.fullmatch(r"[\w][\w./\\ -]*", rem_rel) or ".." in rem_rel:
            return False, f"非法远程拉取路径：{rem_rel}"
        local_dst = _safe_rel_path(project_dir, loc_rel, ("02_实验与验证",))
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        remote_src = f"{ssh_host}:{remote_cwd}/{rem_rel}"
        scp_dn = subprocess.run(["scp", remote_src, str(local_dst)], capture_output=True, text=True, timeout=180)
        if scp_dn.returncode != 0:
            logs.append(f"⚠️ 拉取失败 `{remote_src}`: {scp_dn.stderr.strip()}")
        else:
            logs.append(f"✅ 已取回 `{remote_src}` -> `{loc_rel}`")
    return run_res.returncode == 0 and not any("拉取失败" in line for line in logs), "\n\n".join(logs)


def _guard_checkout(repo, expected_url):
    if repo.resolve() != repo.absolute() or (repo/'.git').is_symlink():
        raise PermissionError('拒绝外部工作树')
    if not repo.exists(): return
    keys = subprocess.check_output(['git','-C',str(repo),'config','--local','--no-includes','--name-only','--list'],text=True).splitlines()
    for key in keys:
        if not re.fullmatch(r'core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)|remote\.origin\.(url|fetch)|branch\.[\w./-]+\.(remote|merge)',key):
            raise PermissionError('仓库含未经授权的本地 Git 配置')
    for opt in ([],['--push']):
        urls = subprocess.check_output(['git','-C',str(repo),'remote','get-url',*opt,'--all','origin'],text=True).splitlines()
        if urls != [expected_url]: raise PermissionError('Git origin 与授权地址不符')


def _guard_push_objects(repo, branch):
    listing = subprocess.check_output(['git','rev-list','--objects',branch,'--not','--remotes=origin'],cwd=repo)
    if not listing: return
    rows = listing.splitlines()
    ids = b'\n'.join(row.split(b' ',1)[0] for row in rows)+b'\n'
    stats = subprocess.check_output(['git','cat-file','--batch-check=%(objecttype) %(objectsize)'],cwd=repo,input=ids).splitlines()
    for row,stat in zip(rows,stats):
        kind,size = stat.split()
        filename = row.partition(b' ')[2].decode('utf-8','replace')
        if kind == b'blob' and (int(size)>MAX_GIT_FILE_BYTES or Path(filename).suffix.lower() in BLOCKED_GIT_EXTS):
            raise ValueError('待推送提交历史中含结果包或大文件，交付请走 Release')


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
    _guard_checkout(repo_dir,repo_url)
    branch = str(job.get("branch", "main")).strip()

    if not (repo_dir / ".git").is_dir():
        res = _run_git_with_token(["clone", "-b", branch, repo_url, str(repo_dir)], cwd=sync_dir, user=user, token=token)
        if res.returncode != 0:
            return False, f"git clone 失败：\n{res.stderr}\n{res.stdout}"
    else:
        res_f = _run_git_with_token(["fetch", "--prune", "origin"], cwd=repo_dir, user=user, token=token)
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
    latest = sync_details(project_dir,dotenv)
    return True, latest + "\n" + inbox_md


def _stage_rel(value):
    if not isinstance(value, str) or not value or any(
        not part or part.startswith('.') or not re.fullmatch(r'[\w .()-]+', part)
        for part in value.split('/')
    ) or protected(value):
        raise ValueError('暂存包和仓库目标须为显式相对路径')
    return Path(value)


def _push_existing_pr(project, job, repo_url, user, token):
    import hashlib
    number = job.get('pr_number')
    branch = job.get('branch', '')
    expected = job.get('expected_head', '')
    if type(number) is not int or number < 1 or not re.fullmatch(r'feat/[\w./-]+', branch) or not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise ValueError('现有 PR 的编号、特性分支及 expected_head 必须明确')
    code, pr = _gitea_api(repo_url, token, f'pulls/{number}')
    if code != 200 or pr.get('state') != 'open' or pr.get('head', {}).get('ref') != branch or pr['head'].get('sha') != expected or pr['head']['repo']['id'] != pr['base']['repo']['id']:
        raise ValueError('PR 状态、来源或远端提交已变化，请重新核对')
    source = _stage_rel(job['stage_dir'])
    target = _stage_rel(job['target_dir'])
    stage = project/'05_Gitea协作同步区'/source
    if not source.parts[0].startswith('STAGE_') or stage.resolve() != stage or not stage.is_dir():
        raise ValueError('请从本课题同步区下的 STAGE_ 目录提交')
    manifest = job.get('file_hashes')
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('请列出本次交付的文件和 SHA-256')
    with tempfile.TemporaryDirectory(prefix='rh-pr-') as tmp:
        def git(*args, data=None):
            return subprocess.run(['git','-c','core.hooksPath=/dev/null',*args],cwd=tmp,input=data,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
        git('init','--bare','--quiet')
        ref = 'refs/heads/'+branch
        git('check-ref-format',ref)
        result = _run_git_with_token(['fetch','--no-tags','--depth=1',repo_url,ref],Path(tmp),user,token)
        if result.returncode or git('rev-parse','FETCH_HEAD').decode().strip() != expected:
            raise ValueError('无法获取指定的 PR 版本')
        git('read-tree',expected)
        for name, digest in manifest.items():
            rel = _stage_rel(name)
            path = stage/rel
            if path.resolve() != path or not path.is_file() or path.suffix.lower() in BLOCKED_GIT_EXTS or path.stat().st_size > MAX_GIT_FILE_BYTES:
                raise ValueError(f'暂存文件不符合交付限制：{rel}')
            with path.open('rb') as f:
                data = f.read(MAX_GIT_FILE_BYTES+1)
            if len(data)>MAX_GIT_FILE_BYTES or hashlib.sha256(data).hexdigest()!=digest:
                raise ValueError(f'文件内容或长度已变化：{rel}')
            dest = (target/rel).as_posix()
            # Refuse replacing symlinks, submodules, or directories in the remote tree.
            for ancestor in (target/rel).parents:
                if ancestor == Path('.'): break
                entry = git('ls-tree',expected,'--',ancestor.as_posix())
                if entry and not entry.startswith(b'040000 '): raise ValueError('目标父路径不是目录')
            entry = git('ls-tree',expected,'--',dest)
            mode = entry.split(b' ',1)[0].decode() if entry else '100644'
            if mode not in ('100644','100755'): raise ValueError('仓库目标不是普通文件')
            blob = git('hash-object','-w','--stdin',data=data).decode().strip()
            git('update-index','--add','--cacheinfo',mode,blob,dest)
        tree = git('write-tree').decode().strip()
        changes = git('-c','core.quotePath=false','diff','--cached','--name-status',expected).decode()
        if not changes: return True, f'PR #{number} 已包含全部指定文件，提交 {expected}'
        if job.get('publish') is not True:
            return True, f'PR #{number} 预检通过，基线 {expected}，远端尚未写入。\n{changes}'
        msg = job.get('commit_message') or f'Update deliverables for PR #{number}'
        commit = git('commit-tree',tree,'-p',expected,'-m',msg).decode().strip()
        pushed = _run_git_with_token(['push',repo_url,f'{commit}:{ref}'],Path(tmp),user,token)
        if pushed.returncode: raise RuntimeError('普通推送失败，请重新查询远端提交')
        code, after = _gitea_api(repo_url,token,f'pulls/{number}')
        if code!=200 or after['head']['sha']!=commit:
            raise RuntimeError(f'推送已返回成功，待核对 PR #{number} 的提交 {commit}')
        return True, f'PR #{number} 已更新并核实提交 {commit}\n{changes}'


def _handle_gitea_push_pr(project_dir: Path, job: Dict[str, Any], dotenv: Dict[str, str]) -> Tuple[bool, str]:
    repo_url, user, token, _, _ = _load_gitea_cfg(project_dir, dotenv)
    if not repo_url or not token:
        return False, "缺少 Gitea repo_url 或 ~/.config/research_hub/.env 中的 GITEA_TOKEN"

    if 'stage_dir' in job or 'pr_number' in job:
        return _push_existing_pr(project_dir, job, repo_url, user, token)

    sync_dir = project_dir / "05_Gitea协作同步区"
    repo_dir = sync_dir / "repo"
    _guard_checkout(repo_dir,repo_url)
    if not (repo_dir / ".git").is_dir():
        return False, "尚未初始化 05_Gitea协作同步区/repo，请先执行 gitea_pull"

    branch = str(job.get("branch", "")).strip()
    if not re.fullmatch(r"feat/[\w][\w./-]*", branch) or ".." in branch:
        return False, f"安全拦截：禁止直接 push 主分支 `{branch}`，请使用特性分支（如 feat/bing-update）"

    # Guard against large binaries / archives bloating 40GB Gitea server
    def candidates():
        for base, dirs, files in os.walk(repo_dir,followlinks=False):
            if any(protected(name) for name in dirs): raise ValueError('工作树含保护目录，已停止提交')
            dirs[:] = [name for name in dirs if name != '.git']
            for name in files:
                yield Path(base)/name
    for p in candidates():
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() in BLOCKED_GIT_EXTS:
            return False, f"拦截：禁止向 Gitea 提交压缩包/大二进制 `{p.relative_to(repo_dir)}`，请改走 tailscale_cp 或 Release 临时中转"
        if p.stat().st_size > MAX_GIT_FILE_BYTES:
            return False, f"拦截：文件 `{p.relative_to(repo_dir)}` 超过 10 MB 限制（40G 硬盘保护）"

    listed = subprocess.run(['git','show-ref','--verify','--quiet','refs/heads/'+branch],cwd=repo_dir).returncode == 0
    checkout = ['checkout',branch] if listed else ['checkout','-b',branch]
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *checkout], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    st = subprocess.run(["git", "status", "--short"], cwd=str(repo_dir), capture_output=True, text=True)
    if not st.stdout.strip():
        return True, "工作区无新增改动，跳过 commit/push。"

    commit_msg = str(job.get("commit_message", "chore: sync updates from Claude workspace")).strip()
    c_res = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", commit_msg], cwd=str(repo_dir), capture_output=True, text=True)
    if c_res.returncode != 0:
        return False, f"git commit 失败：\n{c_res.stderr}\n{c_res.stdout}"

    _guard_push_objects(repo_dir,branch)
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
    latest = sync_details(project_dir,dotenv,pr_report=report)
    return True, latest + "\n" + report


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


def _gitea_delivery(project, job, dotenv):
    import hashlib
    repo,user,token,_,keywords = _load_gitea_cfg(project,dotenv)
    if not token: raise ValueError('缺少 Gitea 令牌')
    action = job['action']
    def request(endpoint, method='GET', payload=None):
        code,data = _gitea_api(repo,token,endpoint,method,payload)
        if not 200 <= code < 300: raise ValueError(f'Gitea {method} {endpoint} 返回 HTTP {code}')
        return data
    if action != 'gitea_release_upload':
        number = int(job['issue_number'])
        prefix = f'issues/{number}'
        item = request(prefix)
        if item.get('pull_request') or not _is_my_gitea_item(item,user,keywords): raise ValueError('目标不是本人相关工单')
        if action == 'gitea_set_issue_state':
            state = job['state']
            if state not in ('open','closed'): raise ValueError('state 使用 open 或 closed')
            if item['state']!=state: request(prefix,'PATCH',{'state':state})
            if request(prefix)['state']!=state: raise ValueError('工单状态核对失败')
            return True,f'工单 #{number} 状态已核验为 {state}'
        if job.get('comment_id'):
            comment = int(job['comment_id'])
            linked = issue_inbox.pages(lambda ep:_gitea_api(repo,token,ep),prefix+'/comments',item.get('comments'))
            if not any(c['id']==comment for c in linked): raise ValueError('评论不属于目标工单')
            prefix=f'issues/comments/{comment}'
    path = _safe_rel_path(project,job['file'],('02_实验与验证','04_组会与周报_Weekly','05_Gitea协作同步区'))
    if not path.is_file() or any(part.startswith('.') for part in path.relative_to(project).parts): raise ValueError('仅接受课题交付文件')
    if action!='gitea_release_upload' and path.stat().st_size>10_000_000: raise ValueError('超过 10 MB 请使用 Release')
    if action=='gitea_release_upload':
        tag=job['tag']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}',tag): raise ValueError('tag 格式无效')
        route='releases/tags/'+tag
        code,release=_gitea_api(repo,token,route)
        if code==404 and job.get('create_release') is True:
            release=request('releases','POST',{'tag_name':tag,'name':tag,'body':'课题临时交接。结单后 7～14 天清理。'})
        elif code!=200: raise ValueError(f'Release 查询 HTTP {code}；新建需 create_release=true')
        prefix=f"releases/{int(release['id'])}"
    endpoint=prefix+'/assets'
    if any(a.get('name')==path.name for a in request(endpoint)): raise ValueError('同名附件已存在，请核对原交付')
    parsed=urllib.parse.urlsplit(repo); owner_repo=parsed.path.strip('/').removesuffix('.git')
    url=f'{parsed.scheme}://{parsed.netloc}/api/v1/repos/{owner_repo}/{endpoint}?'+urllib.parse.urlencode({'name':path.name})
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',token): raise ValueError('令牌格式无效')
    # Supply raw file bytes through stdin; token goes through a private 0600 config.
    with tempfile.NamedTemporaryFile(mode='w') as config, path.open('rb') as source:
        config.write(f'header = "Authorization: token {token}"\n');config.flush()
        result=subprocess.run(['/usr/bin/curl','-q','--noproxy','*','--config',config.name,'--silent','--show-error','--fail','--max-time','600','-F','attachment=@-;filename=upload.bin',url],stdin=source,capture_output=True,timeout=610)
    if result.returncode: raise ValueError(f'附件上传失败 curl={result.returncode}，重试前核对服务器附件列表')
    attachment=json.loads(result.stdout)
    if attachment.get('name')!=path.name or attachment.get('size')!=path.stat().st_size: raise ValueError('附件已请求上传，服务器文件名或大小未核对成功')
    return True,f"已上传 {path.name}，服务器附件 ID {attachment['id']}，{attachment['size']} 字节。"


def _handle_tailscale_cp(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    rel = str(job.get("file", "")).strip()
    peer = str(job.get("peer", "")).strip()
    trust = json.loads((Path.home()/".config/research_hub/dispatch_trust.json").read_text())
    if peer not in trust.get("taildrop_peers", ["win-codex"]) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", peer):
        return False, f"非法 Tailscale 节点名：{peer}"
    src = _safe_rel_path(project_dir, rel, ("02_实验与验证", "04_组会与周报_Weekly", "05_Gitea协作同步区"))
    if not src.is_file():
        return False, f"找不到待传文件：{rel}"
    ts_bin = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not Path(ts_bin).is_file():
        return False, "本机未找到 tailscale CLI"
    res = subprocess.run([ts_bin, "file", "cp", str(src), f"{peer}:"], capture_output=True, text=True, timeout=180)
    return res.returncode == 0, f"tailscale file cp -> {peer}:\nstdout: {res.stdout}\nstderr: {res.stderr}"


def _handle_lit_fetch(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    """Run zero-dependency lit_fetch (Crossref DOI/BibTeX + EuropePMC/PubMed/bioRxiv search)."""
    import lit_fetch
    doi = str(job.get("doi") or "").strip()
    query = str(job.get("query") or "").strip()
    limit = int(job.get("limit") or 5)
    out_rel = str(job.get("output") or "").strip()
    if not doi and not query:
        return False, "lit_fetch 需要提供 `doi` 或 `query` 字段"
    payload: Dict[str, Any] = {}
    if doi:
        payload["doi_result"] = lit_fetch.fetch_by_doi(doi)
    if query:
        payload["query_results"] = lit_fetch.search_europe_pmc(query, limit=max(1, min(limit, 25)))
    formatted = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_rel:
        dst = _safe_rel_path(project_dir, out_rel, ("01_论文与协议", "02_实验与验证", "_bus"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        issue_inbox.replace(dst, formatted + "\n")
    return True, f"文献核验成功（已查询 Crossref/EuropePMC 公开接口）：\n```json\n{formatted[:6000]}\n```"


def _handle_scout_request(project_dir: Path, job: Dict[str, Any]) -> Tuple[bool, str]:
    """Register a deep literature/divergent-ideation request for host Antigravity (Gemini) via _bus/scout_ask.md."""
    import scout_bus
    topic = str(job.get("topic") or "文献与发散调研请求").strip()
    questions = job.get("questions") or []
    if isinstance(questions, str):
        questions = [questions]
    context_txt = str(job.get("context") or "").strip()
    target_file = str(job.get("target_file") or "01_论文与协议/文献事实卡片.md").strip()
    ask_file = scout_bus.enqueue_request(
        project_dir=project_dir,
        job_id=str(job.get("job_id") or "scout"),
        topic=topic,
        context_txt=context_txt,
        questions=[str(q) for q in questions],
        target_file=target_file,
    )
    _notify_macos(
        "ResearchHub 收到宿主机深度调研单",
        f"{project_dir.name} · {topic[:30]}",
        "请在宿主机 Antigravity 对话框输入：处理调研单",
    )
    return (
        True,
        f"已登记宿主机深度调研单：`{ask_file.relative_to(project_dir)}`（目标产出：`{target_file}`）。\n"
        f"请提示 PI 在宿主机 Antigravity 窗口输入：**「处理调研单」**，完成后回执将自动写入 `_bus/scout_reply.md` 并在 `01_PROJECT_STATE.md` 顶部提醒。",
    )


def process_project(project_dir: Path, dotenv: Dict[str, str]) -> int:
    disp = project_dir / "_dispatch"
    pending_dir = disp / "pending"
    running_dir = disp / "running"
    done_dir = disp / "done"
    archive_dir = disp / "archive"
    for d in (pending_dir, running_dir, done_dir, archive_dir):
        if d.resolve() != d.absolute(): raise PermissionError("队列目录不能为软链接")
        d.mkdir(parents=True, exist_ok=True)

    jobs = sorted(pending_dir.glob("*.json"))
    count = 0
    for jf in jobs:
        run_path = running_dir / jf.name
        if jf.is_symlink() or jf.name.startswith("."): continue
        try: jf.rename(run_path)
        except FileNotFoundError: continue
        started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok = False
        detail = ""
        action = "unknown"
        job = {}
        log_path = None
        job_id = jf.stem
        try:
            job = json.loads(run_path.read_text(encoding="utf-8"))
            requested = str(job.get("job_id") or jf.stem)
            if not re.fullmatch(r"[\w][\w.-]{0,119}", requested): raise ValueError('job_id 格式错误')
            job_id = requested
            action = str(job.get("action") or "").strip()
            if action in ('local_py','win_codex_py','remote_py','colab_py'):
                log_file = running_dir / (job_id+'.log')
                log_file.touch(exist_ok=False)
                log_path = log_file
                job['_log'] = log_path
            if action == 'colab_check':
                active=colab_jobs.preflight()
                ok,detail=True,f'宿主机后台认证成功；Google 返回 {len(active)} 个活动运行时。本次检查只读。'
            elif action == 'colab_py':
                job['job_id']=job_id
                ok,detail=colab_jobs.run(project_dir,job,_safe_rel_path)
            elif action in ('gitea_set_issue_state','gitea_issue_attachment','gitea_release_upload'):
                ok,detail=_gitea_delivery(project_dir,job,dotenv)
            elif action == "local_py":
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
            elif action == "lit_fetch":
                ok, detail = _handle_lit_fetch(project_dir, job)
            elif action == "scout_request":
                ok, detail = _handle_scout_request(project_dir, job)
            else:
                ok, detail = False, f"拒绝执行：未知或不在白名单内的 action=`{action}`"
        except Exception as exc:
            ok, detail = False, f"执行异常：{type(exc).__name__}: {exc}"
            if action == 'colab_py' and log_path and log_path.is_file():
                with log_path.open('rb') as src:
                    src.seek(max(0,log_path.stat().st_size-3000))
                    detail += '\n'+src.read().decode('utf8','replace')

        finished = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            from zoneinfo import ZoneInfo
            now_u = datetime.datetime.now(datetime.timezone.utc)
            la_fin = now_u.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
            dual_time_note = f"{started} → {finished} (宿主机北京时间 UTC+8 ｜ 对应虚拟机洛城时间: {la_fin}；项目文档归档日期一律以北京时间日期为准)"
        except Exception:
            dual_time_note = f"{started} → {finished} (宿主机北京时间 UTC+8)"
        status_str = "SUCCESS" if ok else "FAILED"
        result_md = (
            f"# 任务回执：{job_id}\n\n"
            f"- **课题**：`{project_dir.name}`\n"
            f"- **白名单动作**：`{action}`\n"
            f"- **状态**：**{status_str}**\n"
            f"- **开始 / 结束**：{dual_time_note}\n\n"
            f"## 执行详情\n{detail}\n"
        )
        result_file = done_dir / f"{job_id}_result.md"
        issue_inbox.replace(result_file,result_md)
        if log_path is not None:
            os.replace(log_path,done_dir/log_path.name)
        if isinstance(job,dict): job.pop('_log',None)
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
            f"ResearchHub 工单执行{'成功' if ok else '失败'}",
            f"{project_dir.name} · {job_id} ({action})",
            "已写入执行回执，打开大屏按工单编号查询。",
        )
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="宿主机白名单任务总线执行器（_dispatch/）")
    parser.add_argument("--project", help="仅监听指定课题目录（默认扫描 01_在研课题_Projects 下全部课题）")
    parser.add_argument("--once",action="store_true",help="单次处理队列（默认）")
    parser.add_argument("--watch", action="store_true", help="常驻轮询监听 _dispatch/pending/（每 2 秒）")
    parser.add_argument("--interval", type=float, default=2.0, help="watch 轮询间隔秒数（默认 2.0）")
    args = parser.parse_args()

    lock_dir = Path.home()/'.local/state/researchhub'
    lock_dir.mkdir(parents=True,exist_ok=True)
    lock = (lock_dir/'dispatch.lock').open('a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print('已有执行器持锁'); return 1
    config = consult.load_toml(HUB_DIR / "models.toml")
    env_path = Path(str(config.get("general", {}).get("env_file", "~/.config/research_hub/.env"))).expanduser()

    def _targets() -> List[Path]:
        if args.project:
            return [consult.resolve_project_dir(args.project, config)]
        return [p for p in sorted(PROJECTS_DIR.iterdir()) if p.is_dir() and not p.is_symlink() and re.match(r"P\d+_",p.name)]

    if not args.watch:
        dotenv = consult.load_dotenv(env_path)
        total = sum(process_project(p, dotenv) for p in _targets())
        print(f"[Dispatch] 单次扫描完成，共处理 {total} 个任务。")
        return 0

    # A killed worker cannot prove whether a remote run finished. Preserve its receipts
    # and never automatically replay a potentially side-effecting job.
    for project in _targets():
        running = project/'_dispatch/running'
        done = project/'_dispatch/done'
        archive = project/'_dispatch/archive'
        if any(path.is_symlink() or path.resolve()!=path for path in (running,done,archive)): continue
        for stale in running.glob('*.json'):
            if stale.is_symlink(): continue
            try:
                payload = json.loads(stale.read_text())
                ident = str(payload.get('job_id') or stale.stem)
            except (ValueError,AttributeError): ident = stale.stem
            if not re.fullmatch(r'[\w][\w.-]{0,119}',ident): ident = stale.stem
            done.mkdir(exist_ok=True); archive.mkdir(exist_ok=True)
            receipt = done/(ident+'_result.md')
            if not receipt.exists():
                issue_inbox.replace(receipt, f'# 任务回执：{ident}\n\n- **状态**：**INTERRUPTED**\n\n宿主执行器中断。请核对目标机和已有产物，再决定是否重新投递。\n')
            log = running/(ident+'.log')
            if log.is_file() and not log.is_symlink(): os.replace(log,done/log.name)
            os.replace(stale,archive/stale.name)
    def reconcile_cloud():
        while True:
            # Only the worker main loop owns a live lease. Recovery runs before accepting work.
            try:
                colab_jobs.recover()
            except Exception as exc:
                print(f'[Colab] 待核对云运行时：{exc}',flush=True)
            return
    next_cloud_check = 0.0
    def poll():
        while True:
            for proj in _targets():
                cfg = proj/'05_Gitea协作同步区/.gitea_repo.json'
                try:
                    if cfg.is_file() and json.loads(cfg.read_text()).get('enabled') is True:
                        sync_details(proj,consult.load_dotenv(env_path))
                except Exception as exc:
                    print(f'[Gitea] {proj.name} 同步未完成：{exc}',flush=True)
            time.sleep(120)
    threading.Thread(target=poll,name='issue-sync',daemon=True).start()
    print(f"[Dispatch] 已启动白名单常驻监听（间隔 {args.interval}s，0 AI 消耗）。按 Ctrl+C 退出。", flush=True)
    try:
        while True:
            dotenv = consult.load_dotenv(env_path)
            from service_reload import reload_changed
            reload_changed()
            if time.monotonic() >= next_cloud_check:
                reconcile_cloud()
                next_cloud_check = time.monotonic() + 120
            for p in _targets():
                try: process_project(p, dotenv)
                except (OSError,ValueError) as exc: print(f"[Dispatch] {p.name}: {exc}",flush=True)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        print("\n[Dispatch] 已停止监听。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
