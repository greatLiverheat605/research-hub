"""Host-only publisher for the project state file (in-place inode preservation + 0644 content-addressed .state_cache)."""
import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

STATE = Path('_control/01_PROJECT_STATE.md')
CACHE_DIR = Path('_control/.state_cache')
MAX_BYTES = 2 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def durable(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(data)
            tmp.flush()
            os.fchmod(tmp.fileno(), mode)
            os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def storage(project: Path) -> Path:
    root = Path(os.environ.get('RH_STATE_HISTORY', Path.home() / '.local/state/researchhub/project-state'))
    folder = root / (project.name + '-' + digest(os.fsencode(project.absolute()))[:16])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def checkpoint(folder: Path, data: bytes) -> None:
    path = folder / (digest(data) + '.md')
    if not path.exists():
        durable(path, data, mode=0o600)
    if path.read_bytes() != data:
        raise IOError('状态快照校验失败')


def _write_project_state_cache(project: Path, data: bytes, ino: int = 0) -> None:
    """在 _control/.state_cache/<sha256>.md 落盘 0644 零缓存内容寻址副件：
    - 严禁在原路径 _control/01_PROJECT_STATE.md 上使用 os.replace（否则客机 VirtioFS namei 缓存指向被删旧 Inode 会报 FileNotFoundError）；
    - 同时每次提交生成一个以新 <sha256>.md 命名的全新 0644 快照文件（新文件名在客机 100% 无历史 namei/UBC 缓存），供客机秒级校验与自愈。
    """
    sha = digest(data)
    cache_root = project / CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    snap = cache_root / f'{sha}.md'
    if not snap.exists() or (snap.stat().st_mode & 0o777) != 0o644:
        durable(snap, data, mode=0o644)
    meta = json.dumps(
        {'sha256': sha, 'bytes': len(data), 'inode': ino, 'updated_at': int(time.time())},
        ensure_ascii=False,
    ).encode('utf-8')
    durable(cache_root / 'latest.json', meta, mode=0o644)
    try:
        snaps = sorted(cache_root.glob('*.md'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_s in snaps[16:]:
            old_s.unlink(missing_ok=True)
    except OSError:
        pass


def overwrite(stream, data: bytes, path: Path) -> None:
    stream.seek(0)
    if stream.write(data) != len(data):
        raise IOError('状态写入不完整')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())
    if not os.path.samestat(os.fstat(stream.fileno()), path.stat()) or path.read_bytes() != data:
        raise IOError('原路径读回校验失败')


def publish(project, base, data, recover=False):
    project = Path(project).absolute()
    path = project / STATE
    if path.resolve() != path or not data.strip() or len(data) > MAX_BYTES:
        raise ValueError('状态路径或内容无效')
    data.decode('utf-8')
    new_sha = digest(data)
    folder = storage(project)
    journal = folder / 'pending.json'
    with (folder / 'lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with os.fdopen(os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK), 'r+b') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('状态原件必须为普通文件')
            old = stream.read(MAX_BYTES + 1)
            if len(old) > MAX_BYTES:
                raise ValueError('原件超出大小限制')
            observed = digest(old)
            if journal.exists():
                pending = json.loads(journal.read_text())
                if observed not in (pending['old'], pending['new']) and not recover:
                    raise RuntimeError('上次发布中断，保留现场；请用 restore 核对恢复。断点 ' + str(journal))
            # 允许客机在 submit 时已通过客机自身 VFS 率先刷入 new_sha（此时 observed == new_sha）
            if base is not None and observed not in (base, new_sha) and not recover:
                raise ValueError('版本冲突，请重读原件。当前 SHA-256 ' + observed)
            checkpoint(folder, old)
            checkpoint(folder, data)
            _write_project_state_cache(project, data, ino=info.st_ino)
            if old != data:
                durable(journal, json.dumps({'old': observed, 'new': new_sha}).encode(), mode=0o600)
                try:
                    overwrite(stream, data, path)
                except Exception:
                    overwrite(stream, old, path)
                    raise
            journal.unlink(missing_ok=True)
            return {'sha256': new_sha, 'bytes': len(data), 'inode': info.st_ino, 'previous_sha256': base or observed}


def main():
    parser = argparse.ArgumentParser(description='从宿主私有断点恢复项目状态')
    parser.add_argument('action', choices=['restore'])
    parser.add_argument('project', type=Path)
    parser.add_argument('snapshot_sha256')
    parser.add_argument('current_sha256')
    args = parser.parse_args()
    import re
    if not re.fullmatch('[a-f0-9]{64}', args.snapshot_sha256):
        parser.error('快照 SHA-256 无效')
    data = (storage(args.project.absolute()) / (args.snapshot_sha256 + '.md')).read_bytes()
    if digest(data) != args.snapshot_sha256:
        raise ValueError('快照校验失败')
    print(json.dumps(publish(args.project, args.current_sha256, data, recover=True)))


if __name__ == '__main__':
    main()
