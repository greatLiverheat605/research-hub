#!/usr/bin/env python3
"""Explicit, transactional gateway deployment. Never invoked by candidate builds.

The apply CLI is restricted to the dedicated Linux gateway (root + fixed MACs).
On any deployment failure original files are restored and forwarding stays OFF.
Actual activation requires the reviewed maintenance plan and independent console.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import tempfile
import time

FILES = {
    'residential.nft': '/etc/claudeshield/residential.nft',
    'residential-gateway.service': '/etc/systemd/system/residential-gateway.service',
    '90-residential.conf': '/etc/sysctl.d/90-residential.conf',
    'dnsmasq-lan.conf': '/etc/dnsmasq.d/claudeshield-lan.conf',
    'gateway-offline-test.py': '/etc/claudeshield/gateway-offline-test.py',
    'gateway-integrity.py': '/etc/claudeshield/gateway-integrity.py',
}
UNIT = 'residential-gateway.service'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest key')
        result[key] = value
    return result


def regular_bytes(path, maximum=2 * 1024 * 1024):
    path = Path(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
            before.st_size > maximum or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError('Unsafe bundle file')
    data = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Bundle file changed during read')
    return data


def validate_bundle(directory):
    directory = Path(directory).absolute()
    raw = regular_bytes(directory / 'manifest.json', 65536)
    manifest = json.loads(raw, object_pairs_hook=unique_object)
    if (type(manifest) is not dict or manifest.get('schema_version') != 4 or
            type(manifest.get('gateway_files')) is not dict or set(manifest['gateway_files']) != set(FILES)):
        raise ValueError('Gateway manifest shape mismatch')
    mapping = manifest['gateway_files']
    if any(type(v) is not str or len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in mapping.values()):
        raise ValueError('Invalid file digest')
    digest = sha(json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode())
    if manifest.get('gateway_bundle_sha256') != digest:
        raise ValueError('Gateway bundle digest mismatch')
    allowed = set(FILES) | {'manifest.json'}
    if set(p.name for p in directory.iterdir()) != allowed:
        raise ValueError('Unexpected gateway bundle member')
    payload = {name: regular_bytes(directory / name) for name in FILES}
    if any(sha(payload[name]) != mapping[name] for name in FILES):
        raise ValueError('Gateway file hash mismatch')
    payload['manifest.json'] = raw
    return manifest, payload


def guard_gateway():
    if os.uname().sysname != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Apply only inside the dedicated Linux gateway as root')
    expected = {'wan0': '52:54:00:77:00:01', 'lan0': '52:54:00:77:00:02'}
    if {p.name for p in Path('/sys/class/net').iterdir()} != {'lo', *expected}:
        raise RuntimeError('Unexpected dedicated gateway interfaces')
    for interface, mac in expected.items():
        path = Path('/sys/class/net') / interface / 'address'
        if not path.is_file() or path.read_text().strip().lower() != mac:
            raise RuntimeError('Dedicated gateway identity mismatch')


class Runtime:
    @staticmethod
    def command(args, check=True):
        p = subprocess.run(args, capture_output=True, text=True, timeout=250)
        if check and p.returncode:
            raise RuntimeError('Gateway system operation failed')
        return p

    def disable(self):
        self.command(['/usr/sbin/sysctl', '-w', 'net.ipv4.ip_forward=0'])
        self.command(['/usr/sbin/sysctl', '-w', 'net.ipv6.conf.all.forwarding=0'])
        if (Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != '0' or
                Path('/proc/sys/net/ipv6/conf/all/forwarding').read_text().strip() != '0'):
            raise RuntimeError('Forwarding could not be closed')

    def stop(self):
        self.command(['/usr/bin/systemctl', 'stop', UNIT])

    def reload(self):
        self.command(['/usr/bin/systemctl', 'daemon-reload'])

def atomic_write(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked deployment destination')
    fd, name = tempfile.mkstemp(prefix='.cs-pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        os.replace(name, path)
        fd_dir = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd_dir)
        finally:
            os.close(fd_dir)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def remove_marker(marker):
    if marker is None:
        return
    Path(marker).unlink()
    fd = os.open(Path(marker).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def deployment_lock(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe deployment lock path')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise ValueError('Unsafe deployment lock file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def transact(payload, destinations, backup, runtime, write=atomic_write, marker=None):
    """Internal transaction primitive; tests inject isolated paths and fake ops.

    The CLI never accepts destination maps or a replacement runtime/guard.
    """
    backup = Path(backup)
    if any(p.is_symlink() for p in (backup, *backup.parents)):
        raise ValueError('Unsafe deployment backup path')
    if marker is not None and (Path(marker).exists() or Path(marker).is_symlink()):
        raise RuntimeError('Incomplete prior deployment requires console reconciliation')
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    originals, changed = {}, []
    for name, path in destinations.items():
        p = Path(path)
        if any(x.is_symlink() for x in (p, *p.parents)):
            raise ValueError('Unsafe deployment destination')
        if p.exists():
            data = regular_bytes(p)
            originals[name] = (data, p.stat().st_mode & 0o777)
            (backup / name).write_bytes(data)
        else:
            originals[name] = None
    receipt = {'schema_version': 1, 'state': 'prepared', 'changed': [], 'restored': [],
               'forwarding': 'unchanged', 'original_hashes': {
                   k: sha(v[0]) if v else None for k, v in originals.items()}}
    def record():
        atomic_write(backup / 'receipt.json', (json.dumps(receipt, sort_keys=True, indent=2)+'\n').encode(), 0o600)
    record()
    try:
        if marker is not None:
            atomic_write(marker, b'gateway deployment incomplete; forwarding must remain off\n', 0o600)
        runtime.disable()
        receipt['forwarding'] = 'off'
        runtime.stop()
        for name, path in destinations.items():
            # Refuse to overwrite a file changed after the backup observation.
            before = originals[name]
            if (Path(path).exists() != (before is not None) or
                    (before is not None and regular_bytes(path) != before[0])):
                raise RuntimeError('Concurrent gateway file modification')
            changed.append(name)
            receipt['changed'] = changed[:]
            record()
            write(path, payload[name])
        for name, path in destinations.items():
            if regular_bytes(path) != payload[name]:
                raise RuntimeError('Gateway deployment read-back mismatch')
        runtime.reload()
        runtime.disable()
        # New service requires a new CSRUN challenge. Never activate in the old
        # boot with an absent or old-bundle read-only challenge.
        receipt.update(state='installed_forwarding_off_pending_cold_boot', forwarding='off')
        record()
        remove_marker(marker)
        return receipt
    except BaseException:
        # Hold the forwarding boundary closed BEFORE restoring anything.
        for operation in [runtime.disable, runtime.stop]:
            try:
                operation()
            except Exception:
                pass
        try:
            runtime.disable()  # Read-back after stop, including a failed stop.
            receipt['forwarding'] = 'off'
        except Exception:
            receipt['forwarding'] = 'close_failed_requires_console'
        conflicts = []
        for name in reversed(changed):
            path = Path(destinations[name])
            try:
                old = originals[name]
                if old is None and not path.exists():
                    continue
                current = regular_bytes(path)
                if old is not None and current == old[0]:
                    continue
                if current != payload[name]:
                    raise RuntimeError('Later change preserved')
                if old is None:
                    # Preserve newly installed content in the private backup.
                    os.replace(path, backup / ('withdrawn-'+name))
                else:
                    atomic_write(path, old[0], old[1])
                receipt['restored'].append(name)
            except Exception:
                conflicts.append(name)
        try:
            runtime.reload()
        except Exception:
            conflicts.append('daemon-reload')
        state = ('rollback_conflict' if conflicts else
                 'close_failed_requires_console' if receipt['forwarding'] != 'off' else
                 'rolled_back_forwarding_off')
        receipt.update(state=state,
                       conflicts=conflicts)
        record()
        if not conflicts and receipt['forwarding'] == 'off' and marker is not None:
            remove_marker(marker)
        raise RuntimeError('Gateway deployment failed; inspect private receipt') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify', 'apply'])
    parser.add_argument('--bundle', required=True)
    args = parser.parse_args()
    if args.action == 'apply':
        guard_gateway()  # Before any target command, write or bundle parsing.
    if args.action == 'verify':
        validate_bundle(args.bundle)
        print('Gateway bundle verified offline; no target changes.')
        return
    def interrupted(signum, frame):
        raise RuntimeError('Deployment interrupted')
    for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
        signal.signal(sig, interrupted)
    with deployment_lock('/run/lock/claudeshield-deployment.lock'):
        manifest, payload = validate_bundle(args.bundle)
        runtime = Runtime()
        # This migration preserves the existing enabled service; a disabled,
        # masked or missing install needs its own reviewed setup plan.
        enabled = runtime.command(['/usr/bin/systemctl','is-enabled',UNIT], check=False)
        if enabled.returncode != 0 or enabled.stdout.strip() != 'enabled':
            raise RuntimeError('Expected an existing enabled gateway service')
        for dependency in ['/usr/sbin/nft','/usr/sbin/ip','/usr/bin/python3']:
            if not os.access(dependency, os.X_OK):
                raise RuntimeError('Required gateway dependency is missing; no automatic installation')
        destinations = {**FILES, 'manifest.json': '/etc/claudeshield/manifest.json'}
        stamp = str(time.time_ns())
        backup = Path('/etc/claudeshield/backups') / ('deployment-'+stamp)
        result = transact(payload, destinations, backup, runtime,
                          marker='/etc/claudeshield/deployment-incomplete')
    print(json.dumps({'state': result['state'], 'backup': str(backup),
                      'bundle': manifest['gateway_bundle_sha256']}))


if __name__ == '__main__':
    main()
