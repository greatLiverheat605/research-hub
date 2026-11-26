"""Host-only publisher for the project state file with VirtioFS UBC page-cache invalidation."""
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


def durable(path: Path, data: bytes, mode: int = 0o644) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.rh_state_', delete=False) as tmp:
        try:
            tmp.write(data)
            tmp.flush()
            os.fchmod(tmp.fileno(), mode)
            os.fsync(tmp.fileno())
            ino = os.fstat(tmp.fileno()).st_ino
            os.replace(tmp.name, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return ino
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def storage(project: Path) -> Path:
    root = Path(os.environ.get('RH_STATE_HISTORY', Path.home() / '.local/state/researchhub/project-state'))
    folder = root / (project.name + '-' + digest(os.fsencode(project.absolute()))[:16])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def checkpoint(folder: Path, data: bytes) -> Path:
    path = folder / (digest(data) + '.md')
    if not path.exists():
        durable(path, data, mode=0o600)
    if path.read_bytes() != data:
        raise IOError('状态快照校验失败')
    return path


def _sync_Shared_with_inode_rotation(project: Path, path: Path, data: bytes) -> int:
    """解决 macOS 虚拟机 VirtioFS 双层缓存问题：
    1) 若原路径如果只原地覆写同一 Inode，客机 XNU 内核的 UBC (Unified Buffer Cache) 会沿用旧 vnode 的内存页，
       仅调用 ubc_setsize 截断到新长度（导致读出 '旧内容[:新长度]' 的截断旧缓存，如 532d6240...）；
    2) 若直接 os.replace 删掉旧 Inode，客机尚未过期的目录项缓存 (dentry) 指向已 unlink 的旧 Inode 时会报 ENOENT；
    3) 根治方案：
       a) 先在 _control/.state_cache/<sha256>.md 落盘零缓存内容寻址快照（新文件名在客机 100% 无历史缓存）；
       b) 将旧 Inode 硬链接保活到 _control/.state_cache/.grace_<ino> 并同时原地写入新内容（防止过渡期旧 dentry 报 ENOENT 或读到旧字节）；
       c) 用全新临时文件 (新 Inode) 原子替换 (os.replace) _control/01_PROJECT_STATE.md，强制客机 XNU 为新 Inode 分配全新 vnode（0 历史 UBC 页缓存）！
    """
    sha = digest(data)
    cache_root = project / CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)

    # a) 写入以哈希命名的零缓存快照（保留最近 12 个版本）
    snap_in_project = cache_root / f'{sha}.md'
    if not snap_in_project.exists() or snap_in_project.read_bytes() != data:
        durable(snap_in_project, data, mode=0o644)

    # b) 保活并同步旧 Inode，防止客机在 dentry 刷新窗口期访问旧 Inode 时遇到 ENOENT 或旧页
    if path.exists() and not path.is_symlink():
        try:
            old_stat = path.stat()
            grace_link = cache_root / f'.grace_{old_stat.st_ino}'
            if not grace_link.exists():
                try:
                    os.link(path, grace_link)
                except OSError:
                    pass
            with os.fdopen(os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK), 'r+b') as stream:
                stream.seek(0)
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass

    # c) 原子切换到全新 Inode，强制客机 VirtioFS 丢弃旧 vnode 的 UBC 页缓存
    new_ino = durable(path, data, mode=0o644)

    # d) 写入最新状态指针文件（供客机 state_client / MCP 快速自检）
    meta_bytes = json.dumps(
        {'sha256': sha, 'bytes': len(data), 'inode': new_ino, 'updated_at': int(time.time())},
        ensure_ascii=False,
    ).encode('utf-8')
    durable(cache_root / 'latest.json', meta_bytes, mode=0o644)

    # e) 清理超过 8 个的历史保活硬链接与旧快照
    try:
        graces = sorted(cache_root.glob('.grace_*'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_g in graces[6:]:
            old_g.unlink(missing_ok=True)
        snaps = sorted(cache_root.glob('*.md'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_s in snaps[12:]:
            old_s.unlink(missing_ok=True)
    except OSError:
        pass

    if path.read_bytes() != data:
        raise IOError('原路径读回校验失败')
    return new_ino


def publish(project, base, data, recover=False):
    project = Path(project).absolute()
    path = project / STATE
    if path.resolve() != path or not data.strip() or len(data) > MAX_BYTES:
        raise ValueError('状态路径或内容无效')
    data.decode('utf-8')
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
            if base is not None and observed != base and not recover:
                raise ValueError('版本冲突，请重读原件。当前 SHA-256 ' + observed)
            checkpoint(folder, old)
            checkpoint(folder, data)
            durable(journal, json.dumps({'old': observed, 'new': digest(data)}).encode(), mode=0o600)

        try:
            new_ino = _sync_Shared_with_inode_rotation(project, path, data)
        except Exception:
            _sync_Shared_with_inode_rotation(project, path, old)
            raise
        journal.unlink(missing_ok=True)
        return {'sha256': digest(data), 'bytes': len(data), 'inode': new_ino, 'previous_sha256': observed}


def main():
    parser = argparse.ArgumentParser(description='从宿主私有断点恢复项目状态或刷新 VirtioFS 缓存')
    parser.add_argument('action', choices=['restore', 'refresh'])
    parser.add_argument('project', type=Path)
    parser.add_argument('snapshot_sha256', nargs='?')
    parser.add_argument('current_sha256', nargs='?')
    args = parser.parse_args()
    if args.action == 'refresh':
        proj = args.project.absolute()
        path = proj / STATE
        data = path.read_bytes()
        print(json.dumps(publish(proj, digest(data), data, recover=True)))
        return
    import re
    if not args.snapshot_sha256 or not re.fullmatch('[a-f0-9]{64}', args.snapshot_sha256):
        parser.error('快照 SHA-256 无效')
    data = (storage(args.project.absolute()) / (args.snapshot_sha256 + '.md')).read_bytes()
    if digest(data) != args.snapshot_sha256:
        raise ValueError('快照校验失败')
    print(json.dumps(publish(args.project, args.current_sha256, data, recover=True)))


if __name__ == '__main__':
    main()
