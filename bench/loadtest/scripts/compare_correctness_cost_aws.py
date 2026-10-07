#!/usr/bin/env python3
"""Compare the frozen integer fix with the key-lifecycle fix on isolated AWS hosts."""
import argparse
import hashlib
import json
import pathlib
import random
import shutil
import time

import compare_sorted_set_scaling as b
import compare_getset_capacity_aws as g

s = b.s
WORKLOADS = ('get', 'set', 'incr', 'hash', 'list', 'set-member', 'zset')
CONNECTIONS = (32, 64, 128, 256, 512, 1024)


def connection_grid(workload):
    return (1, 4, 16) + CONNECTIONS if workload in ('hash', 'list', 'set-member') else CONNECTIONS


def trace(workload):
    if workload == 'zset':
        return b.trace(mixed=True)
    rng = random.Random(20260930)
    lines = []
    for i in range(32768):
        key, member = rng.randrange(4096), rng.randrange(16)
        key = f'cost:{key}'
        if workload == 'incr': parts = ['INCR', key]
        elif workload == 'hash': parts = ['HGET', key, f'm:{member}'] if i % 5 == 0 else ['HSET', key, f'm:{member}', 'v' * 256]
        elif workload == 'list': parts = ['LINDEX', key, str(member)] if i % 5 == 0 else ['LSET', key, str(member), 'v' * 256]
        elif workload == 'set-member': parts = ['SISMEMBER', key, f'm:{member}']
        else: raise ValueError(workload)
        lines.append(f'{i + 1}.000000 [0 127.0.0.1:12345] ' + ' '.join(json.dumps(x) for x in parts) + '\n')
    return ''.join(lines)


# Shared RESP parser and exact-data verification; no TYPE/DBSIZE dependency on
# the known-incorrect baseline. Preload and verification are outside measurement.
CHECK = b.ZSET_CHECK.split("if mode=='preload':")[0] + r'''
workload=sys.argv[3]
if mode=='preload':
 for k in range(4096):
  key='cost:'+str(k)
  if workload=='incr': assert ask(['SET',key,'0'])=='OK'
  elif workload=='hash': assert int(ask(['HSET',key]+[x for m in range(16) for x in ('m:'+str(m),'v'*256)]))==16
  elif workload=='list': assert int(ask(['RPUSH',key]+['v'*256]*16))==16
  else: assert int(ask(['SADD',key]+['m:'+str(m) for m in range(16)]))==16
total=0
for k in range(4096):
 key='cost:'+str(k)
 if workload=='incr': total+=int(ask(['GET',key]))
 elif workload=='hash':
  assert int(ask(['HLEN',key]))==16
  assert ask(['HMGET',key]+['m:'+str(m) for m in range(16)])==['v'*256]*16
 elif workload=='list': assert ask(['LRANGE',key,'0','-1'])==['v'*256]*16
 else: assert set(ask(['SMEMBERS',key]))=={'m:'+str(m) for m in range(16)}
if workload=='incr': assert total==(0 if mode=='preload' else int(sys.argv[4])),total
print(json.dumps(dict(keys=4096,workload=workload,increment_sum=total,verified=True)))
'''


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
    hashes = {}
    for version in ('before', 'after'):
        dest = b.BUILD / f'static-{version}/bin/vex'
        dest.parent.mkdir(parents=True)
        shutil.copy2(getattr(o, version), dest)
        hashes[version] = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert hashes['before'] == '2299b3ea28e9642640ff364dbf30dac3523700f46e5e5def96790b7cad258618'
    assert hashes['before'] != hashes['after']
    s.write(o.output / 'protocol.json', dict(
        baseline='September 29 integer-fix ReleaseFast; known key-type/lifecycle bugs',
        candidate='September 30 key-lifecycle fix; ReleaseFast aarch64-linux-musl',
        binaries=hashes, cpus=o.cpus, workloads=WORKLOADS, server=b.SERVER_TYPE,
        client='c7g.16xlarge', client_cpu_quota=48, server_memory='8Gi', zone=o.zone,
        workers='equal CPU quota; unpinned; adaptive disabled; 256 partitions',
        arrival_model='closed-loop, uncapped; pipeline 1; service latency',
        connections={w: connection_grid(w) for w in WORKLOADS}, search_seconds=15, confirmation='3 x 60s at fastest passing setting; fallback if needed',
        fixed_load_control='3 x 30s, 128 connections, pipeline 1; no SLO qualification',
        slo_p99_ms=5, strings='1 million 16-byte keys, 256-byte values, uniform GET or SET',
        incr='4096 independent numeric keys, uniform replay; exact increment conservation',
        hash='4096 hashes x 16 fields x 256B; 80% HSET / 20% HGET existing fields',
        list='4096 lists x 16 values x 256B; 80% LSET / 20% LINDEX existing positions',
        set_member='4096 sets x 16 members; 100% SISMEMBER existing members (read-only)',
        zset='64 sets x 4096 members; 80% ZINCRBY / 20% ZRANK',
        ttl='none; does not measure active expiry or metadata overhead on expiring keys',
        client_gate='thread <0.9 CPU, total <38.4 CPU, throttling <1% duration',
        cost='relative server cost/operation = before capacity / after capacity; same hourly host price; not a billing forecast',
        max_runtime_seconds=18000))
    for path in pathlib.Path(__file__).parent.glob('*.py'):
        dest = o.output / 'source' / path.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(path, dest)


