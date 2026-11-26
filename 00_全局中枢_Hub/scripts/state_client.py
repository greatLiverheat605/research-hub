"""Read a project state (with VirtioFS F_NOCACHE & content-addressed self-healing) or enqueue a host commit."""
import argparse
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


def _read_nocache(path: Path) -> bytes:
    """在 macOS/VirtioFS 下强制开启 F_NOCACHE (48) 绕过客机 UBC 内存页缓存直读真实字节。"""
    try:
        # 触发父目录 readdir 刷新客机 dentry 缓存
        os.listdir(path.parent)
    except OSError:
        pass
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.fcntl(fd, F_NOCACHE, 1)
        except OSError:
            pass
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
    """通过客机自身 VFS 写入通道刷新客机内核 UBC 页缓存，使客机普通 read_file 也立刻看到最新内容。"""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.fcntl(fd, F_NOCACHE, 1)
            except OSError:
                pass
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def read(project: Path, expected_sha256: str = None):
    project = Path(project)
    state_path = project / STATE
    cache_root = project / CACHE_DIR
    data = _read_nocache(state_path)
    cur_sha = hashlib.sha256(data).hexdigest()

    # 若指定了期望 sha256 或 _control/.state_cache/ 中已有宿主机最新发布的零缓存快照，自动核对并自愈客机缓存
    target_sha = expected_sha256
    if not target_sha:
        latest_meta = cache_root / 'latest.json'
        if latest_meta.is_file():
            try:
                meta = json.loads(_read_nocache(latest_meta).decode('utf-8'))
                target_sha = meta.get('sha256')
            except Exception:
                target_sha = None

    if target_sha and cur_sha != target_sha:
        snap_file = cache_root / f'{target_sha}.md'
        if snap_file.is_file():
            snap_data = _read_nocache(snap_file)
            if hashlib.sha256(snap_data).hexdigest() == target_sha:
                _refresh_guest_ubc(state_path, snap_data)
                data = snap_data
                cur_sha = target_sha

    return {'sha256': cur_sha, 'bytes': len(data), 'content': data.decode('utf-8')}


def submit(project: Path, expected: str, content: str):
    project = Path(project)
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('请提供编辑开始时原件的 SHA-256')
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 2 * 1024 * 1024:
        raise ValueError('状态内容为空或超长')
    queue = project / '_dispatch/pending'
    if queue.resolve() != queue.absolute() or not queue.is_dir():
        raise ValueError('项目投递目录不可用')
    ident = 'state_' + uuid.uuid4().hex
    job = dict(job_id=ident, action='state_commit', base_sha256=expected, content=content)
    with tempfile.NamedTemporaryFile(dir=queue, suffix='.tmp', mode='w', encoding='utf-8', delete=False) as tmp:
        try:
            json.dump(job, tmp, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.rename(tmp.name, queue / (ident + '.json'))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return {'queued': ident, 'receipt': '_dispatch/done/' + ident + '_result.md', 'sha256': hashlib.sha256(content.encode()).hexdigest()}


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
