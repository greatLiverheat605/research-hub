"""Read-only queue inspection, including work that has no SQLite record yet."""
import fcntl
import json
import re
from pathlib import Path
from datetime import datetime


def bounded(path, limit, tail=False):
    if path.is_symlink() or not path.is_file(): return ''
    with path.open('rb') as src:
        if tail: src.seek(max(0,path.stat().st_size-limit))
        return src.read(limit).decode('utf8','replace')


def checkpoint_series(project):
    root = Path.home()/'.local/state/researchhub/colab/checkpoints'/project.name
    series = []
    if not root.is_dir() or root.resolve() != root:
        return series
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', folder.name):
            continue
        versions = []
        for version in folder.iterdir():
            if version.is_symlink() or not re.fullmatch(r'[0-9]{16}', version.name) or not version.is_dir():
                continue
            payload = version/'state.bin'
            try:
                digest = bounded(version/'sha256', 65)
                if not re.fullmatch(r'[a-f0-9]{64}', digest) or payload.is_symlink() or not payload.is_file():
                    continue
                stat = payload.stat()
                versions.append(dict(step=str(int(version.name)), size=stat.st_size, saved=stat.st_mtime))
            except FileNotFoundError:
                continue  # Retention can rotate the previous generation during a refresh.
        versions.sort(key=lambda v: int(v['step']), reverse=True)
        series.append(dict(key=folder.name, versions=versions[:2]))
    return sorted(series, key=lambda s: s['versions'][0]['saved'] if s['versions'] else 0, reverse=True)


def checkpoint_attempt(project, item):
    ident = item['id']
    if not re.fullmatch(r'[\w][\w.-]{0,119}', ident):
        return None
    folder = {'RUNNING':'running','PENDING':'pending'}.get(item['status'],'done')
    log = project/'_dispatch'/folder/(ident+'.log')
    if log.parent != log.parent.resolve():
        return None
    try:
        text = bounded(log, 8192) + '\n' + bounded(log, 8192, True)
    except FileNotFoundError:
        return None
    match = re.search(r'^检查点 ([A-Za-z0-9_-]+) · (?:首次运行|将从 step=\d+ 续跑)', text, re.M)
    if not match:
        return None
    resumed = re.search(r'^已恢复 Mac 检查点 step=(\d+)', text, re.M)
    saves = re.findall(r'^检查点已保存到 Mac · [^\n]+? · step=(\d+)', text, re.M)
    return dict(key=match[1], resumed=resumed[1] if resumed else None, saved=saves[-1] if saves else None)


def latest_progress(log_text):
    # Only interpret entire, explicit records. Arbitrary counters in stdout may be metrics.
    for line in reversed(log_text.splitlines()):
        if not line.startswith('RH_PROGRESS '):
            continue
        try:
            data = json.loads(line[12:])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        done, total = data.get('done'), data.get('total')
        if type(done) is not int or not 0 <= done <= 2**53-1:
            continue
        if total is not None and (type(total) is not int or not max(done, 1) <= total <= 2**53-1):
            continue
        phase = data.get('phase', '')
        return dict(done=done, total=total, phase=phase[:80] if isinstance(phase, str) else '')
    return None


