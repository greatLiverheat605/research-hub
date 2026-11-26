#!/usr/bin/env python3
"""Fail-closed supervisor for the dedicated Linux gateway, never the host Mac.

Importing only defines functions. Privileged CLI actions require Linux, root,
and the gateway's fixed NICs. Local tests use a fake backend without system calls.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import uuid

MANIFEST = Path('/etc/claudeshield/manifest.json')
DEPLOYMENT_MARKER = Path('/etc/claudeshield/deployment-incomplete')
INSTALLED_FILES = {
    'residential.nft': Path('/etc/claudeshield/residential.nft'),
    'residential-gateway.service': Path('/etc/systemd/system/residential-gateway.service'),
    '90-residential.conf': Path('/etc/sysctl.d/90-residential.conf'),
    'dnsmasq-lan.conf': Path('/etc/dnsmasq.d/claudeshield-lan.conf'),
    'gateway-offline-test.py': Path('/etc/claudeshield/gateway-offline-test.py'),
    'gateway-integrity.py': Path('/etc/claudeshield/gateway-integrity.py'),
}
EXPECTED_MACS = {'wan0': '52:54:00:77:00:01', 'lan0': '52:54:00:77:00:02'}
HASH_RE = re.compile(r'^[0-9a-f]{64}$')
RUN_RE = re.compile(r'^[0-9a-f]{32}$')
CHECK_INTERVAL = 5


class IntegrityError(RuntimeError):
    """A concise code, without command output or configuration contents."""


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IntegrityError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    try:
        def invalid_constant(_value):
            raise IntegrityError('INVALID_JSON_CONSTANT')
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError) as error:
        raise IntegrityError('INVALID_JSON') from error


def regular_bytes(path, limit=2 * 1024 * 1024, root_owned=False):
    """Reject symlinks, nonregular files, races, and oversized inputs."""
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise IntegrityError('NONREGULAR_BUNDLE_FILE')
    if root_owned and (before.st_uid != 0 or before.st_mode & 0o022):
        raise IntegrityError('UNSAFE_BUNDLE_FILE_OWNER_OR_MODE')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise IntegrityError('BUNDLE_FILE_CHANGED_DURING_OPEN')
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise IntegrityError('INVALID_BUNDLE_FILE_SIZE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise IntegrityError('INVALID_BUNDLE_FILE_SIZE')
        return data
    finally:
        os.close(fd)


def bundle_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def verify_bundle(manifest_path=MANIFEST, paths=None, root_owned=False):
    paths = INSTALLED_FILES if paths is None else paths
    manifest = strict_json(regular_bytes(manifest_path, 32768, root_owned).decode('utf-8'))
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 4:
        raise IntegrityError('BUNDLE_SCHEMA_REQUIRED')
    files = manifest.get('gateway_files')
    if not isinstance(files, dict) or set(files) != set(INSTALLED_FILES) or set(paths) != set(INSTALLED_FILES):
        raise IntegrityError('COMPLETE_ALLOWLISTED_BUNDLE_REQUIRED')
    if any(not isinstance(value, str) or not HASH_RE.fullmatch(value) for value in files.values()):
        raise IntegrityError('INVALID_FILE_HASH')
    digest = manifest.get('gateway_bundle_sha256')
    if not isinstance(digest, str) or not HASH_RE.fullmatch(digest) or digest != bundle_digest(files):
        raise IntegrityError('BUNDLE_HASH_MISMATCH')
    for name, expected in files.items():
        if hashlib.sha256(regular_bytes(paths[name], root_owned=root_owned)).hexdigest() != expected:
            raise IntegrityError('INSTALLED_FILE_HASH_MISMATCH')
    return {'gateway_files': dict(files), 'gateway_bundle_sha256': digest}


def validate_run_challenge(document, expected_bundle):
    if not isinstance(document, dict) or set(document) != {'schema_version', 'run_id', 'gateway_bundle_sha256'}:
        raise IntegrityError('COMPLETE_RUN_CHALLENGE_REQUIRED')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise IntegrityError('RUN_SCHEMA_REQUIRED')
    run_id = document['run_id']
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise IntegrityError('FRESH_RUN_ID_REQUIRED')
    if document['gateway_bundle_sha256'] != expected_bundle:
        raise IntegrityError('RUN_BUNDLE_MISMATCH')
    return dict(document)


def canonical_ruleset(document):
    """Keep all nft semantics and order; discard only handles/meta/counter values."""
    if not isinstance(document, dict) or set(document) != {'nftables'} or not isinstance(document['nftables'], list):
        raise IntegrityError('INVALID_NFT_RULESET_JSON')

    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == 'handle':
                    continue
                if key == 'counter' and isinstance(item, dict):
                    result[key] = {field: (0 if field in {'packets', 'bytes'} else normalize(val))
                                   for field, val in item.items() if field != 'handle'}
                else:
                    result[key] = normalize(item)
            return result
        return value

    objects = []
    for item in document['nftables']:
        if not isinstance(item, dict) or len(item) != 1:
            raise IntegrityError('INVALID_NFT_OBJECT')
        if set(item) == {'metainfo'}:
            continue
        objects.append(normalize(item))
    if not objects:
        raise IntegrityError('EMPTY_NFT_RULESET')
    return {'nftables': objects}


def compare_rulesets(expected, actual):
    if canonical_ruleset(expected) != canonical_ruleset(actual):
        raise IntegrityError('ACTIVE_RULESET_MISMATCH')


def valid_wan(value):
    if not isinstance(value, str):
        return False
    try:
        interface = ipaddress.IPv4Interface(value)
    except (ValueError, TypeError):
        return False
    excluded = [ipaddress.IPv4Network(network) for network in
                ['0.0.0.0/8', '127.0.0.0/8', '169.254.0.0/16', '198.18.0.0/15', '224.0.0.0/3']]
    return '/' in value and not any(interface.ip in network for network in excluded)


def validate_runtime(state, forwarding):
    if state.get('forward') != forwarding or type(state.get('forward')) is not int:
        raise IntegrityError('IPV4_FORWARD_STATE_MISMATCH')
    if state.get('ipv6_forward') != 0 or type(state.get('ipv6_forward')) is not int:
        raise IntegrityError('IPV6_FORWARD_MUST_BE_OFF')
    if state.get('lan') != '10.77.0.1/24' or not valid_wan(state.get('wan')):
        raise IntegrityError('GATEWAY_INTERFACE_STATE_INVALID')
    boot_id = state.get('boot_id')
    try:
        if str(uuid.UUID(boot_id)) != boot_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise IntegrityError('BOOT_ID_REQUIRED')


def require_gateway(platform=None, effective_uid=None, nic_root=Path('/sys/class/net')):
    platform = sys.platform if platform is None else platform
    effective_uid = os.geteuid() if effective_uid is None else effective_uid
    if platform != 'linux' or effective_uid != 0:
        raise IntegrityError('DEDICATED_LINUX_ROOT_GATEWAY_REQUIRED')
    nic_root = Path(nic_root)
    if {path.name for path in nic_root.iterdir()} != {'lo', *EXPECTED_MACS}:
        raise IntegrityError('UNEXPECTED_GATEWAY_INTERFACES')
    for name, expected in EXPECTED_MACS.items():
        if (nic_root / name / 'address').read_text().strip().lower() != expected:
            raise IntegrityError('GATEWAY_MAC_BINDING_MISMATCH')


def require_deployment_complete(marker_path=DEPLOYMENT_MARKER):
    """Any marker object, including a broken symlink, blocks activation."""
    try:
        Path(marker_path).lstat()
    except FileNotFoundError:
        return
    raise IntegrityError('GATEWAY_DEPLOYMENT_INCOMPLETE')


class LinuxBackend:
    """System effects live here; construction first checks the dedicated target."""
    def __init__(self, stop_event=None):
        require_gateway()
        self.stop_event = stop_event or threading.Event()

    def command(self, args, timeout=15):
        try:
            return subprocess.run(args, check=True, text=True, capture_output=True,
                                  timeout=timeout).stdout
        except (subprocess.SubprocessError, OSError) as error:
            raise IntegrityError('GATEWAY_COMMAND_FAILED') from error

    def disable_forwarding(self):
        failures = []
        for key in ['net.ipv4.ip_forward', 'net.ipv6.conf.all.forwarding']:
            try:
                self.command(['/usr/sbin/sysctl', '-w', key + '=0'], timeout=3)
            except Exception as error:
                failures.append(error)
        if failures:
            raise IntegrityError('FAILED_TO_DISABLE_FORWARDING')
        if self.forwarding() != (0, 0):
            raise IntegrityError('FORWARDING_DISABLE_NOT_CONFIRMED')

    def forwarding(self):
        try:
            return (int(Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()),
                    int(Path('/proc/sys/net/ipv6/conf/all/forwarding').read_text().strip()))
        except (OSError, ValueError) as error:
            raise IntegrityError('FORWARD_STATE_UNREADABLE') from error

    def enable_forwarding(self):
        self.command(['/usr/sbin/sysctl', '-w', 'net.ipv6.conf.all.forwarding=0'])
        self.command(['/usr/sbin/sysctl', '-w', 'net.ipv4.ip_forward=1'])

    def bundle(self):
        return verify_bundle(root_owned=True)

    def require_deployment_complete(self):
        require_deployment_complete()

    def challenge(self, expected_bundle, mountpoint=None):
        mountpoint = Path('/run/claudeshield-run-iso') if mountpoint is None else Path(mountpoint)
        if mountpoint.exists():
            info = mountpoint.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or os.path.ismount(mountpoint):
                raise IntegrityError('UNSAFE_RUN_ISO_MOUNTPOINT')
        else:
            mountpoint.mkdir(mode=0o700)
        self.command(['/usr/bin/mount', '-o', 'ro,nodev,nosuid,noexec',
                      '/dev/disk/by-label/CSRUN', str(mountpoint)])
        try:
            content = regular_bytes(mountpoint / 'run.json', 4096).decode('utf-8')
            return validate_run_challenge(strict_json(content), expected_bundle)
        finally:
            self.command(['/usr/bin/umount', str(mountpoint)])

    def offline_reference(self):
        result = strict_json(self.command(['/usr/bin/python3', str(INSTALLED_FILES['gateway-offline-test.py']),
                                          '--emit-canonical'], timeout=120))
        if (not isinstance(result, dict) or type(result.get('schema_version')) is not int
                or result['schema_version'] != 1 or result.get('tests') != 'PASS'):
            raise IntegrityError('OFFLINE_TESTS_REQUIRED')
        return canonical_ruleset(result.get('ruleset'))

    def apply_rules(self):
        self.command(['/usr/sbin/nft', '-f', str(INSTALLED_FILES['residential.nft'])])

    def active_rules(self):
        return strict_json(self.command(['/usr/sbin/nft', '-j', 'list', 'ruleset']))

    def runtime(self):
        require_gateway()
        addresses = {}
        for device in ['lan0', 'wan0']:
            document = strict_json(self.command(['/usr/sbin/ip', '-j', '-4', 'addr', 'show', 'dev', device]))
            values = [str(entry['local']) + '/' + str(entry['prefixlen'])
                      for interface in document for entry in interface.get('addr_info', [])
                      if entry.get('family') == 'inet']
            if len(values) != 1:
                raise IntegrityError('SINGLE_IPV4_ADDRESS_PER_NIC_REQUIRED')
            addresses[device] = values[0]
        forward, ipv6_forward = self.forwarding()
        return {'forward': forward, 'ipv6_forward': ipv6_forward,
                'lan': addresses['lan0'], 'wan': addresses['wan0'],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

    def emit(self, document):
        with Path('/dev/hvc0').open('w') as console:
            console.write('CS_GATEWAY_STATUS_JSON:' + json.dumps(document, sort_keys=True,
                                                                 separators=(',', ':')) + '\n')
            console.flush()

    def notify(self, message):
        destination = os.environ.get('NOTIFY_SOCKET')
        watchdog = os.environ.get('WATCHDOG_USEC', '')
        if not destination or not watchdog.isdigit() or int(watchdog) < CHECK_INTERVAL * 2 * 1_000_000:
            raise IntegrityError('SYSTEMD_WATCHDOG_REQUIRED')
        if os.environ.get('WATCHDOG_PID') not in (None, str(os.getpid())):
            raise IntegrityError('SYSTEMD_WATCHDOG_PID_MISMATCH')
        address = '\0' + destination[1:] if destination.startswith('@') else destination
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.settimeout(2)
            message_bytes = message.encode('utf-8')
            if channel.sendto(message_bytes, address) != len(message_bytes):
                raise IntegrityError('SYSTEMD_NOTIFY_FAILED')

    def wait(self):
        return self.stop_event.wait(CHECK_INTERVAL)

    def stopping(self):
        return self.stop_event.is_set()


def supervise(backend):
    """Emit ready only after bundle, nonce, offline and active checks pass."""
    bundle = None
    challenge = None
    state = {}
    sequence = 0
    exit_code = 1
    failure = 'STOPPED'
    try:
        backend.disable_forwarding()
        # The CLI shares this gate; --stop-forwarding remains usable during repair.
        backend.require_deployment_complete()
        bundle = backend.bundle()
        challenge = backend.challenge(bundle['gateway_bundle_sha256'])
        expected = backend.offline_reference()
        # Check again after the longer isolated tests, before opening forwarding.
        if backend.bundle() != bundle:
            raise IntegrityError('BUNDLE_CHANGED_DURING_TESTS')
        backend.apply_rules()
        compare_rulesets(expected, backend.active_rules())
        state = backend.runtime()
        validate_runtime(state, 0)
        if backend.stopping():
            raise IntegrityError('STOP_REQUESTED_BEFORE_FORWARDING')
        backend.enable_forwarding()
        while True:
            if backend.stopping():
                exit_code = 0
                break
            backend.require_deployment_complete()
            if backend.bundle() != bundle:
                raise IntegrityError('BUNDLE_CHANGED_AFTER_START')
            compare_rulesets(expected, backend.active_rules())
            current = backend.runtime()
            validate_runtime(current, 1)
            if state['boot_id'] != current['boot_id']:
                raise IntegrityError('BOOT_CHANGED_DURING_SUPERVISION')
            state = current
            backend.notify(('READY=1\n' if sequence == 0 else '') + 'WATCHDOG=1\nSTATUS=Gateway integrity verified')
            sequence += 1
            backend.emit({'schema_version': 3, 'run_id': challenge['run_id'],
                          'gateway_bundle_sha256': bundle['gateway_bundle_sha256'],
                          'files': bundle['gateway_files'], 'verdict': 'ready',
                          'tests': 'PASS', 'rules': 'PASS', 'sequence': sequence, **state})
            if backend.wait():
                exit_code = 0
                break
    except Exception as error:
        failure = str(error) if isinstance(error, IntegrityError) else type(error).__name__
    finally:
        # Close first, then publish the failure/stop record.
        closed = True
        try:
            backend.disable_forwarding()
        except Exception:
            closed = False
            failure = 'EMERGENCY_FORWARDING_CLOSE_FAILED'
            exit_code = 1
        sequence += 1
        document = {'schema_version': 3, 'run_id': challenge['run_id'] if challenge else '',
                    'gateway_bundle_sha256': bundle['gateway_bundle_sha256'] if bundle else '',
                    'files': bundle['gateway_files'] if bundle else {},
                    'verdict': 'fail', 'forward': 0 if closed else -1,
                    'ipv6_forward': 0 if closed else -1,
                    'boot_id': state.get('boot_id', ''), 'lan': state.get('lan', ''),
                    'wan': state.get('wan', ''), 'tests': 'FAIL', 'rules': 'FAIL',
                    'sequence': sequence, 'reason': failure}
        try:
            backend.emit(document)
        except Exception:
            exit_code = 1
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--serve', action='store_true')
    action.add_argument('--stop-forwarding', action='store_true')
    args = parser.parse_args(argv)
    try:
        event = threading.Event()
        backend = LinuxBackend(event)
        if args.stop_forwarding:
            backend.disable_forwarding()
            return 0
        signal.signal(signal.SIGTERM, lambda _signum, _frame: event.set())
        signal.signal(signal.SIGINT, lambda _signum, _frame: event.set())
        return supervise(backend)
    except Exception as error:
        code = str(error) if isinstance(error, IntegrityError) else type(error).__name__
        print('CS_GATEWAY_INTEGRITY_FAIL:' + code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
