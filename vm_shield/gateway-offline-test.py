#!/usr/bin/env python3
"""Test the verified gateway bundle in disconnected Linux network namespaces.

No test interface reaches the VM WAN, host, or Internet. Public-looking addresses
are local mocks inside namespaces. Importing this file performs no system action.
The privileged CLI is restricted to the dedicated Linux gateway's fixed NICs.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import textwrap


def load_integrity():
    spec = importlib.util.spec_from_file_location('gateway_integrity',
                                                Path(__file__).with_name('gateway-integrity.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready_line(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(5):
            raise RuntimeError('OFFLINE_MOCK_START_TIMEOUT')
        return stream.readline().strip()


def run_offline_tests():
    integrity = load_integrity()
    integrity.require_gateway()
    integrity.verify_bundle(root_owned=True)
    os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    prefix = 'cs-offline-' + str(os.getpid())
    client, router, server = [prefix + '-' + side for side in ['client', 'router', 'server']]
    created = []
    process = None
    gw_proc = None

    def run(args, namespace=None, **kwargs):
        command = (['ip', 'netns', 'exec', namespace] if namespace else []) + args
        kwargs.setdefault('timeout', 15)
        return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)

    try:
        for mod in ['nf_tables', 'nft_nat', 'nft_masq', 'nft_chain_nat_ipv4', 'nf_tables_set']:
            subprocess.run(['modprobe', '-q', mod], check=False, capture_output=True, timeout=10)
        for name in [client, router, server]:
            run(['ip', 'netns', 'add', name])
            created.append(name)
            run(['ip', 'link', 'set', 'lo', 'up'], name)
        for local, peer, destination in [('lan0', 'client0', client), ('wan0', 'server0', server)]:
            run(['ip', 'link', 'add', local, 'type', 'veth', 'peer', 'name', peer], router)
            run(['ip', 'link', 'set', peer, 'netns', destination], router)
            run(['ip', 'link', 'set', local, 'up'], router)
            run(['ip', 'link', 'set', peer, 'up'], destination)
        for namespace, device, ipv4, ipv6 in [
            (client, 'client0', '10.77.0.10/24', 'fd77:1::2/64'),
            (router, 'lan0', '10.77.0.1/24', 'fd77:1::1/64'),
            (router, 'wan0', '192.0.2.1/24', 'fd77:2::1/64'),
            (server, 'server0', '192.0.2.2/24', 'fd77:2::2/64')
        ]:
            run(['ip', 'addr', 'add', ipv4, 'dev', device], namespace)
            run(['ip', '-6', 'addr', 'add', ipv6, 'dev', device, 'nodad'], namespace)
        for address in ['198.51.100.24/32', '198.51.100.1/32', '1.1.1.1/32']:
            run(['ip', 'addr', 'add', address, 'dev', 'lo'], server)
        for namespace, ipv4, ipv6 in [(client, '10.77.0.1', 'fd77:1::1'),
                                      (router, '192.0.2.2', 'fd77:2::2'),
                                      (server, '192.0.2.1', 'fd77:2::1')]:
            run(['ip', 'route', 'add', 'default', 'via', ipv4], namespace)
            run(['ip', '-6', 'route', 'add', 'default', 'via', ipv6], namespace)
        run(['sysctl', '-w', 'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1'], router)
        run(['nft', '-f', '/etc/claudeshield/residential.nft'], router)
        reference = integrity.canonical_ruleset(integrity.strict_json(run(['nft', '-j', 'list', 'ruleset'], router).stdout))
        mock = r'''
    import socket, threading, time
    def tcp(sock):
        while True:
            connection, _ = sock.accept()
            with connection: connection.sendall(b'OK')
    def udp(sock):
        while True:
            data, address = sock.recvfrom(1024)
            sock.sendto(b'OK', address)
    for address, port, kind in [
        ('198.51.100.24', 26333, 'tcp'), ('198.51.100.24', 443, 'tcp'),
        ('198.51.100.24', 26333, 'udp'), ('198.51.100.24', 443, 'udp'),
        ('198.51.100.1', 443, 'tcp'), ('1.1.1.1', 53, 'udp'),
        ('1.1.1.1', 53, 'tcp'), ('fd77:2::2', 443, 'tcp')
    ]:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM if kind == 'udp' else socket.SOCK_STREAM)
        sock.bind((address, port))
        if kind == 'tcp': sock.listen()
        threading.Thread(target=udp if kind == 'udp' else tcp, args=(sock,), daemon=True).start()
    print('READY', flush=True)
    while True: time.sleep(60)
    '''
        process = subprocess.Popen(['ip', 'netns', 'exec', server, sys.executable, '-u', '-c', textwrap.dedent(mock)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if ready_line(process.stdout) != 'READY':
            raise RuntimeError('Offline mock listeners could not start')
        gw_mock = r'''
    import socket, threading, time
    for port in [22, 53]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('10.77.0.1', port))
        s.listen()
        threading.Thread(target=lambda sock=s: sock.accept()[0].sendall(b'OK'), daemon=True).start()
    print('GW_READY', flush=True)
    while True: time.sleep(60)
    '''
        gw_proc = subprocess.Popen(['ip', 'netns', 'exec', router, sys.executable, '-u', '-c', textwrap.dedent(gw_mock)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if ready_line(gw_proc.stdout) != 'GW_READY':
            raise RuntimeError('Gateway mock listeners could not start')
        connection = r'''
    import socket, sys
    kind, address, port, expected = sys.argv[1:]
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM if kind == 'udp' else socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        try:
            sock.connect((address, int(port)))
            if kind == 'udp': sock.send(b'offline')
            received = sock.recv(2) == b'OK'
        except (TimeoutError, OSError): received = False
        if received != (expected == 'allow'): raise SystemExit(1)
    '''
        cases = [('tcp', '198.51.100.24', '26333', 'allow'),
                 ('tcp', '198.51.100.24', '443', 'deny'),
                 ('udp', '198.51.100.24', '26333', 'deny'),
                 ('udp', '198.51.100.24', '443', 'deny'),
                 ('tcp', '198.51.100.1', '443', 'deny'),
                 ('udp', '1.1.1.1', '53', 'deny'),
                 ('tcp', '1.1.1.1', '53', 'deny'),
                 ('tcp', 'fd77:2::2', '443', 'deny'),
                 ('tcp', '10.77.0.1', '22', 'deny'),
                 ('tcp', '10.77.0.1', '53', 'deny')]
        for case in cases:
            run([sys.executable, '-c', textwrap.dedent(connection), *case], client, timeout=5)
        # The gateway's own sockets cannot reach the WAN side (the host NAT), even the relay.
        for case in [('tcp', '198.51.100.1', '443', 'deny'),
                     ('tcp', '198.51.100.24', '26333', 'deny'),
                     ('udp', '1.1.1.1', '53', 'deny'),
                     ('tcp', 'fd77:2::2', '443', 'deny')]:
            run([sys.executable, '-c', textwrap.dedent(connection), *case], router, timeout=5)
        run(['sysctl', '-w', 'net.ipv4.ip_forward=0'], router)
        run([sys.executable, '-c', textwrap.dedent(connection), 'tcp', '198.51.100.24', '26333', 'deny'],
            client, timeout=5)
        return reference
    finally:
        cleanup_errors = []
        for child in [gw_proc, process]:
            if child is not None:
                try:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                except Exception as error:
                    cleanup_errors.append(error)
        for name in reversed(created):
            try:
                subprocess.run(['ip', 'netns', 'del', name], check=True, capture_output=True, timeout=5)
            except Exception as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            raise RuntimeError('OFFLINE_NAMESPACE_CLEANUP_FAILED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emit-canonical', action='store_true')
    args = parser.parse_args(argv)
    try:
        reference = run_offline_tests()
        if args.emit_canonical:
            print(json.dumps({'schema_version': 1, 'tests': 'PASS', 'ruleset': reference},
                             sort_keys=True, separators=(',', ':')))
        else:
            print('CS_GATEWAY_OFFLINE_TESTS_PASS: isolated protocol and failure cases passed')
        return 0
    except Exception as error:
        # No raw subprocess output, addresses, or configuration contents in logs.
        print('CS_GATEWAY_OFFLINE_TESTS_FAIL:' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
