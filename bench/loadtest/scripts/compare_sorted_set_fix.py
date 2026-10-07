#!/usr/bin/env python3
"""Local paired diagnostic of prebuilt Vex sorted-set before/after binaries."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import time

from dragonfly_hypotheses import ZSET_CHECK, trace
from realistic_workloads import validate_result

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / 'bench/loadtest/runs/2026-09-27-sorted-set-fix'
NETWORK = 'vex-zset-fix-20260927'
SERVER, CLIENT = NETWORK + '-server', NETWORK + '-client'
IMAGE = 'vex-perf-local:arm64'
CLIENT_IMAGE = 'vex-memtier-monotonic:arm64'


def docker(*args, timeout=120, check=True):
    return subprocess.run(['docker', *map(str, args)], capture_output=True, text=True, timeout=timeout, check=check)


def save(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='local-pairs')
    parser.add_argument('--after-binary', default='server-after')
    parser.add_argument('--before-binary', default='server-before')
    options = parser.parse_args()
    binaries = {'before': options.before_binary, 'after': options.after_binary}
    for kind, name in [('network', NETWORK), ('container', SERVER), ('container', CLIENT)]:
        assert docker(kind, 'inspect', name, check=False).returncode != 0, 'resource already exists: ' + name
    out = OUT / options.label
    out.mkdir(exist_ok=False)
    manifest = dict(classification='local Docker diagnostic, not AWS release comparison',
                    cpu_quota=2, workers=2, pinning=False, memory='2g', client_cpu_quota=4,
                    threads=4, clients_per_thread=16, pipeline=4, warmup_seconds=5, seconds=20,
                    rounds=3, boards=64, members_per_board=4096,
                    binaries={v: hashlib.sha256((OUT / binaries[v]).read_bytes()).hexdigest() for v in ('before', 'after')},
                    server_image=json.loads(docker('image', 'inspect', IMAGE).stdout)[0]['Id'],
                    client_image=json.loads(docker('image', 'inspect', CLIENT_IMAGE).stdout)[0]['Id'])
    save(out / 'protocol.json', manifest)
    for profile in ('update', 'mixed'):
        save(out / (profile + '.trace'), trace(mixed=profile == 'mixed'))
    rows = []
    try:
        docker('network', 'create', NETWORK)
        docker('run', '-d', '--name', CLIENT, '--network', NETWORK, '--cpuset-cpus', '2-5', '--cpus', 4,
               '--memory', '1g', '-v', str(out) + ':/results', '--entrypoint', 'sh', CLIENT_IMAGE, '-c', 'sleep 3600')
        for round_no in range(1, 4):
            for profile in ('update', 'mixed'):
                for version in (('before', 'after') if round_no % 2 else ('after', 'before')):
                    cell = out / f'r{round_no}-{profile}-{version}'
                    cell.mkdir()
                    docker('run', '-d', '--name', SERVER, '--network', NETWORK, '--cpuset-cpus', '0-1', '--cpus', 2,
                           '--memory', '2g', '--memory-swap', '2g', '-e', 'VEX_PIN_WORKERS=0',
                           '-v', str(OUT / binaries[version]) + ':/vex:ro', '--entrypoint', '/vex', IMAGE,
                           '--reactor', '--workers', 2, '--no-persistence', '--port', 6379)
                    try:
                        for attempt in range(30):
                            probe = docker('exec', CLIENT, 'python3', '-c',
                                'import socket,sys;s=socket.create_connection((sys.argv[1],6379),1);s.sendall(b"*1\\r\\n$4\\r\\nPING\\r\\n");assert s.recv(128)==b"+PONG\\r\\n"', SERVER, check=False)
                            if probe.returncode == 0:
                                break
                            time.sleep(.2)
                        else:
                            raise RuntimeError(probe.stderr)
                        save(cell / 'preload.json', docker('exec', CLIENT, 'python3', '-c', ZSET_CHECK, SERVER, 'preload', timeout=300).stdout)
                        increments = 0
                        for phase, seconds in (('warmup', 5), ('measure', 20)):
                            save(cell / (phase + '.server-before.txt'), docker('exec', SERVER, 'cat', '/proc/1/status', '/sys/fs/cgroup/cpu.stat', '/sys/fs/cgroup/memory.events').stdout)
                            command = ['exec', CLIENT, 'memtier_benchmark', '-s', SERVER, '-p', 6379, '-t', 4, '-c', 16,
                                       '--pipeline', 4, '--test-time', seconds, '--distinct-client-seed', '--hide-histogram',
                                       '--print-percentiles', '50,95,99,99.9', '--monitor-input', '/results/' + profile + '.trace',
                                       '--command', '__monitor_line@__', '--monitor-pattern', 'R',
                                       '--json-out-file', '/results/' + cell.name + '/' + phase + '.json']
                            save(cell / (phase + '.command.json'), command)
                            result = docker(*command, timeout=seconds + 90)
                            save(cell / (phase + '.stdout'), result.stdout)
                            save(cell / (phase + '.stderr'), result.stderr)
                            save(cell / (phase + '.server-after.txt'), docker('exec', SERVER, 'cat', '/proc/1/status', '/sys/fs/cgroup/cpu.stat', '/sys/fs/cgroup/memory.events').stdout)
                            raw = json.loads((cell / (phase + '.json')).read_text())
                            row = validate_result(raw, result.stderr, seconds, 0, False)
                            stats = raw['ALL STATS']
                            count = stats['Totals']['Count']
                            writes = sum(v['Count'] for k, v in stats.items() if k.lower().rstrip('s') == 'zincrby')
                            ranks = sum(v['Count'] for k, v in stats.items() if k.lower().rstrip('s') == 'zrank')
                            assert writes + ranks == count
                            assert (.19 < ranks / count < .21) if profile == 'mixed' else ranks == 0
                            increments += writes
                            if phase == 'measure':
                                row.update(round=round_no, profile=profile, version=version, operations=count,
                                           client_headroom_ok=max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values()) < .9 and row['client_cpu']['cpu_cores_used'] < 3.2)
                        save(cell / 'verify.json', docker('exec', CLIENT, 'python3', '-c', ZSET_CHECK, SERVER, 'verify', increments, timeout=300).stdout)
                        save(cell / 'complete.json', row)
                        rows.append(row)
                        save(out / 'summary.json', rows)
                        print('COMPLETE', cell.name, round(row['ops_per_sec']), 'p99', row['p99_ms'], 'client_valid', row['client_headroom_ok'], flush=True)
                    finally:
                        logs = docker('logs', SERVER, check=False)
                        save(cell / 'server.log', logs.stdout + logs.stderr)
                        docker('rm', '-f', SERVER, check=False)
    finally:
        for name in (SERVER, CLIENT):
            docker('rm', '-f', name, check=False)
        docker('network', 'rm', NETWORK, check=False)
        save(out / 'cleanup.json', {name: docker(kind, 'inspect', name, check=False).returncode != 0
             for kind, name in [('container', SERVER), ('container', CLIENT), ('network', NETWORK)]})


if __name__ == '__main__':
    main()
