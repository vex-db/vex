#!/usr/bin/env python3
"""Bounded follow-up on an existing run's unchanged hosts; always request cleanup."""
import argparse
import datetime
import hashlib
import json
import pathlib
import shutil
import time
import types

import sweep_request_scaling_aws as r
b, s, g = r.b, r.s, r.g


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--existing-run', type=pathlib.Path, required=True)
    p.add_argument('--output', type=pathlib.Path, required=True)
    a = p.parse_args(); source = a.existing_run.resolve(); out = a.output.resolve()
    resources = json.loads((source / 'resources.json').read_text())
    ns = resources['items'][0]['metadata']['name']
    assert ns.startswith('vex-scaling-request-')
    assert json.loads(s.run(['aws', 'sts', 'get-caller-identity']).stdout)['Account'] == '208168340597'
    s.BASE, s.NS, s.K = out, ns, ['kubectl', '--context', s.CTX, '-n', ns]
    original = json.loads((source / 'nodes.json').read_text())
    live = json.loads(s.kub('get', 'nodes', '-l', 'bench.vex.dev/run=' + ns, '-o', 'json'))
    identity = lambda data: {(n['metadata']['uid'], n['spec']['providerID'], n['status']['nodeInfo']['bootID']) for n in data['items']}
    assert len(live['items']) == 2 and identity(live) == identity(original)
    assert all(not n['metadata'].get('deletionTimestamp') for n in live['items'])
    born = min(datetime.datetime.fromisoformat(n['metadata']['creationTimestamp'].replace('Z', '+00:00')).timestamp() for n in live['items'])
    stop = born + 5.75 * 3600  # Leave 15 minutes before the existing six-hour expiry.
    assert stop - time.time() > 1200, 'insufficient time for controlled comparison'
    protocol = json.loads((source / 'protocol.json').read_text())
    b.BUILD = source / 'frozen'
    assert hashlib.sha256((b.BUILD / 'static-after/bin/vex').read_bytes()).hexdigest() == protocol['binary_sha256']
    out.mkdir(parents=True)
    s.write(out / 'nodes.json', live)
    s.write(out / 'protocol.json', dict(parent_run=str(source), binary_sha256=protocol['binary_sha256'],
        reason='Credential pause consumed original controller window; bounded follow-up within original node expiry.',
        stop_epoch=stop, cpu=16, client_threads=32, configurations=[[32, 1], [32, 16], [512, 1]],
        repetitions=3, seconds=60, order='rotated each repetition', workload=protocol['get'],
        interpretation='Equal maximum outstanding requests does not equal identical batching or latency semantics.'))
    shutil.copy2(__file__, out / 'runner.py')
    shutil.copy2(pathlib.Path(g.__file__), out / 'getset-helper.py')
    old = json.loads((b.SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    template = json.loads((b.SOURCE / 'server-candidate-16.json').read_text().replace(old, ns))
    template['spec']['containers'][0]['image'] = template['spec']['containers'][1]['image']
    template['spec']['activeDeadlineSeconds'] = 21600
    o = types.SimpleNamespace(output=out, namespace=ns, zone=protocol['zone'], template=template,
                              deadline=time.monotonic()+stop-time.time(), refine=True, client_threads=32, pipeline=1)
    try:
        s.kub('delete', 'pod', 'client', '--wait=true', '--timeout=90s')
        s.kub('create', '-f', str(source / 'client-48.json'))
        s.kub('wait', '--for=condition=Ready', 'pod/client', 'pod/server-holder', '--timeout=300s', timeout=310)
        s.memtier_hash()
        cell = out / 'connection-diagnostic'; cell.mkdir()
        host, engine, topology, instances = b.start(o, 16, 'after', cell, partitions=256)
        before = b.ena(cell, 'before', instances)
        g.phase(o, cell, host, engine, 'get', 1, 0, 'preload', preload=True)
        g.phase(o, cell, host, engine, 'get', 1, 10, 'warmup')
        rows = []; configs = [(1, 1), (1, 16), (16, 1)]
        for rep in range(3):
            for clients, pipeline in configs[rep:] + configs[:rep]:
                assert time.monotonic() + 120 < o.deadline
                o.pipeline = pipeline
                rows.append(g.phase(o, cell, host, engine, 'get', clients, 60,
                                    f'c{clients*32}-p{pipeline}-r{rep+1}'))
        s.write(cell / 'verify.json', s.sampled_values(host))
        s.assert_final_topology(topology, engine)
        after = b.ena(cell, 'after', instances)
        deltas = {role: {k: after[role][k]-v for k,v in values.items()} for role,values in before.items()}
        s.write(cell / 'complete.json', dict(rows=rows, network_deltas=deltas,
            network_valid=all(v == 0 for values in deltas.values() for v in values.values())))
        print('COMPLETE connection diagnostic', flush=True)
        # Continue the original pipeline-one sweep only while a full cell fits.
        o.output = source; o.pipeline = 1; del o.client_threads
        for workload in r.WORKLOADS:
            if o.deadline-time.monotonic() < 900: break
            path = source / f'c8-after-{workload}'
            if (path / 'complete.json').exists(): continue
            if path.exists():
                dest = source / 'interrupted'; dest.mkdir(exist_ok=True)
                path.rename(dest / (path.name + '-' + str(time.time_ns())))
            if workload != 'get':
                s.kub('cp', str(source / (workload + '.trace')), 'client:/results/' + workload + '.trace', '-c', 'memtier', retry_safe=True)
            r.cell(o, 8, workload)
    finally:
        result = s.run(s.K + ['delete', '-f', str(source / 'resources.json'), '--ignore-not-found', '--wait=false'], check=False)
        s.write(out / 'cleanup-request.json', dict(exit=result.returncode, stdout=result.stdout, stderr=result.stderr, time=time.time()))
        print('CLEANUP', result.returncode, flush=True)


if __name__ == '__main__': main()
