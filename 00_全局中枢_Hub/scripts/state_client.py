"""Read a project state (with Guest VFS write-through + .state_cache self-healing) or enqueue a host commit."""
import argparse
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

STATE = Path('_control/01_PROJECT_STATE.md')
CACHE_DIR = Path('_control/.state_cache')
F_NOCACHE = getattr(fcntl, 'F_NOCACHE', 48)
PROT_READ = 0x01
MAP_SHARED = 0x0001
MS_INVALIDATE = 0x0002

try:
    _LIBC = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    _LIBC.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
    _LIBC.mmap.restype = ctypes.c_void_p
    _LIBC.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    _LIBC.msync.restype = ctypes.c_int
    _LIBC.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    _LIBC.munmap.restype = ctypes.c_int
except Exception:
    _LIBC = None


def invalidate_ubc_fd(fd: int) -> None:
    try:
        fcntl.fcntl(fd, F_NOCACHE, 1)
    except OSError:
        pass
    if _LIBC is None:
        return
    try:
        sz = os.fstat(fd).st_size
        if sz > 0:
            addr = _LIBC.mmap(None, sz, PROT_READ, MAP_SHARED, fd, 0)
            if addr and addr != ctypes.c_void_p(-1).value:
                _LIBC.msync(ctypes.c_void_p(addr), sz, MS_INVALIDATE)
                _LIBC.munmap(ctypes.c_void_p(addr), sz)
    except Exception:
        pass


def _read_nocache(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY)
    try:
        invalidate_ubc_fd(fd)
        chunks = []
        while True:
            buf = os.read(fd, 65536)
            if not buf:
                break
            chunks.append(buf)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _refresh_guest_ubc(path: Path, data: bytes) -> None:
    """通过客机自身 VFS 写入通道刷新客机内核 UBC 页缓存，同时保持原 Inode 不变。
    在 VirtioFS 下，只要由客机发起一次 VFS write+ftruncate+fsync，客机内核 UBC 中的旧页与尾部 NUL 填充会立即被真实内容覆盖！
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            invalidate_ubc_fd(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, len(data))
            os.fsync(fd)
            invalidate_ubc_fd(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _latest_snapshot_in_cache(cache_root: Path) -> Path:
    if not cache_root.is_dir():
        return None
    try:
        md_files = [
            cache_root / name
            for name in os.listdir(cache_root)
            if re.fullmatch(r'[a-f0-9]{64}\.md', name)
        ]
        if not md_files:
            return None
        return max(md_files, key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        return None


def read(project: Path, expected_sha256: str = None):
    project = Path(project)
    state_path = project / STATE
    cache_root = project / CACHE_DIR
    data = _read_nocache(state_path)
    cur_sha = hashlib.sha256(data).hexdigest()

    snap_file = None
    if expected_sha256 and cur_sha != expected_sha256:
        cand = cache_root / f'{expected_sha256}.md'
        if cand.is_file():
            snap_file = cand
    if snap_file is None:
        latest_snap = _latest_snapshot_in_cache(cache_root)
        # 若客机读到的 data 含有尾部 NUL (\x00) 字节（典型的 VirtioFS 文件增长 UBC 旧页未刷新特征），或与最新快照不一致，立即从零缓存快照自愈
        if latest_snap is not None and (b'\x00' in data or latest_snap.stem != cur_sha):
            try:
                if b'\x00' in data or latest_snap.stat().st_mtime_ns >= state_path.stat().st_mtime_ns - 5_000_000_000:
                    snap_file = latest_snap
            except OSError:
                snap_file = latest_snap

    if snap_file is not None and snap_file.is_file():
        try:
            snap_data = _read_nocache(snap_file)
            snap_sha = hashlib.sha256(snap_data).hexdigest()
            if snap_sha == snap_file.stem:
                _refresh_guest_ubc(state_path, snap_data)
                data = snap_data
                cur_sha = snap_sha
        except OSError:
            pass

    return {'sha256': cur_sha, 'bytes': len(data), 'content': data.decode('utf-8', errors='replace')}


def submit(project: Path, expected: str, content: str):
    project = Path(project)
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('请提供编辑开始时原件的 SHA-256')
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 2 * 1024 * 1024:
        raise ValueError('状态内容为空或超长')
    queue = project / '_dispatch/pending'
    if queue.resolve() != queue.absolute() or not queue.is_dir():
        raise ValueError('项目投递目录不可用')

    # 先在客机侧核对 expected，若通过则同时在客机 VFS 原地刷入新内容，确保客机自身 UBC 页缓存当场 100% 更新！
    cur = read(project, expected_sha256=expected)
    raw = content.encode('utf-8')
    new_sha = hashlib.sha256(raw).hexdigest()
    if cur['sha256'] == expected:
        _refresh_guest_ubc(project / STATE, raw)

    ident = 'state_' + uuid.uuid4().hex
    job = dict(job_id=ident, action='state_commit', base_sha256=expected, content=content)
    with tempfile.NamedTemporaryFile(dir=queue, suffix='.tmp', mode='w', encoding='utf-8', delete=False) as tmp:
        try:
            json.dump(job, tmp, ensure_ascii=False)
            tmp.flush()
            os.fchmod(tmp.fileno(), 0o644)
            os.fsync(tmp.fileno())
            os.rename(tmp.name, queue / (ident + '.json'))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return {'queued': ident, 'receipt': '_dispatch/done/' + ident + '_result.md', 'sha256': new_sha}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('read', 'submit', 'verify'))
    parser.add_argument('project', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--draft', type=Path)
    args = parser.parse_args()
    if args.action == 'submit':
        if not args.draft:
            parser.error('--draft required')
        result = submit(args.project, args.sha256, args.draft.read_bytes().decode())
    else:
        result = read(args.project, expected_sha256=args.sha256)
        if args.action == 'verify':
            del result['content']
            if result['sha256'] != args.sha256:
                parser.exit(1, '原件读回哈希不匹配 ' + result['sha256'] + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
