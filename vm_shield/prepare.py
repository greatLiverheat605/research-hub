#!/usr/bin/env python3
"""Build isolated, credential-free hardening candidates; never boot or deploy.

The previous combined provisioner also edited live VM resources and copied client
applications. Preparation is now a separate, explicit candidate build. Activation
requires its own reviewed deployment plan and does not run through this command.
"""
import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]
GATEWAY_INPUTS = {
    'residential.nft': 'audit/gateway/residential.nft',
    'residential-gateway.service': 'audit/gateway/residential-gateway.service',
    '90-residential.conf': 'audit/gateway/90-residential.conf',
    'dnsmasq-lan.conf': 'audit/gateway/dnsmasq-lan.conf',
    'gateway-offline-test.py': 'vm/gateway-offline-test.py',
    'gateway-integrity.py': 'vm/gateway-integrity.py',
}
GUEST_INPUTS = {
    'install-in-guest.sh': 'vm/install-in-guest.sh',
    'repair-guest-network.sh': 'vm/repair-guest-network.sh',
    'Install.command': 'vm/Install.command',
    'guest-network-common.sh': 'vm/guest-network-common.sh',
    'guest-config-check.rb': 'vm/guest-config-check.rb',
}
LAUNCHER_INPUTS = ['vm/VMCoordinator.swift', 'vm/GatewayReadiness.swift',
                   'vm/ClipboardWire.swift', 'vm/GuestShareAudit.swift',
                   'vm/ControlPanel.swift', 'vm/DemoGuest.swift', 'vm/PanelSelfTest.swift',
                   'vm/virtualization.entitlements']


def maintenance_seed_documents(manifest, payload, deployer):
    """Cloud-init delivery for an explicitly isolated gateway-only update boot.

    The new launcher disconnects WAN in --stage-gateway-update. No macOS guest is
    configured. The transaction stages files with forwarding off and powers down;
    only a later normal boot with a NEW CSRUN challenge may enable forwarding.
    """
    import textwrap
    files = []
    for name, data in payload.items():
        files.append({'path': '/run/claudeshield-update/gateway/'+name,
                      'permissions': '0600', 'content': data.decode('utf-8')})
    files.append({'path': '/run/claudeshield-update/gateway/manifest.json',
                  'permissions': '0600', 'content': json.dumps(manifest,sort_keys=True)})
    files.append({'path': '/run/claudeshield-update/deploy-gateway-bundle.py',
                  'permissions': '0600', 'content': deployer.decode('utf-8')})
    runner = textwrap.dedent('''\
        import importlib.util, json, pathlib, subprocess
        succeeded = False
        try:
            subprocess.run(['/usr/bin/python3', '/run/claudeshield-update/deploy-gateway-bundle.py',
                            'apply', '--bundle', '/run/claudeshield-update/gateway'], check=True)
            spec = importlib.util.spec_from_file_location('gateway_integrity', '/etc/claudeshield/gateway-integrity.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            backend = module.LinuxBackend()
            backend.disable_forwarding()
            module.require_deployment_complete()
            bundle = backend.bundle()
            challenge = backend.challenge(bundle['gateway_bundle_sha256'])
            if backend.forwarding() != (0, 0):
                raise RuntimeError('forwarding not closed')
            # Linux kernel policy exercise runs entirely in disconnected local
            # namespaces. The maintenance WAN itself has no NAT attachment.
            backend.offline_reference()
            if backend.bundle() != bundle or backend.forwarding() != (0, 0):
                raise RuntimeError('bundle or forwarding changed during offline tests')
            receipt = {'schema_version':1, 'run_id':challenge['run_id'],
                       'gateway_bundle_sha256':bundle['gateway_bundle_sha256'],
                       'state':'installed_forwarding_off_pending_cold_boot', 'forward':0,
                       'offline_tests':'PASS'}
            with pathlib.Path('/dev/hvc0').open('w') as console:
                console.write('CS_GATEWAY_DEPLOYMENT_JSON:'+json.dumps(receipt,sort_keys=True,separators=(',',':'))+'\\n')
                console.flush()
            succeeded = True
        except Exception:
            with pathlib.Path('/dev/hvc0').open('w') as console:
                console.write('CS_GATEWAY_DEPLOYMENT_FAIL\\n');console.flush()
        finally:
            subprocess.run(['/usr/sbin/shutdown', '-h', 'now'], check=False)
        raise SystemExit(0 if succeeded else 1)
        ''')
    files.append({'path':'/run/claudeshield-update/stage.py','permissions':'0600','content':runner})
    user_data = {'disable_root':True,'ssh_pwauth':False,'package_update':False,
                 'output':{'all':'| tee -a /var/log/cloud-init-output.log /dev/hvc0'},
                 'write_files':files,'runcmd':[['/usr/bin/python3','/run/claudeshield-update/stage.py']]}
    meta = {'instance-id':'claudeshield-gateway-v4-'+manifest['gateway_bundle_sha256'],
            'local-hostname':'residential-gateway'}
    network = {'version':2,'ethernets':{
        'wan':{'match':{'macaddress':'52:54:00:77:00:01'},'set-name':'wan0',
               'dhcp4':True,'dhcp6':False,'accept-ra':False,'optional':True},
        'lan':{'match':{'macaddress':'52:54:00:77:00:02'},'set-name':'lan0',
               'addresses':['10.77.0.1/24'],'dhcp4':False,'dhcp6':False,
               'accept-ra':False,'optional':True}}}
    return {'user-data':'#cloud-config\n'+json.dumps(user_data,indent=2),
            'meta-data':json.dumps(meta,indent=2), 'network-config':json.dumps(network,indent=2)}


