#!/usr/bin/env python3
"""Local paired regression check, not an AWS capacity or scaling claim."""
import argparse
import hashlib
import json
import pathlib
import shutil
import statistics
import subprocess
import time

import redis
from compare_correctness_cost_aws import trace
from realistic_workloads import validate_result
from dragonfly_hypotheses import ZSET_CHECK
import sys


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=180, **kwargs).stdout


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def cpu(container):
    return {k: int(v) for k, v in (line.split() for line in run(['docker', 'exec', container, 'cat', '/sys/fs/cgroup/cpu.stat']).splitlines())}


def zset_check(c, mode, writes=0):
    port = c.connection_pool.connection_kwargs['port']
    script = ZSET_CHECK.replace('(host,6379)', '(host,' + str(port) + ')')
    run([sys.executable, '-c', script, '127.0.0.1', mode, str(writes)])


def preload(c, workload):
    if workload == 'zset':
        zset_check(c, 'preload')
        return
    count = 100_000 if workload == 'get' else 4096
    for start in range(0, count, 128):
        with c.pipeline(transaction=False) as p:
            for k in range(start, min(start + 128, count)):
                key = f'cost:{k + 1 if workload == "get" else k}'
                if workload == 'get': p.set(key, 'v' * 256)
                elif workload == 'hash': p.hset(key, mapping={f'm:{m}': 'v' * 256 for m in range(16)})
                elif workload == 'list': p.rpush(key, *(['v' * 256] * 16))
                else: p.sadd(key, *[f'm:{m}' for m in range(16)])
            p.execute()