def phase(o, out, host, engine, workload, connections, seconds, label):
    if workload in ('get', 'set'):
        row = g.phase(o, out, host, engine, workload, connections // 32, seconds, label)
        # The shared GET/SET helper already records all snapshots.
        after = json.loads((out / (label + '.server-after.json')).read_text()) if row.get('measurement_valid', True) else None
        if after: row['rss_mib'] = after['process']['rss_bytes'] / 2**20
        return row
    remote = '/results/' + out.name + '/' + label
    s.ex('client', 'memtier', 'mkdir', '-p', remote)
    threads = min(connections, 32 if connections <= 32 else 64)
    command = ['memtier_benchmark', '-s', host, '-p', '6379', '-t', str(threads), '-c', str(connections // threads),
        '--pipeline', '1', '--test-time', str(seconds), '--distinct-client-seed', '--hide-histogram',
        '--print-percentiles', '50,95,99,99.9', '--monitor-input', '/results/' + workload + '.trace',
        '--command', '__monitor_line@__', '--monitor-pattern', 'R', '--json-out-file', remote + '/result.json']
    before = s.snapshot_server('vex'); cb = b.client_snapshot()
    result = b.remote_job(command, remote + '/job', seconds + 180)
    s.write(out / (label + '.command.json'), command)
    s.write(out / (label + '.stdout'), result.stdout); s.write(out / (label + '.stderr'), result.stderr)
    assert result.returncode == 0, result.stderr
    raw = json.loads(s.ex('client', 'memtier', 'cat', remote + '/result.json'))
    s.write(out / (label + '.raw.json'), raw)
    assert not any(x in result.stderr.lower() for x in ('connection dropped', 'connection reset', 'thread restart')), 'connection failure'
    row = b.validate_result(raw, result.stderr, seconds, 0, False)
    cfg = raw['configuration']
    assert all(cfg.get(k) == v for k, v in dict(server=host, threads=threads, clients=connections // threads, pipeline=1, rate_limit=0).items()), cfg
    after = s.snapshot_server('vex'); ca = b.client_snapshot()
    assert before['boot'] == after['boot'] and cb['boot'] == ca['boot']
    assert all(before['process'][k] == after['process'][k] for k in ('pid', 'start'))
    s.assert_memory(before, after); s.assert_memory(cb, ca)
    for name, value in [('server-before', before), ('server-after', after), ('client-before', cb), ('client-after', ca)]:
        s.write(out / (label + '.' + name + '.json'), value)
    stats = raw['ALL STATS']; count = stats['Totals']['Count']
    commands = {k.lower().rstrip('s'): v['Count'] for k, v in stats.items() if isinstance(v, dict) and 'Count' in v and k != 'Totals'}
    allowed = {'incr': {'incr'}, 'hash': {'hset', 'hget'}, 'list': {'lset', 'lindex'}, 'set-member': {'sismember'}, 'zset': {'zincrby', 'zrank'}}[workload]
    assert sum(commands.get(k, 0) for k in allowed) == count, commands
    if workload in ('hash', 'list', 'zset'):
        read = {'hash': 'hget', 'list': 'lindex', 'zset': 'zrank'}[workload]
        assert .19 < commands[read] / count < .21
    throttle = (ca['cgroup']['cpu']['throttled_usec'] - cb['cgroup']['cpu']['throttled_usec']) / 1e6
    row.update(label=label, connections=connections, operations=count, writes=commands.get('zincrby', 0),
        cpu_us_per_op=(after['cgroup']['cpu']['usage_usec'] - before['cgroup']['cpu']['usage_usec']) / count,
        rss_mib=after['process']['rss_bytes'] / 2**20, client_throttled_seconds=throttle,
        client_valid=max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values()) < .9 and row['client_cpu']['cpu_cores_used'] < 38.4 and throttle < .01 * seconds)
    row['pass_5ms'] = g.passing(row)
    s.write(out / (label + '.summary.json'), row)
    print(out.name, label, round(row['ops_per_sec']), 'p99', row['p99_ms'], 'client', row['client_valid'], flush=True)
    return row


def cell(o, cpu, version, workload):
    out = o.output / f'c{cpu}-{version}-{workload}'; out.mkdir()
    host, engine, topology, instances = b.start(o, cpu, version, out, partitions=256)
    net_before = b.ena(out, 'before', instances)
    if workload in ('get', 'set'):
        g.phase(o, out, host, engine, workload, 1, 0, 'preload', preload=True)
    else:
        command = ['python3', '-c', b.ZSET_CHECK if workload == 'zset' else CHECK, host, 'preload']
        if workload != 'zset': command.append(workload)
        result = b.remote_job(command, '/results/preload-' + out.name, 600)
        s.write(out / 'preload.json', result.stdout); assert result.returncode == 0, result.stderr
    rows = [phase(o, out, host, engine, workload, 128, 10, 'warmup')]
    trials = []
    for connections in connection_grid(workload):
        row = phase(o, out, host, engine, workload, connections, 15, f'search-c{connections}')
        rows.append(row); trials.append(row)
        if not row['client_valid'] or row['p99_ms'] > 5: break
    confirmed = []
    for candidate in sorted((r for r in trials if r['pass_5ms']), key=lambda r: r['ops_per_sec'], reverse=True):
        connections = candidate['connections']
        group = [phase(o, out, host, engine, workload, connections, 60, f'confirm-c{connections}-r{r}') for r in (1, 2, 3)]
        rows += group
        if all(r['pass_5ms'] for r in group): confirmed = group; break
    control = [phase(o, out, host, engine, workload, 128, 30, f'control-r{r}') for r in (1, 2, 3)]
    rows += control
    if workload in ('get', 'set'):
        s.write(out / 'verify.json', s.sampled_values(host))
    else:
        total = sum(r['writes'] if workload == 'zset' else r['operations'] for r in rows)
        command = ['python3', '-c', b.ZSET_CHECK if workload == 'zset' else CHECK, host, 'verify']
        if workload != 'zset': command.append(workload)
        result = b.remote_job(command + [str(total)], '/results/verify-' + out.name, 600)
        s.write(out / 'verify.json', result.stdout); assert result.returncode == 0, result.stderr
    s.assert_final_topology(topology, engine)
    net_after = b.ena(out, 'after', instances)
    deltas = {role: {k: net_after[role][k] - v for k, v in values.items()} for role, values in net_before.items()}
    result = dict(cpu=cpu, version=version, workload=workload, confirmed=confirmed, control=control,
        network_deltas=deltas, network_valid=all(v == 0 for values in deltas.values() for v in values.values()))
    s.write(out / 'complete.json', result)
    print('COMPLETE', out.name, flush=True)
    return result


def resume(o):
    """Resume an auth interruption only while the original hosts remain intact."""
    protocol = json.loads((o.output / 'protocol.json').read_text())
    assert protocol['cpus'] == o.cpus and protocol['zone'] == o.zone
    assert time.time() - (o.output / 'protocol.json').stat().st_mtime < getattr(o, 'max_runtime_seconds', protocol['max_runtime_seconds'])
    b.BUILD = o.output / 'frozen'
    for version in ('before', 'after'):
        for path in (getattr(o, version), b.BUILD / f'static-{version}/bin/vex'):
            assert hashlib.sha256(path.read_bytes()).hexdigest() == protocol['binaries'][version]
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
    s.write(o.output / f'resume-{stamp}.json', dict(nodes=nodes, reason='controlled restart; retain original hosts and binaries',
        connections={w: connection_grid(w) for w in WORKLOADS},
        max_runtime_seconds=getattr(o, 'max_runtime_seconds', protocol['max_runtime_seconds'])))
    s.write(o.output / f'runner-resume-{stamp}.py', pathlib.Path(__file__).read_text())
    # Killing the old client also stops any detached trial before starting anew.
    s.kub('delete', 'pod', 'client', '--wait=true', '--timeout=90s')
    s.kub('create', '-f', str(o.output / 'client-48.json'))
    for path in o.output.glob('c*-*'):
        if path.is_dir() and not (path / 'complete.json').exists():
            excluded = o.output / 'interrupted'; excluded.mkdir(exist_ok=True)
            path.rename(excluded / (path.name + '-' + stamp))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--before', type=pathlib.Path, required=True)
    p.add_argument('--after', type=pathlib.Path, required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--zone', default='ap-south-1b')
    p.add_argument('--cpus', nargs='+', type=int, choices=(4, 16), default=[4, 16])
    p.add_argument('--resume', action='store_true')
    p.add_argument('--max-runtime-seconds', type=int, choices=(18000, 20400), default=18000,
                   help='Controller bound from original start; extended bound leaves 20 minutes before node expiry')
    o = p.parse_args(); o.output = o.output.resolve(); o.refine = True
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws', 'sts', 'get-caller-identity']).stdout)['Account'] == '208168340597'
    s.BASE, s.NS = o.output, o.namespace; s.K = ['kubectl', '--context', s.CTX, '-n', o.namespace]
    if not o.resume:
        for kind, name in [('namespace', o.namespace), ('nodepool', o.namespace + '-server'), ('nodepool', o.namespace + '-client')]:
            assert not s.kub('get', kind, name, '--ignore-not-found', '-o', 'name').strip()
        prepare(o)
    started = time.monotonic() - (time.time() - (o.output / 'protocol.json').stat().st_mtime)
    try:
        if o.resume:
            resume(o)
        else:
            for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
                s.kub('create', '-f', str(o.output / name))
        s.kub('wait', '--for=condition=Ready', 'pod/client', 'pod/server-holder', '--timeout=600s', timeout=610)
        s.memtier_hash()
        for workload in WORKLOADS[2:]:
            path = o.output / (workload + '.trace')
            if not path.exists(): s.write(path, trace(workload))
            assert path.read_text() == trace(workload)
            s.kub('cp', str(path), 'client:/results/' + path.name, '-c', 'memtier', retry_safe=True)
        results = []
        for index, cpu in enumerate(o.cpus):
            for wi, workload in enumerate(WORKLOADS):
                for version in (('before', 'after') if (index + wi) % 2 == 0 else ('after', 'before')):
                    assert time.monotonic() - started < o.max_runtime_seconds, 'bounded run deadline'
                    complete = o.output / f'c{cpu}-{version}-{workload}' / 'complete.json'
                    if o.resume and complete.exists():
                        results.append(json.loads(complete.read_text()))
                        print('RETAIN', complete.parent.name, flush=True)
                    else:
                        results.append(cell(o, cpu, version, workload))
        s.write(o.output / 'summary.json', results)
    finally:
        result = s.run(s.K + ['delete', '-f', str(o.output / 'resources.json'), '--ignore-not-found', '--wait=false'], check=False)
        cleanup = o.output / 'cleanup-request.json'
        if cleanup.exists(): cleanup = o.output / f'cleanup-request-{time.time_ns()}.json'
        s.write(cleanup, dict(exit=result.returncode, stdout=result.stdout, stderr=result.stderr, time=time.time()))
        print('CLEANUP', result.returncode, flush=True)


if __name__ == '__main__': main()
