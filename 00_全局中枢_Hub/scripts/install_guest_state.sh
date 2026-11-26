#!/bin/sh
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
share='/Volumes/My Shared Files/ClaudeShield-Exchange'
[ -d "$share/01_在研课题_Projects" ] || { echo '请在已挂载交换卷的 macOS 客机执行'; exit 1; }
python3 - "$src" <<'PY'
import hashlib, json, os, shutil, sys, tempfile
from pathlib import Path
src=Path(sys.argv[1]); dest=Path.home()/'Library/Application Support/Claude'
config=json.loads((dest/'claude_desktop_config.json').read_text())
if not any(Path(arg).expanduser()==dest/'mcp_filesystem_server.py' for arg in config.get('mcpServers',{}).get('filesystem',{}).get('args',[])):
    sys.exit('客机使用其他 MCP 入口，请核对配置。')
files=['state_client.py','mcp_filesystem_server.py']; backup=Path(tempfile.mkdtemp(dir=dest,prefix='pre-state-')); hashes={}
for name in files:
    body=(src/name).read_bytes(); compile(body,name,'exec'); hashes[name]=hashlib.sha256(body).hexdigest()
    if (dest/name).exists(): shutil.copy2(dest/name,backup/name)
for name in files:
    with tempfile.NamedTemporaryFile(dir=dest,delete=False) as f:
        body=(src/name).read_bytes(); assert hashlib.sha256(body).hexdigest()==hashes[name]; f.write(body); f.flush(); os.fsync(f.fileno())
    os.replace(f.name,dest/name)
    assert hashlib.sha256((dest/name).read_bytes()).hexdigest()==hashes[name]
print('客机部署断点',backup,'。重新打开 Claude Desktop 后可使用 read_project_state / submit_project_state。')
PY