def canonical_digest(mapping):
    raw = json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def source_bytes(path):
    p = Path(path)
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 5 * 1024 * 1024:
        raise ValueError('Unsafe candidate source')
    if any(parent.is_symlink() for parent in (p, *p.parents)):
        raise ValueError('Symlinked candidate source')
    data = p.read_bytes()
    after = p.stat()
    if (st.st_ino, st.st_size, st.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Candidate source changed during read')
    return data


def copy_inputs(mapping, destination, repo=REPO):
    destination.mkdir(mode=0o700)
    hashes = {}
    for basename, relative in mapping.items():
        data = source_bytes(repo / relative)
        p = destination / basename
        p.write_bytes(data)
        p.chmod(0o600)
        hashes[basename] = hashlib.sha256(data).hexdigest()
    return hashes


def publish_exclusive(source, destination):
    # macOS header sys/stdio.h defines RENAME_EXCL=0x00000004. An ordinary
    # rename could overwrite a concurrently created empty destination directory.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, 'renamex_np'):
        raise RuntimeError('Exclusive candidate publication requires macOS renamex_np')
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
        raise OSError(ctypes.get_errno(), 'Exclusive candidate publication failed')


@contextlib.contextmanager
def candidate_lock(output):
    output = Path(output).absolute()
    path = output.parent / ('.'+output.name+'.build.lock')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe candidate lock path')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise ValueError('Unsafe candidate lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def build_candidate(output, compile_launcher=False, client_date=None, repo=REPO, maintenance_media=False):
    with candidate_lock(output):
        return _build_candidate(output, compile_launcher, client_date, repo, maintenance_media)


def _build_candidate(output, compile_launcher=False, client_date=None, repo=REPO, maintenance_media=False):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Candidate output already exists; never overwrite')
    if not output.parent.is_dir() or any(p.is_symlink() for p in (output.parent, *output.parent.parents)):
        raise ValueError('Candidate parent must be an existing regular directory')
    os.umask(0o077)
    # Stage all inputs before exposing a complete candidate directory.
    with tempfile.TemporaryDirectory(prefix='.candidate-build-', dir=output.parent) as tmp:
        stage = Path(tmp) / 'candidate'
        stage.mkdir(mode=0o700)
        gateway_files = copy_inputs(GATEWAY_INPUTS, stage / 'gateway', repo)
        guest_files = copy_inputs(GUEST_INPUTS, stage / 'guest-network-sources', repo)
        manifest = {'schema_version': 4, 'gateway_files': gateway_files,
                    'gateway_bundle_sha256': canonical_digest(gateway_files)}
        for target in [stage / 'manifest.json', stage / 'gateway/manifest.json']:
            target.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
            target.chmod(0o600)
        deployer = source_bytes(repo / 'vm/deploy-gateway-bundle.py')
        (stage / 'deploy-gateway-bundle.py').write_bytes(deployer)
        payload = {name:(stage/'gateway'/name).read_bytes() for name in GATEWAY_INPUTS}
        documents = maintenance_seed_documents(manifest,payload,deployer)
        seed = stage/'maintenance-seed'
        seed.mkdir(mode=0o700)
        for name,text in documents.items():
            (seed/name).write_text(text)
        launcher_dir = stage / 'launcher-sources'
        launcher_dir.mkdir(mode=0o700)
        launcher_hashes = {}
        for relative in LAUNCHER_INPUTS:
            data = source_bytes(repo / relative)
            (launcher_dir / Path(relative).name).write_bytes(data)
            launcher_hashes[Path(relative).name] = hashlib.sha256(data).hexdigest()
        receipt = {
            'schema_version': 1, 'artifact_kind': 'offline-hardening-candidate',
            'client_date': client_date, 'built_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'gateway_bundle_sha256': manifest['gateway_bundle_sha256'],
            'gateway_files': gateway_files, 'guest_source_files': guest_files,
            'launcher_source_files': launcher_hashes,
            'deployment_script_sha256': hashlib.sha256(deployer).hexdigest(),
            'contains_credentials': False, 'contains_client_apps': False,
            'vm_disks_accessed': False, 'activated': False,
            'guest_payload_status': 'not_assembled_requires_existing_user_owned_config_and_core',
            'launcher_status': 'source_only',
        }
        if maintenance_media:
            image = stage/'gateway-seed.iso'
            subprocess.run(['hdiutil','makehybrid','-iso','-joliet','-default-volume-name',
                            'cidata','-o',str(image),str(seed)],check=True)
            if not image.is_file() or image.stat().st_size == 0:
                raise RuntimeError('Maintenance seed image was not created')
            image.chmod(0o400)
            receipt['maintenance_seed_sha256']=hashlib.sha256(image.read_bytes()).hexdigest()
            receipt['maintenance_mode']='gateway_only_disconnected_wan_then_poweroff'
        if compile_launcher:
            app = stage / 'ClaudeShield VM Candidate.app'
            binary_dir = app / 'Contents/MacOS'
            binary_dir.mkdir(parents=True)
            # Default points to a NONEXISTENT directory: double-clicking this
            # candidate can never acquire the real environment or boot its disks.
            # Actual deployment must explicitly create a separately reviewed app.
            inactive_environment = output / 'not-activated'
            info = {
                'CFBundleExecutable': 'ClaudeShieldVM',
                'CFBundleIdentifier': 'local.claudeshield.virtual-machine-candidate',
                'CFBundleName': 'ClaudeShield VM Candidate', 'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': '0.4', 'CFBundleVersion': '4',
                'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
                'EnvironmentPath': str(inactive_environment),
            }
            with (app / 'Contents/Info.plist').open('wb') as f:
                plistlib.dump(info, f)
            binary = binary_dir / 'ClaudeShieldVM'
            subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-O',
                            str(launcher_dir / 'VMCoordinator.swift'),
                            str(launcher_dir / 'GatewayReadiness.swift'),
                            str(launcher_dir / 'ClipboardWire.swift'),
                            str(launcher_dir / 'GuestShareAudit.swift'),
                            str(launcher_dir / 'ControlPanel.swift'),
                            str(launcher_dir / 'DemoGuest.swift'),
                            str(launcher_dir / 'PanelSelfTest.swift'), '-o', str(binary)], check=True)
            subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements',
                            str(launcher_dir / 'virtualization.entitlements'), str(app)], check=True)
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
            receipt['launcher_status'] = 'compiled_and_signature_verified_not_started'
            receipt['launcher_binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
            receipt['launcher_default_environment'] = 'not-activated'
        # Recheck every selected original to detect a concurrent source edit.
        for name, relative in GATEWAY_INPUTS.items():
            if hashlib.sha256(source_bytes(repo / relative)).hexdigest() != gateway_files[name]:
                raise ValueError('Gateway source changed while building candidate')
        for name, relative in GUEST_INPUTS.items():
            if hashlib.sha256(source_bytes(repo / relative)).hexdigest() != guest_files[name]:
                raise ValueError('Guest source changed while building candidate')
        for relative in LAUNCHER_INPUTS:
            if hashlib.sha256(source_bytes(repo / relative)).hexdigest() != launcher_hashes[Path(relative).name]:
                raise ValueError('Launcher source changed while building candidate')
        if hashlib.sha256(source_bytes(repo / 'vm/deploy-gateway-bundle.py')).hexdigest() != receipt['deployment_script_sha256']:
            raise ValueError('Deployment source changed while building candidate')
        (stage / 'artifact-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
        (stage / 'README.txt').write_text(
            'Offline candidate only. No production files or VM disks were modified.\n'
            'Do not treat this directory as a deployed VM environment.\n'
            'Gateway deployment, guest payload assembly and VM startup each require the reviewed maintenance plan.\n'
            'The candidate launcher defaults to a nonexistent environment.\n'
            'No Claude/Chrome application, node credentials or subscriptions are bundled.\n')
        publish_exclusive(stage, output)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('build-candidate', help='Build offline source/candidate artifacts only')
    p.add_argument('--output', required=True)
    p.add_argument('--compile-launcher', action='store_true')
    p.add_argument('--maintenance-media', action='store_true', help='Build local read-only delivery ISO; never boot it')
    p.add_argument('--client-date', default=None)
    args = parser.parse_args()
    receipt = build_candidate(args.output, args.compile_launcher, args.client_date,
                              maintenance_media=args.maintenance_media)
    print(json.dumps({'artifact_kind': receipt['artifact_kind'],
                      'gateway_bundle_sha256': receipt['gateway_bundle_sha256'],
                      'launcher_status': receipt['launcher_status'], 'activated': False}, indent=2))


if __name__ == '__main__':
    main()
