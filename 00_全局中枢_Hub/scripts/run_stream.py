"""The child writes directly to the job log. Keep only a bounded receipt tail in memory."""
import json
import os
import signal
import site
import subprocess
import sys
import tempfile
from pathlib import Path


def stream(argv, path, timeout, **options):
    with path.open('ab', buffering=0) as output:
        proc = subprocess.Popen(argv,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,**options)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait()
            output.write(b'\n[dispatch] TIMEOUT\n')
    with path.open('rb') as f:
        f.seek(max(0,os.fstat(f.fileno()).st_size-6400))
        tail = f.read().decode('utf-8','replace')
    return subprocess.CompletedProcess(argv,proc.returncode,tail,'')


def local(project, script, args, path, timeout, *, read_dirs=None, write_dirs=None, cwd=None):
    # Sandbox applied by the OS. HOME redirection alone cannot protect host credentials.
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        def sub(p): return '(subpath '+json.dumps(str(p),ensure_ascii=False)+')'
        code,exp = project/'03_代码与脚本',project/'02_实验与验证'
        reads = ['/System','/usr','/bin','/Library/Developer','/Library/Frameworks','/opt/homebrew','/private/etc','/dev',site.getusersitepackages(),*(read_dirs or [code,exp]),*(write_dirs or [exp]),work]
        policy = '(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-read-metadata)(allow file-read-data (literal "/"))(allow file-read* '+ ' '.join(map(sub,reads))+')(allow file-write* '+sub(work)+' '+' '.join(map(sub,write_dirs or [exp]))+' (literal "/dev/null"))(deny file-read* file-write* (regex #"/(benchmark|冻结件|冻结存档-[^/]*)(/|$)"))'
        env = dict(HOME=str(work),TMPDIR=str(work),PATH='/usr/bin:/bin:/opt/homebrew/bin',LANG='en_US.UTF-8',PYTHONPATH=site.getusersitepackages(),PYTHONDONTWRITEBYTECODE='1')
        return stream(['/usr/bin/sandbox-exec','-p',policy,sys.executable,'-u',str(script),*args],path,timeout,cwd=cwd or project,env=env)
