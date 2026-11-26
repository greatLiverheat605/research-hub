#!/usr/bin/env python3
"""Build the guest text clipboard app and read-only media, without starting a VM."""
import fcntl
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile


def main():
    os.umask(0o077)
    repo = Path(__file__).resolve().parents[1]
    root = repo / '.private/residential-vm'
    image = root / 'clipboard-tools.raw'
    with (root / 'clipboard-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if image.exists():
            print('Existing clipboard tools media preserved.')
            return
        with tempfile.TemporaryDirectory(prefix='clipboard-build-', dir=root) as temp:
            folder = Path(temp)
            content = folder / 'content'
            app = content / 'ClaudeShield Clipboard.app'
            binary_dir = app / 'Contents/MacOS'
            binary_dir.mkdir(parents=True)
            for directory in [content, app, app / 'Contents', binary_dir]:
                directory.chmod(0o755)
            info = {
                'CFBundleExecutable': 'ClaudeShieldClipboard',
                'CFBundleIdentifier': 'local.claudeshield.clipboard',
                'CFBundleName': 'ClaudeShield Clipboard',
                'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': '0.1', 'CFBundleVersion': '1',
                'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
            }
            with (app / 'Contents/Info.plist').open('wb') as output:
                plistlib.dump(info, output)
            (app / 'Contents/Info.plist').chmod(0o644)
            binary = binary_dir / 'ClaudeShieldClipboard'
            subprocess.run([
                'swiftc', '-swift-version', '5', '-parse-as-library', '-O',
                '-import-objc-header', str(repo / 'vm/clipboard-vsock.h'),
                str(repo / 'vm/ClipboardWire.swift'), str(repo / 'vm/ClipboardGuest.swift'),
                '-o', str(binary)
            ], check=True)
            binary.chmod(0o755)
            subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
            readme = content / '使用说明.txt'
            readme.write_text(
                '在专用 macOS 虚拟机中双击 ClaudeShield Clipboard.app，保持它运行。\n'
                '可以将应用拖到虚拟机的 Applications 文件夹，今后直接打开。\n\n'
                '宿主 → 虚拟机：宿主复制文字，点击外层窗口“发送文字 → 虚拟机”，'
                '在虚拟机输入框按 Command-V。\n'
                '虚拟机 → 宿主：虚拟机复制文字，点击外层窗口“取回文字 → 宿主”，'
                '在宿主应用按 Command-V。\n\n'
                '仅支持文字，每次最多 256 KiB；图片和文件不在本工具支持范围内。\n'
                '每次传输由按钮触发。工具不自动执行收到的文字，不使用网络或云服务。\n',
                encoding='utf-8'
            )
            readme.chmod(0o644)
            pending = folder / 'clipboard-tools.cdr'
            subprocess.run([
                'hdiutil', 'create', '-srcfolder', str(content), '-fs', 'HFS+',
                '-format', 'UDTO', '-volname', 'CSClipboard', '-o', str(pending)
            ], check=True)
            if pending.stat().st_size == 0 or pending.stat().st_size % 512:
                raise RuntimeError('Clipboard media is not block aligned')
            pending.chmod(0o600)
            os.replace(pending, image)
        print('Clipboard guest app and read-only media prepared; no VM restarted.')


if __name__ == '__main__':
    main()
