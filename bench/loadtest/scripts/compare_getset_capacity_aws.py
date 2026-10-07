#!/usr/bin/env python3
"""Matched GET/SET concurrency sweeps; reuse isolated AWS lifecycle and validators."""
import argparse
import hashlib
import json
import pathlib
import shutil
import statistics
import time

import compare_sorted_set_scaling as b
s = b.s
CONCURRENCY = (1, 4, 16, 64, 120)  # Per thread; 32 threads, pipeline 1.
REFINED_CONCURRENCY = (1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 120)


def command(host, remote, workload, clients, seconds, preload=False, threads=32, pipeline=1):
    args = s.memtier_args(host, seconds, preload)
    args[args.index('--json-out-file') + 1] = remote + '/result.json'
    args[args.index('--print-percentiles') + 1] = '50,95,99,99.9'
    if not preload:
        args[args.index('--pipeline') + 1] = str(pipeline)
        args[args.index('-t') + 1] = str(threads)
        args[args.index('--ratio') + 1] = '0:1' if workload == 'get' else '1:0'
        args[args.index('-c') + 1] = str(clients)
    return args


def passing(row):
    return row['client_valid'] and row['p99_ms'] <= 5


def phase(o, out, host, engine, workload, clients, seconds, label, preload=False):
    remote = '/results/' + out.name + '/' + label
    s.ex('client', 'memtier', 'mkdir', '-p', remote)
    # Grid units stay at 32 connections; split larger refined loads over more threads.
    threads = getattr(o, 'client_threads', None) or (64 if getattr(o, 'refine', False) and clients > 1 else 32)
    pipeline = getattr(o, 'pipeline', 1)
    actual_clients = clients * 32 // threads
    assert clients * 32 % threads == 0 and pipeline > 0
    args = command(host, remote, workload, actual_clients, seconds, preload, threads, pipeline)
    if not preload:
        before = s.snapshot_server(engine['process_name']); cb = b.client_snapshot()
    result = b.remote_job(args, remote + '/job', seconds + 300)
    s.write(out / (label + '.command.json'), args)
    s.write(out / (label + '.stderr'), result.stderr)
    s.write(out / (label + '.stdout'), result.stdout)
    assert result.returncode == 0, result.stderr
    raw = json.loads(s.ex('client', 'memtier', 'cat', remote + '/result.json'))
    s.write(out / (label + '.raw.json'), raw)
    if preload:
        s.validate_preload(raw, result.stderr, host)
        return
    try:
        assert not any(x in result.stderr.lower() for x in ('connection dropped', 'connection reset', 'thread restart')), 'connection failure during measurement'
        row = b.validate_result(raw, result.stderr, seconds, 0, False)
    except AssertionError as exc:
        row = dict(label=label, clients_per_thread=actual_clients, client_threads=threads,
                   search_clients=clients, connections=32 * clients,
                   measurement_valid=False, client_valid=False, pass_5ms=False, p99_ms=None,
                   rejection=str(exc))
        s.write(out / (label + '.summary.json'), row)
        print(out.name, label, 'REJECTED', str(exc), flush=True)
        return row
    expected = dict(server=host, port=6379, threads=threads, clients=actual_clients, pipeline=pipeline,
        ratio='0:1' if workload == 'get' else '1:0', data_size=256,
        key_prefix='vex:key::', key_minimum=1000000, key_maximum=1999999,
        key_pattern='R:R', rate_limit=0)
    assert all(raw['configuration'].get(k) == v for k, v in expected.items()), raw['configuration']
    stats = raw['ALL STATS']; count = stats['Totals']['Count']
    assert stats['Gets' if workload == 'get' else 'Sets']['Count'] == count
    after = s.snapshot_server(engine['process_name']); ca = b.client_snapshot()
    assert before['boot'] == after['boot'] and cb['boot'] == ca['boot']
    assert all(before['process'][k] == after['process'][k] for k in ('pid', 'start'))
    s.assert_memory(before, after); s.assert_memory(cb, ca)
    for name, value in [('server-before', before), ('server-after', after), ('client-before', cb), ('client-after', ca)]:
        s.write(out / (label + '.' + name + '.json'), value)
    throttle = (ca['cgroup']['cpu']['throttled_usec'] - cb['cgroup']['cpu']['throttled_usec']) / 1e6
    row.update(label=label, clients_per_thread=actual_clients, client_threads=threads, pipeline=pipeline,
        search_clients=clients, connections=32 * clients,
        cpu_us_per_op=(after['cgroup']['cpu']['usage_usec'] - before['cgroup']['cpu']['usage_usec']) / count,
        client_throttled_seconds=throttle,
        client_valid=max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values()) < .9
                     and row['client_cpu']['cpu_cores_used'] < 38.4 and throttle < .01 * seconds)
    row['pass_5ms'] = passing(row)
    s.write(out / (label + '.summary.json'), row)
    print(out.name, label, round(row['ops_per_sec']), 'p99', row['p99_ms'], 'client', row['client_valid'], flush=True)
    return row


