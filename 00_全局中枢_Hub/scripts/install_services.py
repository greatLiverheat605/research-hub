#!/usr/bin/env python3
"""Deploy reviewed service code on the host and install login agents for the GUI user."""
import hashlib
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

source = Path(__file__).resolve().parents[1]
runtime = Path.home()/'Library/Application Support/ResearchHub/runtime'
logs = Path.home()/'Library/Logs/ResearchHub'
plists = Path.home()/'Library/LaunchAgents'
target = f'gui/{os.getuid()}'
files = ['consult.py','models.toml','policy.toml','OPERATION_MANUAL.md','WORKSPACE_INDEX.md','skills/SKILL_INDEX.md','dashboard/serve.py','dashboard/index.html']
files += ['scripts/'+name+'.py' for name in ('dispatch_worker','check_manuscript','exp_store','issue_inbox','run_stream','rh_checkpoint','colab_checkpoint','colab_jobs','colab_bridge','job_status','protected_paths','service_reload','lit_fetch','scout_bus','context_distiller','privacy_guard')]

def install():
    logs.mkdir(parents=True,exist_ok=True)
    plists.mkdir(exist_ok=True)
    for rel in files:
        dest = runtime/rel
        dest.parent.mkdir(parents=True,exist_ok=True)
        data = (source/rel).read_bytes()
        if dest.is_file() and dest.read_bytes()==data: continue
        if rel.endswith('.py'): compile(data,str(dest),'exec')
        with tempfile.NamedTemporaryFile(dir=dest.parent,delete=False) as tmp:
            tmp.write(data)
        os.replace(tmp.name,dest)
    app = Path.home()/'Applications/ResearchHub.app'
    exe = app/'Contents/MacOS/ResearchHub'
    stamp = runtime/'service_host.sha256'
    host_source = source/'scripts/service_host.c'
    fingerprint = hashlib.sha256(host_source.read_bytes()).hexdigest()
    if not exe.exists() or not stamp.exists() or stamp.read_text() != fingerprint:
        exe.parent.mkdir(parents=True,exist_ok=True)
        info = dict(CFBundleIdentifier='com.researchhub.servicehost',CFBundleName='ResearchHub',CFBundleExecutable='ResearchHub',CFBundlePackageType='APPL',CFBundleVersion='1',LSUIElement=True,NSDocumentsFolderUsageDescription='读取文稿中的 ResearchHub 课题，并写入已授权任务的结果。')
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        subprocess.run(['/usr/bin/clang','-O2',str(host_source),'-o',str(exe)],check=True)
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(app)],check=True)
        stamp.write_text(fingerprint)
    for short, entry, args in [('dispatchd','scripts/dispatch_worker.py',['--watch']),('dashboard','dashboard/serve.py',['--port','8765'])]:
        label = 'com.researchhub.'+short
        file = plists/(label+'.plist')
        settings = dict(Label=label,AssociatedBundleIdentifiers=['com.researchhub.servicehost'],ProgramArguments=[str(exe),'/usr/bin/python3','-u',str(runtime/entry),*args],RunAtLoad=True,KeepAlive=True,ThrottleInterval=10, WorkingDirectory=str(runtime),StandardOutPath=str(logs/(short+'.log')),StandardErrorPath=str(logs/(short+'.err')),EnvironmentVariables={'RH_AGENT_READY':str(runtime/(short+'.ready')), 'RH_EXCHANGE_ROOT':str(source.parent),'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'})
        data = plistlib.dumps(settings)
        active = subprocess.run(['launchctl','print',target+'/'+label],capture_output=True).returncode == 0
        if not file.exists() or file.read_bytes()!=data:
            if active:
                subprocess.run(['launchctl','bootout',target+'/'+label],check=True)
                active = False
            file.write_bytes(data)
        if not active: subprocess.run(['launchctl','bootstrap',target,str(file)],check=True)
        print(label, '已部署')

if __name__=='__main__': install()
