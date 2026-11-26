#!/usr/bin/env python3
"""Create a private, read-only HFS+ installer image without accessing VM disks.

Existing media is never replaced: a running VM could have it attached. To rebuild,
first shut down the environment and move the old image aside.
"""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    repo = Path(__file__).resolve().parents[1]
    root = repo / '.private/residential-vm'
    share = root / 'share'
    image = root / 'guest-installer.raw'
    os.umask(0o077)
    with (root / 'installer-media.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if image.exists():
            print('Existing installer media preserved.')
            return
        for name in ['install-in-guest.sh', 'verge-mihomo',
                     'mihomo-residential.yaml', 'Claude.app', 'Google Chrome.app']:
            if not (share / name).exists():
                raise RuntimeError(f'Missing installer input: {name}')
        shutil.copy2(repo / 'vm/Install.command', share / 'Install.command')
        (share / 'Install.command').chmod(0o755)
        shutil.copy2(repo / 'vm/install-in-guest.sh', share / 'i')
        (share / 'mihomo-residential.yaml').chmod(0o600)
        with tempfile.TemporaryDirectory(prefix='installer-build-', dir=root) as temp:
            pending = Path(temp) / 'guest-installer.cdr'
            subprocess.run([
                'hdiutil', 'create', '-srcfolder', str(share), '-fs', 'HFS+',
                '-format', 'UDTO', '-volname', 'CS',
                '-o', str(pending)
            ], check=True)
            if pending.stat().st_size == 0 or pending.stat().st_size % 512:
                raise RuntimeError('Installer image is not block aligned')
            pending.chmod(0o600)
            os.replace(pending, image)
        print('Read-only installer media ready; VM disks were not opened.')


if __name__ == '__main__':
    main()