def prepare(o):
    o.concurrency = REFINED_CONCURRENCY if getattr(o, 'refine', False) else CONCURRENCY
    hours = 8 if getattr(o, 'refine', False) else 6
    o.output.mkdir(parents=True, exist_ok=False)
    old = json.loads((b.SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
        obj = json.loads((b.SOURCE / name).read_text().replace(old, o.namespace))
        if name == 'resources.json':
            for item in obj['items'][1:]:
                item['spec']['template']['spec']['expireAfter'] = f'{hours}h'
                for req in item['spec']['template']['spec']['requirements']:
                    if req['key'] == 'topology.kubernetes.io/zone': req['values'] = [o.zone]
                    if req['key'] == 'node.kubernetes.io/instance-type' and item['metadata']['name'].endswith('-server'):
                        req['values'] = [b.SERVER_TYPE]
        else:
            obj['spec']['activeDeadlineSeconds'] = hours * 3600
            for container in obj['spec']['containers']:
                if container.get('command') == ['sleep', '14400']: container['command'] = ['sleep', str(hours * 3600)]
        s.write(o.output / name, obj)
    o.template = json.loads((b.SOURCE / 'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    b.BUILD = o.output / 'frozen'
    dest = b.BUILD / 'static-after/bin/vex'; dest.parent.mkdir(parents=True)
    shutil.copy2(o.binary, dest)
    inventory = json.loads((b.SOURCE / 'engines.json').read_text())
    inventory['vex'] = dict(binary_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
        description='Frozen September 28 development build; unpinned; adaptive ownership disabled')
    inventory.pop('candidate')
    s.write(o.output / 'engines.json', inventory)
    protocol = dict(cpu_quotas=o.cpus, server=b.SERVER_TYPE, client='c7g.16xlarge', zone=o.zone,
        memory='8Gi', dataset='1,000,000 keys; 256-byte values; 16-byte keys', workloads=['get', 'set'],
        workers='Vex workers / Dragonfly proactors / Redis I/O threads equal CPU quota',
        cpu_allocation='cgroup quotas on unchanged dedicated hosts; not exclusive physical core assignments',
        persistence=False, vex_pinned=False, adaptive_owner=False, partitions=256,
        arrival_model='uncapped closed-loop, pipeline 1; service latency',
        client_threads='32 at 32 connections; 64 otherwise' if getattr(o, 'refine', False) else '32',
        search_connections=[32 * n for n in o.concurrency], search_seconds=15,
        connection_ceiling='3840; below Vex fixed 4096-entry file-descriptor table including internal FDs',
        confirmation='3 x 60 seconds at fastest passing setting; fall back to next passing setting if needed',
        primary_p99_ms=5, latency_scope='Not scheduled-arrival latency; confirmed capacities remain lower bounds',
        client_gate='max thread average <0.9 CPU, aggregate <38.4 CPUs, throttling <1% duration',
        network_gate='zero change in ENA allowance-exceeded counters per cell',
        memtier_sha256=s.MEMTIER_SHA256, engines=inventory,
        max_runtime_seconds=hours * 3600 - 1800, node_expiry_hours=hours)
    s.write(o.output / 'protocol.json', protocol)
    for path in pathlib.Path(__file__).parent.glob('*.py'):
        dest = o.output / 'source' / path.name; dest.parent.mkdir(exist_ok=True); shutil.copy2(path, dest)
    return protocol


def cell(o, cpu, name, workload):
    out = o.output / f'c{cpu}-{name}-{workload}'; out.mkdir()
    host, engine, topology, instances = b.start(o, cpu, name, out, partitions=256 if name == 'after' else None)
    phase(o, out, host, engine, workload, 1, 0, 'preload', preload=True)
    s.write(out / 'values-before.json', s.sampled_values(host))
    net_before = b.ena(out, 'before', instances)
    phase(o, out, host, engine, workload, 4, 10, 'warmup')
    trials = []
    for clients in o.concurrency:
        row = phase(o, out, host, engine, workload, clients, 15, f'search-c{clients}')
        trials.append(row)
        # Stop at the first measured SLO failure; retain lower passing settings.
        # An invalid trial is evidence of a search limit, never a capacity value.
        if row['client_valid'] and row['p99_ms'] > 5: break
        if not row['client_valid']: break
    confirmations = []; history = []
    for candidate in sorted((r for r in trials if passing(r)), key=lambda r:r['ops_per_sec'], reverse=True):
        clients = candidate['search_clients']
        group = [phase(o, out, host, engine, workload, clients, 60, f'confirm-c{clients}-r{i}') for i in (1, 2, 3)]
        history += group
        if all(passing(r) for r in group):
            confirmations = group; break
    s.write(out / 'values-after.json', s.sampled_values(host))
    s.postload_checks('vex' if name == 'after' else name, host, out, 'engine-after.json', io_threads=cpu)
    s.assert_final_topology(topology, engine)
    net_after = b.ena(out, 'after', instances)
    deltas = {role:{k:net_after[role][k] - v for k,v in counters.items()} for role,counters in net_before.items()}
    network_valid = all(v == 0 for counters in deltas.values() for v in counters.values())
    result = dict(cpu=cpu, engine='vex' if name == 'after' else name, workload=workload,
        trials=trials, confirmations=confirmations, confirmation_history=history, network_deltas=deltas,
        network_valid=network_valid, valid=bool(confirmations) and network_valid,
        confirmed_ops_per_sec=statistics.median(r['ops_per_sec'] for r in confirmations) if confirmations and network_valid else None,
        classification='invalid search trial; lower bound' if any(r.get('measurement_valid') is False for r in trials) else
            'client-limited lower bound' if any(not r['client_valid'] for r in trials) else
            'SLO boundary observed' if any(r['p99_ms'] > 5 for r in trials) else 'search ceiling; lower bound only')
    s.write(out / 'complete.json', result)
    print('COMPLETE', out.name, result['confirmed_ops_per_sec'], result['classification'], flush=True)
    return result


def resume(o):
    """Retain completed cells only when the original matched hosts survived."""
    protocol = json.loads((o.output / 'protocol.json').read_text())
    assert protocol['cpu_quotas'] == o.cpus and protocol['zone'] == o.zone
    o.concurrency = REFINED_CONCURRENCY if getattr(o, 'refine', False) else CONCURRENCY
    assert protocol['search_connections'] == [32 * n for n in o.concurrency], 'resume must use the original search grid'
    resources = json.loads((o.output / 'resources.json').read_text())
    assert resources['items'][0]['metadata']['name'] == o.namespace
    b.BUILD = o.output / 'frozen'
    assert hashlib.sha256((b.BUILD / 'static-after/bin/vex').read_bytes()).hexdigest() == protocol['engines']['vex']['binary_sha256']
    initial = json.loads((o.output / 'nodes.json').read_text())
    current = json.loads(s.kub('get', 'nodes', '-l', 'bench.vex.dev/run=' + o.namespace, '-o', 'json'))
    identity = lambda ns: {(n['metadata']['uid'], n['status']['nodeInfo']['bootID'], n['spec']['providerID']) for n in ns['items']}
    assert len(current['items']) == 2 and identity(current) == identity(initial), 'host changed; a fresh matched run is required'
    for item in resources['items']:
        live = json.loads(s.kub('get', item['kind'], item['metadata']['name'], '-o', 'json'))
        assert live['metadata']['labels']['bench.vex.dev/run'] == o.namespace
        assert not live['metadata'].get('deletionTimestamp'), 'cleanup already underway'
    old = json.loads((b.SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    o.template = json.loads((b.SOURCE / 'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    stamp = str(time.time_ns())
    s.write(o.output / ('resume-' + stamp + '.json'), dict(nodes=current, reason='credential interruption; completed cells retained, incomplete cell restarted'))
    # A fresh client ensures detached jobs cannot overlap resumed measurements.
    s.kub('delete', 'pod', 'client', '--wait=true', '--timeout=90s')
    s.kub('create', '-f', str(o.output / 'client-48.json'))
    for path in o.output.glob('c*-*'):
        if path.is_dir() and not (path / 'complete.json').exists():
            archived = o.output / 'interrupted'; archived.mkdir(exist_ok=True)
            path.rename(archived / (path.name + '-' + stamp))
    for path in pathlib.Path(__file__).parent.glob('*.py'):
        dest = o.output / ('resume-source-' + stamp) / path.name
        dest.parent.mkdir(exist_ok=True); shutil.copy2(path, dest)
    return protocol


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--binary', type=pathlib.Path, required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--zone', default='ap-south-1b')
    p.add_argument('--cpus', type=int, nargs='+', default=[1, 2, 4, 8, 16])
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--refine', action='store_true', help='Use finer concurrency steps with the same 5 ms p99 limit')
    o = p.parse_args(); o.output = o.output.resolve()
    assert o.namespace.startswith('vex-scaling-') and set(o.cpus) <= {1, 2, 4, 8, 16}
    s.BASE = o.output; s.NS = o.namespace; s.K = ['kubectl', '--context', s.CTX, '-n', o.namespace]
    assert not (o.prepare_only and o.resume)
    if not o.prepare_only:
        assert json.loads(s.run(['aws', 'sts', 'get-caller-identity']).stdout)['Account'] == '208168340597'
        if not o.resume:
            for kind, name in [('namespace', o.namespace), ('nodepool', o.namespace + '-server'), ('nodepool', o.namespace + '-client')]:
                assert not s.kub('get', kind, name, '--ignore-not-found', '-o', 'name').strip()
    protocol = resume(o) if o.resume else prepare(o)
    if o.prepare_only: return
    started = time.monotonic() - (time.time() - (o.output / 'protocol.json').stat().st_mtime)
    try:
        if not o.resume:
            for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
                s.kub('create', '-f', str(o.output / name))
        s.kub('wait', '--for=condition=Ready', 'pod/client', 'pod/server-holder', '--timeout=600s', timeout=610)
        s.memtier_hash()
        rows = []
        for index, cpu in enumerate(o.cpus):
            names = ['after', 'redis', 'dragonfly']; shift = index % 3
            for name in names[shift:] + names[:shift]:
                for workload in ('get', 'set'):
                    assert time.monotonic() - started < protocol['max_runtime_seconds'], 'bounded run deadline'
                    complete = o.output / f'c{cpu}-{name}-{workload}' / 'complete.json'
                    if o.resume and complete.exists():
                        print('RETAIN', complete.parent.name, flush=True)
                        rows.append(json.loads(complete.read_text()))
                    else:
                        rows.append(cell(o, cpu, name, workload))
        s.write(o.output / 'summary.json', rows)
    finally:
        result = s.run(s.K + ['delete', '-f', str(o.output / 'resources.json'), '--ignore-not-found', '--wait=false'], check=False)
        cleanup = o.output / 'cleanup-request.json'
        if cleanup.exists(): cleanup = o.output / ('cleanup-request-' + str(time.time_ns()) + '.json')
        s.write(cleanup, dict(exit=result.returncode, stdout=result.stdout, stderr=result.stderr, time=time.time()))
        print('CLEANUP', result.returncode, flush=True)


if __name__ == '__main__': main()
