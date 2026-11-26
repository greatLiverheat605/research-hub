"""Run Colab jobs with multi-session lease pool, dynamic Drive auto-binding, optional deadlines, scoped file transfer and verified release."""
import base64
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from issue_inbox import replace
from protected_paths import protected
import colab_checkpoint

ROOT = Path.home() / '.local/state/researchhub/colab'
LEASES_DIR = ROOT / 'leases'
CONFIG = Path.home() / '.config/research_hub/colab_policy.json'
PYTHON = Path.home() / '.local/share/uv/tools/google-colab-cli/bin/python'
BRIDGE = Path(__file__).with_name('colab_bridge.py')

_STATE_LOCK = threading.Lock()
_ALLOC_LOCK = threading.Lock()
_ACTIVE_SESSIONS: set[str] = set()


@contextlib.contextmanager
def _file_lock(name: str = 'colab_state.lock'):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = ROOT / name
    with _STATE_LOCK:
        with lock_file.open('a') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def command(args, timeout, output=None):
    proc = subprocess.run(
        [str(PYTHON), '-I', '-u', str(BRIDGE), *args],
        stdin=subprocess.DEVNULL,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode:
        # Conserve privacy: errors can contain backend credentials. Surface actionable categories.
        err = proc.stderr.decode(errors='replace').lower()
        if proc.returncode == 77 or 'auth_required' in err:
            raise ValueError('AUTH_REQUIRED：请检查宿主机网络和登录钥匙串，再运行 colab sessions 授权。')
        if any(s in err for s in ('connection', 'timeout', 'proxy', 'ssl', 'network')):
            raise ValueError('NETWORK_ERROR：宿主机连接 Google 失败，请检查网络或代理。')
        if any(s in err for s in ('quota', 'capacity', 'too many active sessions', 'precondition failed', 'toomanyassignmentserror')):
            raise ValueError('CAPACITY：额度不足、云端并发会话数已达上限或云资源暂不可用。')
        raise ValueError(f'Colab {args[0]} 失败，退出码 {proc.returncode}。')
    return (proc.stdout or b'').decode(errors='replace').strip()


def preflight():
    return json.loads(command(['inspect'], 32))


def _is_pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _iter_lease_files() -> list[Path]:
    files: list[Path] = []
    legacy = ROOT / 'lease.json'
    if legacy.is_file():
        files.append(legacy)
    if LEASES_DIR.is_dir():
        files.extend(sorted(LEASES_DIR.glob('*.json')))
    return files


def _is_lease_active(data: dict) -> bool:
    sess = str(data.get('session') or '')
    pid = data.get('pid')
    if sess and sess in _ACTIVE_SESSIONS:
        return True
    if isinstance(pid, int) and pid != os.getpid() and _is_pid_alive(pid):
        return True
    return False


def _known_peer_endpoints(exclude_session: str = '') -> set[str]:
    endpoints: set[str] = set()
    for lf in _iter_lease_files():
        try:
            d = json.loads(lf.read_text())
        except Exception:
            continue
        if d.get('session') == exclude_session:
            continue
        ep = d.get('endpoint')
        if ep:
            endpoints.add(str(ep))
    return endpoints


def _release_lease_file(lease_path: Path) -> None:
    if not lease_path.exists():
        return
    try:
        data = json.loads(lease_path.read_text())
    except Exception:
        lease_path.unlink(missing_ok=True)
        return
    session = str(data.get('session') or '')
    endpoint = data.get('endpoint') or (command(['endpoint', session], 10) if session else '')
    if not endpoint:
        # Allocation may have succeeded before local store was updated. Exclude known peer endpoints.
        known = set(data.get('baseline') or []) | _known_peer_endpoints(exclude_session=session)
        if set(preflight()) - known:
            raise ValueError('RELEASE_PENDING：分配中断，有身份未确认的云实例，请在 Colab 管理会话中核对。')
    else:
        command(['release', session, endpoint], 35)
    lease_path.unlink(missing_ok=True)


def recover():
    """仅回收已结束/崩溃遗留的孤儿租约，绝不误杀当前正在并行运行的活跃 Colab 会话。"""
    for lf in _iter_lease_files():
        try:
            data = json.loads(lf.read_text())
        except Exception:
            lf.unlink(missing_ok=True)
            continue
        if _is_lease_active(data):
            continue
        _release_lease_file(lf)


def job_deadline(job):
    seconds = job.get('timeout_s')
    if seconds is None:
        return None
    if type(seconds) is not int or seconds < 0:
        raise ValueError('timeout_s 应为非负整数秒或 null')
    return time.monotonic() + seconds if seconds else None


def time_left(deadline, operation_limit):
    if deadline is None:
        return operation_limit
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError('达到本工单指定的时限')
    return left if operation_limit is None else min(left, operation_limit)


def _auto_stage_drive_input(project: Path, root: Path, rel: str) -> Path:
    """多源输入自动寻址与备齐（解决 Problem #1）：
    若已绑定 Drive 课题目录 (root/rel) 尚无该文件，自动按优先级从：
    1) My Drive 顶层目录 (root.parent.parent / rel，如 我的云端硬盘/P01_probe/train_only.csv)
    2) 宿主机私有输入舱 (~/.local/state/researchhub/inputs/<project.name>/<rel>)
    3) 课题 02_实验与验证/ 下的同名清单文件（仅限 *manifest*.json 等非测试集文件）
    安全复制到 root/rel，确保开机前自动备齐。
    """
    local = root / rel
    if local.is_file() and not local.is_symlink():
        return local

    candidates = []
    # 1) My Drive 顶层 (我的云端硬盘/<rel>)
    if root.parent and root.parent.parent:
        candidates.append(root.parent.parent / rel)
    # 2) 宿主机私有输入舱
    candidates.append(Path.home() / ".local/state/researchhub/inputs" / project.name / rel)
    # 3) 课题 02_实验与验证 下的同名 manifest/配置文件（严禁触碰 test_only 或冻结目录）
    fname = Path(rel).name
    if "test" not in fname.lower() and (fname.endswith(".json") or fname.endswith(".tsv")):
        exp_dir = project / "02_实验与验证"
        if exp_dir.is_dir():
            for found in sorted(exp_dir.rglob(fname)):
                if not any(p.startswith(("冻结", "_prev")) for p in found.parts):
                    candidates.append(found)

    for cand in candidates:
        try:
            if cand.is_file() and not cand.is_symlink() and cand.resolve() == cand:
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_name(f".stage_{uuid.uuid4().hex}_{local.name}")
                shutil.copy2(cand, tmp)
                os.replace(tmp, local)
                return local
        except OSError:
            continue
    return local


def resolve_drive_project_root(project: Path, policy: dict) -> Path:
    """动态解析并自动创建课题的桌面版 Google Drive 绑定目录（兼容未来新增 P04/P05 等任意课题）：
    1) 优先使用 policy['drive_projects'][project.name]；
    2) 若为新增课题（尚未在 drive_projects 中显式登记），自动使用 policy['drive_root']
       （或已有 drive_projects 路径的公共父目录 ResearchHub_Colab）拼接 <drive_root>/<project.name>；
    3) 若父目录 ResearchHub_Colab 已挂载在线而本课题子目录尚未创建，自动建立该子目录。
    """
    drive_projects = policy.get('drive_projects') or {}
    spec = drive_projects.get(project.name)
    if spec:
        root = Path(spec)
    else:
        drive_root_str = str(policy.get('drive_root') or '').strip()
        if not drive_root_str:
            parents = [str(Path(v).parent) for v in drive_projects.values() if v]
            drive_root_str = parents[0] if parents else ''
        if not drive_root_str:
            raise ValueError('DRIVE_UNAVAILABLE：未配置 drive_root 或本课题的桌面版 Drive 目录')
        root = Path(drive_root_str) / project.name

    if not root.is_absolute():
        raise ValueError('DRIVE_UNAVAILABLE：Drive 课题目录必须为绝对路径')
    parent = root.parent
    if not root.exists() and parent.is_dir() and not parent.is_symlink() and parent.resolve() == parent:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError('DRIVE_UNAVAILABLE：宿主机课题目录不可用，请检查桌面版 Drive')
    return root


def drive_files(project, job, policy):
    transfers = []
    for field in ('drive_inputs', 'drive_outputs'):
        items = job.get(field, [])
        if not isinstance(items, list):
            raise ValueError(f'{field} 应为课题内文件相对路径数组')
        transfers.append(items)
    inputs, outputs = transfers
    if not inputs and not outputs:
        return [], []
    root = resolve_drive_project_root(project, policy)
    seen = set()
    result = [[], []]
    for group, paths in enumerate(transfers):
        for rel in paths:
            if not isinstance(rel, str) or not rel or any(not part or part.startswith('.') for part in rel.split('/')) or protected(rel):
                raise ValueError('Drive 清单只接受课题内独立文件的相对路径')
            local = _auto_stage_drive_input(project, root, rel) if group == 0 else (root / rel)
            if local != local.resolve() or local in seen:
                raise ValueError('Drive 路径重复或经过符号链接')
            if (group == 0 and not local.is_file()) or (group == 1 and local.exists()):
                raise ValueError(f'Drive 输入缺失或输出已存在：{rel}')
            seen.add(local)
            result[group].append((local, rel))
    return result


def run(project, job, safe_path):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    LEASES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = job['_log']

    def stage(text):
        with log.open('a') as stream:
            print(text, file=stream, flush=True)

    stage('正在核对 Colab 授权与预算')
    recover()
    policy = json.loads(CONFIG.read_text())
    accelerator = str(job.get('accelerator', 'CPU')).upper()
    if accelerator not in policy['allowed_accelerators'] or accelerator not in ('CPU', 'T4', 'L4', 'A100', 'H100', 'G4'):
        raise ValueError('BUDGET_BLOCKED：宿主机策略未放行此加速器。')
    deadline = job_deadline(job)
    drive_in, drive_out = drive_files(project, job, policy)
    drive_dir = '/content/drive/MyDrive/ResearchHub_Colab/' + project.name
    script = safe_path(project, job.get('script', ''), ('03_代码与脚本',))
    if script.suffix != '.py' or not script.is_file() or script.stat().st_size > 2_000_000:
        raise ValueError('需要 2 MB 以内的 Python 入口脚本')
    args = job.get('args', [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError('args 应为字符串数组')
    files = []
    for field in ('inputs', 'outputs'):
        paths = job.get(field, [])
        if not isinstance(paths, list) or len(paths) > 32:
            raise ValueError('文件清单格式错误')
        paths = [safe_path(project, p, ('02_实验与验证',)) for p in paths]
        if any(not re.fullmatch(r'[\w.-]+', p.name) or not p.suffix for p in paths):
            raise ValueError('仅允许具备扩展名的独立文件')
        files.append(paths)
    inputs, outputs = files
    if any(not p.is_file() for p in inputs) or any(p.exists() for p in outputs):
        raise ValueError('输入缺失或目标产物已存在，请使用独立输出路径')
    if len({p.name for p in inputs + outputs}) != len(inputs + outputs):
        raise ValueError('云端输入和输出文件名必须各自唯一')
    ck_folder = None
    if 'checkpoint_key' in job:
        ck_folder = colab_checkpoint.store(
            ROOT,
            project,
            job['checkpoint_key'],
            script,
            args,
            [(p, '/content/' + p.name) for p in inputs] + list(drive_in),
        )
        versions = colab_checkpoint.history(ck_folder)
        stage('检查点 ' + ck_folder.name + ' · ' + (f'将从 step={int(versions[-1].name)} 续跑' if versions else '首次运行'))

    name = 'rh-' + uuid.uuid4().hex[:16]
    lease = LEASES_DIR / f'{name}.json'
    key = project.name + '/' + job['job_id']

    with _file_lock():
        ledger = ROOT / (datetime.date.today().isoformat() + '.json')
        reservations = json.loads(ledger.read_text()) if ledger.exists() else {}
        if key in reservations:
            raise ValueError('BUDGET_BLOCKED：今天已提交过同一 job_id。')
        reservations[key] = job.get('timeout_s') or 0
        replace(ledger, json.dumps(reservations))
        _ACTIVE_SESSIONS.add(name)

    def call(args, output=None, operation_limit=None):
        return command(args, time_left(deadline, operation_limit), output)

    awake = None
    try:
        awake = subprocess.Popen(
            ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 分配云实例时支持多开排队退避（当触发 Google 账号并发上限 CAPACITY 且有其他活跃会话时，自动等待重试）
        max_alloc_attempts = 4
        for attempt in range(1, max_alloc_attempts + 1):
            with _ALLOC_LOCK:
                baseline = preflight()
                record = dict(
                    session=name,
                    baseline=baseline,
                    job=key,
                    project=project.name,
                    accelerator=accelerator,
                    pid=os.getpid(),
                    started_at=datetime.datetime.now().isoformat(timespec='seconds'),
                )
                replace(lease, json.dumps(record))
                stage('正在分配 ' + accelerator + ' · ' + name + (f' (第 {attempt} 次尝试)' if attempt > 1 else ''))
                try:
                    call(['new', '-s', name] + ([] if accelerator == 'CPU' else ['--gpu', accelerator]), operation_limit=180)
                    record['endpoint'] = call(['endpoint', name], operation_limit=15)
                    replace(lease, json.dumps(record))
                    if not record['endpoint']:
                        raise ValueError('云端分配身份未确认')
                    break
                except ValueError as exc:
                    if 'CAPACITY' in str(exc) and attempt < max_alloc_attempts and len(_ACTIVE_SESSIONS) > 1:
                        wait_s = min(45, max(5, int(time_left(deadline, 45) or 45) - 5))
                        stage(f'云端并发槽位暂满 ({exc})，当前有其他课题并行运行中，等待 {wait_s}s 后自动重试...')
                    else:
                        raise
            time.sleep(wait_s)

        for p in inputs:
            call(['upload', '-s', name, str(p), '/content/' + p.name])
        setup = (
            "import os, pathlib, sys, types\n"
            f"os.environ['RH_DRIVE_DIR'] = {drive_dir!r}\n"
            "# 屏蔽交互式 google.colab.drive.mount 弹窗，统一使用宿主机预置的 /content/drive 目录 (解决 Problem #2)\n"
            "try:\n"
            "    import google.colab.drive as _gcd\n"
            "    _gcd.mount = lambda *a, **k: print('[RH] 已通过宿主机免交互通道挂载 Drive 目录')\n"
            "    _gcd.flush_and_unmount = lambda *a, **k: None\n"
            "except Exception:\n"
            "    pass\n"
        )
        if drive_in or drive_out:
            for local, rel in drive_in:
                upload = '/content/rh_input_' + uuid.uuid4().hex
                call(['upload', '-s', name, str(local), upload])
                dest = str(Path(drive_dir) / rel)
                setup += f"pathlib.Path({dest!r}).parent.mkdir(parents=True, exist_ok=True)\nos.replace({upload!r}, {dest!r})\n"
            for _, rel in drive_out:
                parent = str((Path(drive_dir) / rel).parent)
                setup += f"pathlib.Path({parent!r}).mkdir(parents=True, exist_ok=True)\n"
        with tempfile.TemporaryDirectory() as tmp:
            marker = 'RH_COMPLETED_' + uuid.uuid4().hex
            wrapper = Path(tmp) / 'entry.py'
            encoded = base64.b64encode(script.read_bytes()).decode()
            wrapper.write_text(
                setup
                + f'import base64,sys,pathlib\n'
                f'__file__={script.name!r}\n'
                f'sys.argv={[script.name,*args]!r}\n'
                f'_rh_script_bytes=base64.b64decode({encoded!r})\n'
                f'pathlib.Path(__file__).write_bytes(_rh_script_bytes)\n'
                f'exec(compile(_rh_script_bytes,__file__,"exec"),globals())\n'
                f'print({marker!r},flush=True)\n'
            )
            stage('云端已分配，开始执行 ' + script.name)
            if ck_folder is not None:
                colab_checkpoint.execute(ck_folder, call, name, Path(tmp), script, args, setup, marker, log, stage)
            else:
                with log.open('ab', buffering=0) as out:
                    call(['exec', '-s', name, '--timeout', '0', '-f', str(wrapper)], out)
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size - 65536))
                tail = f.read().decode(errors='replace')
            if marker not in [line.strip() for line in tail.splitlines()]:
                raise ValueError('云端脚本未正常返回，请查看运行日志尾部。')
            for p in outputs:
                local = Path(tmp) / p.name
                call(['download', '-s', name, '/content/' + p.name, str(local)])
            drive_results = []
            for dest, rel in drive_out:
                local = Path(tmp) / ('rh_output_' + uuid.uuid4().hex)
                call(['download', '-s', name, str(Path(drive_dir) / rel), str(local)])
                drive_results.append((local, dest))
            for local, dest in drive_results:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.resolve() != dest:
                    raise ValueError('Drive 输出路径发生变化')
                with dest.open('xb') as out:
                    try:
                        with local.open('rb') as src:
                            shutil.copyfileobj(src, out)
                    except BaseException:
                        dest.unlink(missing_ok=True)
                        raise
            if drive_results:
                stage(f'已写入 Mac 的 Drive 课题目录 {len(drive_results)} 个文件，云端同步由桌面版 Drive 完成')
            for p in outputs:
                p.parent.mkdir(parents=True, exist_ok=True)
                os.replace(Path(tmp) / p.name, p)
    finally:
        stage('正在向 Google 确认释放运行时')
        try:
            _release_lease_file(lease)
        except Exception as exc:
            stage('RELEASE_PENDING · ' + name)
            raise RuntimeError('RELEASE_PENDING：请核对云端，已阻止继续开机。' + str(exc)) from exc
        finally:
            with _file_lock():
                _ACTIVE_SESSIONS.discard(name)
            if awake is not None:
                awake.terminate()
                awake.wait()
        stage('云端释放已确认')
    return True, f'Colab {accelerator} 完成，云端释放已确认，回收产物 {len(outputs) + len(drive_out)} 个。\n' + tail[-2000:]