def verify(c, workload, writes=0):
    if workload == 'zset':
        zset_check(c, 'verify', writes)
        return
    count = 100_000 if workload == 'get' else 4096
    for start in range(0, count, 128):
        with c.pipeline(transaction=False) as p:
            for k in range(start, min(start + 128, count)):
                key = f'cost:{k + 1 if workload == "get" else k}'
                if workload == 'get': p.get(key)
                elif workload == 'hash': p.hgetall(key)
                elif workload == 'list': p.lrange(key, 0, -1)
                else: p.smembers(key)
            expected = (b'v' * 256 if workload == 'get' else
                        {f'm:{m}'.encode(): b'v' * 256 for m in range(16)} if workload == 'hash' else
                        [b'v' * 256] * 16 if workload == 'list' else
                        {f'm:{m}'.encode() for m in range(16)})
            assert all(value == expected for value in p.execute())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=pathlib.Path, required=True)
    parser.add_argument('--after', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--cpus', nargs='+', type=int, choices=(4, 8), default=[4, 8])
    parser.add_argument('--versions', nargs='+', choices=('before', 'after'), default=['before', 'after'])
    parser.add_argument('--workloads', nargs='+', choices=('get', 'hash', 'list', 'set-member', 'zset'), default=['get', 'hash', 'list', 'set-member'])
    o = parser.parse_args()
    root = o.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, root / 'runner.py')
    versions = {}
    for version in ('before', 'after'):
        target = root / (version + '-vex')
        shutil.copy2(getattr(o, version), target)
        versions[version] = hashlib.sha256(target.read_bytes()).hexdigest()
    assert versions['before'] != versions['after']
    for workload in ('hash', 'list', 'set-member', 'zset'): (root / (workload + '.trace')).write_text(trace(workload))
    network = 'vex-collection-check-' + str(time.time_ns())
    run(['docker', 'network', 'create', network])
    client = None
    cells = []
    try:
        client = run(['docker', 'run', '-d', '--rm', '--network', network, '--cpus', '8', '-v', str(root) + ':/results',
                      '--entrypoint', '/bin/sh', 'vex-memtier-monotonic:arm64', '-c', 'sleep 1800']).strip()
        save(root / 'protocol.json', dict(binaries=versions, cpus=o.cpus, versions=o.versions, workloads=o.workloads, workers='equal CPU quota', client_cpus=8,
            connections=32, threads=8, pipeline=1, distinct_client_seed=True, repetitions='3 x 10 seconds after 3 second warmup',
            collections='4096 keys x 16 members; same hash/list/set traces as AWS cost study',
            get='100000 keys x 256B; uniform reads', zset='64 sets x 4096 members; 80% ZINCRBY, 20% ZRANK', environment='local Docker VM; epoll; not AWS; no TTL/persistence; adaptive off; 256 partitions',
            docker_info=json.loads(run(['docker', 'info', '--format', '{{json .}}']))['NCPU']))
        for allocation in o.cpus:
            for wi, workload in enumerate(o.workloads):
                for version in (('before', 'after') if wi % 2 == 0 else ('after', 'before')):
                    if version not in o.versions: continue
                    out = root / f'c{allocation}-{version}-{workload}'; out.mkdir()
                    cmd = ['docker', 'run', '-d', '--rm', '--network', network, '--network-alias', 'engine', '--cpus', str(allocation),
                           '--memory', '3g', '-p', '127.0.0.1::6379', '-v', str(root / (version + '-vex')) + ':/vex-test:ro',
                           '-e', 'VEX_CONFIG=', '-e', 'VEX_PIN_WORKERS=0', '-e', 'VEX_EXPERIMENTAL_ADAPTIVE_OWNER=0', '--entrypoint', '/vex-test',
                           'vex-perf-local:arm64', '--reactor', '--workers', str(allocation), '--port', '6379', '--host', '0.0.0.0', '--no-persistence']
                    engine = run(cmd).strip(); save(out / 'server-command.json', cmd)
                    try:
                        port = int(run(['docker', 'port', engine, '6379/tcp']).strip().rsplit(':', 1)[1])
                        c = redis.Redis(port=port, socket_timeout=10, socket_connect_timeout=1)
                        for _ in range(100):
                            try:
                                if c.ping(): break
                            except redis.ConnectionError: pass
                            time.sleep(.1)
                        else: raise RuntimeError('server not ready')
                        preload(c, workload)
                        rows = []; writes = 0
                        for trial, seconds in [('warmup', 3)] + [(f'r{i}', 10) for i in (1, 2, 3)]:
                            remote = '/results/' + out.name + '/' + trial + '.raw.json'
                            command = ['docker', 'exec', client, 'memtier_benchmark', '-s', 'engine', '-p', '6379', '-t', '8', '-c', '4',
                                '--pipeline', '1', '--distinct-client-seed', '--test-time', str(seconds), '--hide-histogram', '--print-percentiles', '50,95,99,99.9', '--json-out-file', remote]
                            if workload == 'get': command += ['--ratio', '0:1', '--key-prefix', 'cost:', '--key-minimum', '1', '--key-maximum', '100000', '--key-pattern', 'R:R', '--data-size', '256']
                            else: command += ['--monitor-input', '/results/' + workload + '.trace', '--command', '__monitor_line@__', '--monitor-pattern', 'R']
                            before, cb = cpu(engine), cpu(client)
                            p = subprocess.run(command, capture_output=True, text=True, timeout=120)
                            after, ca = cpu(engine), cpu(client)
                            (out / (trial + '.stderr')).write_text(p.stderr); (out / (trial + '.stdout')).write_text(p.stdout)
                            save(out / (trial + '.command.json'), command)
                            assert p.returncode == 0, p.stderr
                            raw = json.loads((out / (trial + '.raw.json')).read_text())
                            row = validate_result(raw, p.stderr, seconds, 0, False)
                            count = raw['ALL STATS']['Totals']['Count']
                            writes += raw['ALL STATS'].get('Zincrbys', {}).get('Count', 0)
                            row['cpu_us_per_op'] = (after['usage_usec'] - before['usage_usec']) / count
                            row['client_throttled_seconds'] = (ca['throttled_usec'] - cb['throttled_usec']) / 1e6
                            row['client_valid'] = max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values()) < .9 and row['client_cpu']['cpu_cores_used'] < 6.4 and row['client_throttled_seconds'] < .01 * seconds
                            save(out / (trial + '.samples.json'), dict(server_before=before, server_after=after, client_before=cb, client_after=ca))
                            save(out / (trial + '.summary.json'), row)
                            assert row['client_valid'], row
                            if trial != 'warmup': rows.append(row)
                        verify(c, workload, writes); c.close()
                        state = json.loads(run(['docker', 'inspect', engine]))[0]
                        assert state['RestartCount'] == 0 and state['State']['Running'] and not state['State']['OOMKilled']
                        cell = dict(cpu=allocation, version=version, workload=workload, data_verified=True,
                                    **{metric: statistics.median(r[metric] for r in rows) for metric in ('ops_per_sec', 'p99_ms', 'cpu_us_per_op')})
                        save(out / 'complete.json', cell); cells.append(cell)
                        print(json.dumps(cell), flush=True)
                    finally:
                        (out / 'server.log').write_text(run(['docker', 'logs', engine]))
                        run(['docker', 'rm', '-f', engine])
        save(root / 'summary.json', cells)
    finally:
        cleanup = []
        if client: cleanup.append(run(['docker', 'rm', '-f', client]).strip())
        cleanup.append(run(['docker', 'network', 'rm', network]).strip())
        save(root / 'cleanup.json', cleanup)


if __name__ == '__main__': main()
