#!/usr/bin/env python3
"""Current Vex GET/hash/zset scaling on isolated AWS server/client hosts."""
import argparse
import hashlib
import json
import pathlib
import shutil
import time

import compare_correctness_cost_aws as c

b, s, g = c.b, c.s, c.g
WORKLOADS = ('get', 'hash', 'zset')
CONNECTIONS = (32, 64, 128, 256, 512, 1024)


def prepare(o):
    o.output.mkdir(parents=True, exist_ok=False)
    old = json.loads((b.SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
        obj = json.loads((b.SOURCE / name).read_text().replace(old, o.namespace))
        if name == 'resources.json':
            for item in obj['items'][1:]:
                item['spec']['template']['spec']['expireAfter'] = '6h'
                for req in item['spec']['template']['spec']['requirements']:
                    if req['key'] == 'topology.kubernetes.io/zone': req['values'] = [o.zone]
                    if req['key'] == 'node.kubernetes.io/instance-type' and item['metadata']['name'].endswith('-server'):
                        req['values'] = [b.SERVER_TYPE]
        else:
            obj['spec']['activeDeadlineSeconds'] = 21600
            for container in obj['spec']['containers']:
                if container.get('command') == ['sleep', '14400']: container['command'] = ['sleep', '21600']
        s.write(o.output / name, obj)
    o.template = json.loads((b.SOURCE / 'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    o.template['spec']['activeDeadlineSeconds'] = 21600
    b.BUILD = o.output / 'frozen'
    dest = b.BUILD / 'static-after/bin/vex'
    dest.parent.mkdir(parents=True)
    shutil.copy2(o.binary, dest)
    s.write(o.output / 'protocol.json', dict(
        binary_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
        candidate='current request-path optimized Vex; no older engine measured',
        cpus=o.cpus, workloads=WORKLOADS, connections=CONNECTIONS,
        server=b.SERVER_TYPE, client='c7g.16xlarge', client_cpu_quota=48,
        zone=o.zone, server_memory='8Gi', workers='equal CPU quota; unpinned; adaptive disabled; 256 partitions',
        arrival_model='uncapped closed loop; pipeline 1; service latency',
        search_seconds=15, confirmation='3 x 60 seconds at fastest passing setting; fall back if needed',
        p99_budget_ms=5, get='1 million 256-byte values, uniform reads',
        hash='4096 hashes x 16 fields x 256B; 80% HSET / 20% HGET',
        zset='64 sets x 4096 members; 80% ZINCRBY / 20% ZRANK',
        client_gate='max thread <0.9 CPU; aggregate <38.4 CPU; throttling <1% duration',
        network_gate='zero ENA allowance-exceeded deltas', ttl=False, persistence=False,
        max_runtime_seconds=18000))
    source = o.output / 'source'; source.mkdir()
    for path in pathlib.Path(__file__).parent.glob('*.py'): shutil.copy2(path, source / path.name)
    for name in ('src/server/worker.zig', 'src/command/handler.zig'):
        target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(b.ROOT / name, target)


def cell(o, cpu, workload):
    out = o.output / f'c{cpu}-after-{workload}'; out.mkdir()
    host, engine, topology, instances = b.start(o, cpu, 'after', out, partitions=256)
    net_before = b.ena(out, 'before', instances)
    if workload == 'get':
        g.phase(o, out, host, engine, workload, 1, 0, 'preload', preload=True)
    else:
        command = ['python3', '-c', b.ZSET_CHECK if workload == 'zset' else c.CHECK, host, 'preload']
        if workload != 'zset': command.append(workload)
        result = b.remote_job(command, '/results/preload-' + out.name, 600)
        s.write(out / 'preload.json', result.stdout); assert result.returncode == 0, result.stderr
    rows = [c.phase(o, out, host, engine, workload, 32, 10, 'warmup')]
    trials = []
    # Measure every requested connection count, even after crossing the latency
    # budget. Invalid/client-limited trials never qualify for confirmation.
    for connections in CONNECTIONS:
        row = c.phase(o, out, host, engine, workload, connections, 15, f'search-c{connections}')
        rows.append(row); trials.append(row)
    confirmed = []
    for candidate in sorted((r for r in trials if r['pass_5ms']), key=lambda r: r['ops_per_sec'], reverse=True):
        assert time.monotonic() < o.deadline, 'bounded run deadline'
        connections = candidate['connections']
        group = [c.phase(o, out, host, engine, workload, connections, 60, f'confirm-c{connections}-r{r}') for r in (1, 2, 3)]
        rows += group
        if all(r['pass_5ms'] for r in group): confirmed = group; break
    if workload == 'get':
        s.write(out / 'verify.json', s.sampled_values(host))
    else:
        total = sum(r.get('writes', 0) for r in rows)
        command = ['python3', '-c', b.ZSET_CHECK if workload == 'zset' else c.CHECK, host, 'verify']
        if workload != 'zset': command.append(workload)
        result = b.remote_job(command + [str(total)], '/results/verify-' + out.name, 600)
        s.write(out / 'verify.json', result.stdout); assert result.returncode == 0, result.stderr
    s.assert_final_topology(topology, engine)
    net_after = b.ena(out, 'after', instances)
    deltas = {role: {k: net_after[role][k] - v for k, v in values.items()} for role, values in net_before.items()}
    result = dict(cpu=cpu, workload=workload, search=trials, confirmed=confirmed,
                  network_deltas=deltas, network_valid=all(v == 0 for values in deltas.values() for v in values.values()))
    s.write(out / 'complete.json', result)
    print('COMPLETE', out.name, flush=True)
    return result


def resume(o):
    protocol = json.loads((o.output / 'protocol.json').read_text())
    assert protocol['cpus'] == o.cpus and protocol['zone'] == o.zone
    assert time.time() - (o.output / 'protocol.json').stat().st_mtime < protocol['max_runtime_seconds']
    b.BUILD = o.output / 'frozen'
    for path in (o.binary, b.BUILD / 'static-after/bin/vex'):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == protocol['binary_sha256']
    resources = json.loads((o.output / 'resources.json').read_text())
    assert resources['items'][0]['metadata']['name'] == o.namespace
    for item in resources['items']:
        live = json.loads(s.kub('get', item['kind'], item['metadata']['name'], '-o', 'json'))
        assert live['metadata']['labels']['bench.vex.dev/run'] == o.namespace
        assert not live['metadata'].get('deletionTimestamp'), 'cleanup already started'
    nodes = json.loads(s.kub('get', 'nodes', '-l', 'bench.vex.dev/run=' + o.namespace, '-o', 'json'))
    original = json.loads((o.output / 'nodes.json').read_text())
    identity = lambda ns: {(x['metadata']['uid'], x['spec']['providerID'], x['status']['nodeInfo']['bootID']) for x in ns['items']}
    assert len(nodes['items']) == 2 and identity(nodes) == identity(original), 'hosts changed; new study required'
    old = json.loads((b.SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    o.template = json.loads((b.SOURCE / 'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    o.template['spec']['activeDeadlineSeconds'] = 21600
    stamp = str(time.time_ns())
    s.write(o.output / f'resume-{stamp}.json', dict(nodes=nodes, reason='credential refresh; restart interrupted cells'))
    s.write(o.output / f'runner-resume-{stamp}.py', pathlib.Path(__file__).read_text())
    # Stop any detached load before a fresh trial; keep the original hosts.
    s.kub('delete', 'pod', 'client', '--wait=true', '--timeout=90s')
    s.kub('create', '-f', str(o.output / 'client-48.json'))
    for path in o.output.glob('c*-after-*'):
        if path.is_dir() and not (path / 'complete.json').exists():
            excluded = o.output / 'interrupted'; excluded.mkdir(exist_ok=True)
            path.rename(excluded / (path.name + '-' + stamp))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--binary', type=pathlib.Path, required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--zone', default='ap-south-1b')
    p.add_argument('--cpus', nargs='+', type=int, choices=(1, 2, 4, 8, 16), default=[1, 2, 4, 8, 16])
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--resume', action='store_true')
    o = p.parse_args(); o.output = o.output.resolve(); o.refine = True
    assert o.namespace.startswith('vex-scaling-')
    s.BASE, s.NS = o.output, o.namespace; s.K = ['kubectl', '--context', s.CTX, '-n', o.namespace]
    if not o.prepare_only:
        assert json.loads(s.run(['aws', 'sts', 'get-caller-identity']).stdout)['Account'] == '208168340597'
        if not o.resume:
            for kind, name in [('namespace', o.namespace), ('nodepool', o.namespace + '-server'), ('nodepool', o.namespace + '-client')]:
                assert not s.kub('get', kind, name, '--ignore-not-found', '-o', 'name').strip()
    assert not (o.resume and o.prepare_only)
    if not o.resume: prepare(o)
    if o.prepare_only: return
    o.deadline = time.monotonic() + 18000 - (time.time() - (o.output / 'protocol.json').stat().st_mtime)
    try:
        if o.resume:
            resume(o)
        else:
            for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
                s.kub('create', '-f', str(o.output / name))
        s.kub('wait', '--for=condition=Ready', 'pod/client', 'pod/server-holder', '--timeout=600s', timeout=610)
        s.memtier_hash()
        for workload in ('hash', 'zset'):
            path = o.output / (workload + '.trace')
            if not path.exists(): s.write(path, c.trace(workload))
            assert path.read_text() == c.trace(workload)
            s.kub('cp', str(path), 'client:/results/' + path.name, '-c', 'memtier', retry_safe=True)
        results = []
        for cpu in o.cpus:
            for workload in WORKLOADS:
                assert time.monotonic() < o.deadline, 'bounded run deadline'
                complete = o.output / f'c{cpu}-after-{workload}' / 'complete.json'
                if o.resume and complete.exists():
                    results.append(json.loads(complete.read_text()))
                    print('RETAIN', complete.parent.name, flush=True)
                else:
                    results.append(cell(o, cpu, workload))
        s.write(o.output / 'summary.json', results)
    finally:
        result = s.run(s.K + ['delete', '-f', str(o.output / 'resources.json'), '--ignore-not-found', '--wait=false'], check=False)
        cleanup = o.output / 'cleanup-request.json'
        if cleanup.exists(): cleanup = o.output / f'cleanup-request-{time.time_ns()}.json'
        s.write(cleanup, dict(exit=result.returncode, stdout=result.stdout, stderr=result.stderr, time=time.time()))
        print('CLEANUP', result.returncode, flush=True)


if __name__ == '__main__': main()