def snapshot(project, query=''):
    root=project/'_dispatch'
    items=[]
    for folder,pattern in [('pending','*.json'),('running','*.json'),('done','*_result.md')]:
        directory=root/folder
        if directory.resolve()!=directory: continue
        for path in directory.glob(pattern):
            if path.is_symlink() or path.name.startswith('.'): continue
            try:
                progress = None
                modified=path.stat().st_mtime
                verdict_info = {'verdict': 'UNKNOWN', 'summary': ''}
                stage_info = {'stage': '', 'last_line': ''}
                if folder=='done':
                    ident=path.name[:-len('_result.md')]
                    if query.lower() not in ident.lower(): continue
                    text=bounded(path,8192)
                    match=re.search(r'\*\*状态\*\*[：:]\s*\*\*(\w+)\*\*',text)
                    status=match[1] if match else 'UNKNOWN'
                    match=re.search(r'\*\*白名单动作\*\*[：:]\s*`([^`]+)`',text)
                    action=match[1] if match else ''
                    full_detail=text.partition('## 执行详情')[2].strip()
                    if status=='FAILED' and not full_detail: full_detail=text[-1400:]
                    gate_fails=[]
                    gate_passes=[]
                    for m in re.finditer(r'\b([A-Z0-9_]+(?:_CHECK|_GATE|_TEST|_VERIFY|_AUDIT))\s+(FAIL|FAILED|PASS|OK)\b([^\n]*)', full_detail):
                        gname, gstat, grest = m[1], m[2], m[3].strip()
                        if gstat in ('FAIL', 'FAILED'):
                            false_keys = re.findall(r"'([^']+)'\s*:\s*False", grest)
                            suffix = f" ({', '.join(false_keys)})" if false_keys else (f" {grest[:80]}" if grest else "")
                            gate_fails.append(f"{gname} FAIL{suffix}")
                        else:
                            gate_passes.append(f"{gname} PASS")
                    if status != 'SUCCESS':
                        verdict_info = {'verdict': 'EXEC_FAIL', 'summary': '执行进程异常退出'}
                    elif gate_fails:
                        verdict_info = {'verdict': 'GATE_FAIL', 'summary': '; '.join(dict.fromkeys(gate_fails))}
                    elif '未收敛' in full_detail or 'WARNING' in full_detail:
                        verdict_info = {'verdict': 'WARN', 'summary': '执行完成但含告警/未收敛项'}
                    elif gate_passes or 'COMPLETE_OK' in full_detail:
                        verdict_info = {'verdict': 'PASS', 'summary': '; '.join(dict.fromkeys(gate_passes)) or 'COMPLETE_OK'}
                    else:
                        verdict_info = {'verdict': 'PASS', 'summary': '执行完成'}
                    if status!='SUCCESS' or verdict_info['verdict']=='GATE_FAIL':
                        detail=full_detail[-1400:]
                    else:
                        detail=full_detail[:140]
                    stage_info = {'stage': '5. 已结束', 'last_line': verdict_info['summary']}
                else:
                    try: job=json.loads(bounded(path,65536))
                    except ValueError: job={}
                    if not isinstance(job,dict): job={}
                    ident=str(job.get('job_id') or path.stem);action=str(job.get('action') or '')
                    if query.lower() not in ident.lower(): continue
                    status=folder.upper();detail=''
                    stage_label = '0. 排队待接单' if folder=='pending' else '1. 核对授权与备齐输入'
                    last_line = ''
                    if folder=='running' and re.fullmatch(r'[\w][\w.-]{0,119}',ident):
                        tail = bounded(directory/(ident+'.log'), 65536, True)
                        detail = tail[-1600:]
                        progress = latest_progress(tail)
                        if '回传' in tail or '释放' in tail or 'unassign' in tail:
                            stage_label = '4. 回传校验与释放算力'
                        elif '执行实验脚本' in tail or 'RH_PROGRESS' in tail or 'Epoch' in tail or 'step' in tail.lower():
                            stage_label = '3. 执行实验脚本'
                        elif '分配' in tail or '连接' in tail or 'assign' in tail.lower():
                            stage_label = '2. 分配/连接算力实例'
                        lines = [ln.strip() for ln in tail.splitlines() if ln.strip() and not ln.strip().startswith('RH_PROGRESS')]
                        if lines: last_line = lines[-1][:140]
                    stage_info = {'stage': stage_label, 'last_line': last_line}
                    verdict_info = {'verdict': 'RUNNING' if folder=='running' else 'PENDING', 'summary': stage_label}
                items.append(dict(id=ident,action=action,status=status,verdict=verdict_info['verdict'],verdict_summary=verdict_info['summary'],stage=stage_info['stage'],last_line=stage_info['last_line'],detail=detail,updated=modified,progress=progress))
            except FileNotFoundError: continue
    lock=Path.home()/'.local/state/researchhub/dispatch.lock'
    alive=False
    if lock.exists():
        with lock.open('r') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:alive=True
    lease=Path.home()/'.local/state/researchhub/colab/lease.json'
    tracked=''
    try:
        data=json.loads(lease.read_text());tracked=data.get('job','')+' · '+data.get('session','')
    except (OSError,ValueError):pass
    items.sort(key=lambda i:(i['status'] not in ('PENDING','RUNNING'),-i['updated']))
    visible = items[:40]
    for item in visible:
        if item['action'] == 'colab_py':
            item['checkpoint'] = checkpoint_attempt(project, item)
    return dict(worker_online=alive, cloud_lease=tracked, jobs=visible, checkpoints=checkpoint_series(project))
