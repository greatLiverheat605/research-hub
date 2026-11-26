"""Host-side checkpoint transfer. Publish only complete versions on the Mac."""
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from rh_checkpoint import CHUNK


def hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for data in iter(lambda: src.read(CHUNK), b''):
            digest.update(data)
    return digest.hexdigest()


def history(folder):
    return sorted(p for p in folder.iterdir() if re.fullmatch(r'\d{16}', p.name) and p.is_dir())


def store(root, project, key, script, args, inputs):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', key):
        raise ValueError('checkpoint_key 仅允许字母、数字、短横线或下划线')
    folder = root/'checkpoints'/project.name/key
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    signature = [hash_file(script), args, [[name, hash_file(p)] for p, name in inputs]]
    path = folder/'signature.json'
    if path.exists():
        if json.loads(path.read_text()) != signature:
            raise ValueError('检查点对应的脚本、参数或输入已改变，请核对后使用新的 checkpoint_key')
    else:
        from issue_inbox import replace
        replace(path, json.dumps(signature))
    return folder


def receive(folder, step, size, digest, download):
    final = folder/f'{step:016d}'
    previous = history(folder)
    if previous and final.name <= previous[-1].name:
        raise ValueError('检查点步数没有递增')
    with tempfile.TemporaryDirectory(dir=folder, prefix='.part-') as work:
        snapshot = Path(work)/'snapshot'
        snapshot.mkdir()
        dest = snapshot/'state.bin'
        part = Path(work)/'chunk'
        checksum = hashlib.sha256()
        with dest.open('wb') as out:
            for offset in range(0, size, CHUNK):
                download(offset // CHUNK, part)
                data = part.read_bytes()
                if len(data) != min(size-offset, CHUNK):
                    raise ValueError('检查点分片长度不匹配')
                out.write(data)
                checksum.update(data)
            out.flush()
            os.fsync(out.fileno())
        if checksum.hexdigest() != digest:
            raise ValueError('检查点 SHA-256 不匹配')
        (snapshot/'sha256').write_text(digest)
        snapshot.rename(final)
    for old in previous[:-1]:
        shutil.rmtree(old)
    return final


def execute(folder, call, session, temporary, script, args, setup, marker, log, stage):
    prefix = '/content/rh_ckpt_' + session
    state = prefix + '.state'
    helper = Path(__file__).with_name('rh_checkpoint.py')
    call(['upload', '-s', session, str(helper), '/content/rh_checkpoint.py'])
    before = history(folder)
    step = -1
    restore = ''
    if before:
        snap = before[-1]
        step = int(snap.name)
        expected = (snap/'sha256').read_text()
        if hash_file(snap/'state.bin') != expected:
            raise ValueError('Mac 检查点文件校验失败')
        count = 0
        with (snap/'state.bin').open('rb') as src:
            for data in iter(lambda: src.read(CHUNK), b''):
                part = temporary/'restore.bin'
                part.write_bytes(data)
                call(['upload','-s',session,str(part),f'{prefix}.{count}'])
                count += 1
        restore = f'''import hashlib
h = hashlib.sha256()
with open({state!r}, 'wb') as target:
    for i in range({count}):
        part = Path({prefix!r} + '.' + str(i))
        data = part.read_bytes()
        target.write(data)
        h.update(data)
        part.unlink()
if h.hexdigest() != {expected!r}: raise ValueError('restore SHA-256 mismatch')
print('已恢复 Mac 检查点 step={step}', flush=True)
'''
    encoded = base64.b64encode(script.read_bytes()).decode()
    wrapper = temporary/'entry.py'
    while True:
        wrapper.write_text(setup + 'import base64,sys,os\nfrom pathlib import Path\n' + restore + f'''
sys.path.insert(0, '/content')
import rh_checkpoint as ck
os.environ.update(RH_CKPT_PREFIX={prefix!r}, RH_RESUME_PATH={state if step >= 0 else ''!r}, RH_RESUME_STEP={str(step)!r})
sys.argv = {[script.name, *args]!r}
_rh_script_bytes = base64.b64decode({encoded!r})
Path({script.name!r}).write_bytes(_rh_script_bytes)
try:
    exec(compile(_rh_script_bytes, {script.name!r}, 'exec'), dict(__name__='__main__', __file__={script.name!r}))
except ck.Boundary as boundary:
    print({marker+' SAVE'!r}, *boundary.args, flush=True)
else:
    print({marker!r}, flush=True)
''')
        start = log.stat().st_size
        with log.open('ab', buffering=0) as out:
            call(['exec','-s',session,'--timeout','0','-f',str(wrapper)], out)
        with log.open('rb') as src:
            src.seek(max(start, log.stat().st_size-65536))
            lines = src.read().decode(errors='replace').splitlines()
        if marker in (v.strip() for v in lines):
            return
        match = next((m for v in reversed(lines) if (m := re.fullmatch(re.escape(marker) + r' SAVE (\d{1,16}) (\d{1,20}) ([0-9a-f]{64})', v.strip()))), None)
        if not match:
            raise ValueError('实验中断，请从日志中最后一次 Mac 已保存的步数续跑')
        step, size = map(int, match.groups()[:2])
        digest = match[3]
        receive(folder, step, size, digest, lambda i,p: call(['download','-s',session,f'{prefix}.{i}',str(p)]))
        stage(f'检查点已保存到 Mac · {folder.name} · step={step} · {size} 字节 · SHA-256 已校验')
        setup = ''
        # Reassemble the identical state in this VM without uploading it again.
        count = (size+CHUNK-1)//CHUNK
        restore = f'''with open({state!r}, 'wb') as dest:
    for i in range({count}):
        part = Path({prefix!r} + '.' + str(i))
        dest.write(part.read_bytes())
        part.unlink()
'''
