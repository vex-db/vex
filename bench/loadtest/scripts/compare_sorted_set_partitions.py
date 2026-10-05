#!/usr/bin/env python3
"""Fixed-load baseline/candidate/Dragonfly comparison on one AWS host pair."""
import argparse
import hashlib
import json
import pathlib
import compare_sorted_set_scaling as b
s = b.s


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--build', type=pathlib.Path, required=True)
    o = p.parse_args()
    o.output = o.output.resolve()
    b.BUILD = o.build.resolve()
    o.three_engines, o.cpus, o.zone = True, [8, 16], 'ap-south-1b'
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws', 'sts', 'get-caller-identity', '--output', 'json']).stdout)['Account'] == '208168340597'
    s.BASE, s.NS = o.output, o.namespace
    s.K = ['kubectl', '--context', s.CTX, '-n', o.namespace]
    for kind, name in [('namespace', o.namespace), ('nodepool', o.namespace+'-server'), ('nodepool', o.namespace+'-client')]:
        assert not s.kub('get', kind, name, '--ignore-not-found', '-o', 'name').strip()
    b.prepare(o)
    protocol = json.loads((o.output/'protocol.json').read_text())
    (o.output/'protocol.json').rename(o.output/'template-protocol.json')
    protocol.update(engines=['before', 'after', 'dragonfly'],
        baseline='Vex 64 sorted-set partitions', candidate='Vex 256 sorted-set partitions; all other source identical',
        arrival_model='uncapped closed-loop, identical 128 connections and pipeline 16 for every engine',
        search=None, confirmation='10-second warmup, then three 60-second repetitions; fixed settings, not maximum capacity',
        pure_update_control=None, mixed_pressure_control=None,
        order={'8': ['before', 'after', 'dragonfly'], '16': ['dragonfly', 'after', 'before']})
    protocol['binaries']['before'] = hashlib.sha256((b.BUILD/'static-before/bin/vex').read_bytes()).hexdigest()
    protocol['binaries'].pop('redis')
    b.save(o.output/'protocol.json', protocol)
    b.save(o.output/'partition-runner.py', pathlib.Path(__file__).read_text())
    try:
        for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
            s.kub('create', '-f', str(o.output/name))
        s.kub('wait', '--for=condition=Ready', 'pod/client', '--timeout=600s', timeout=610)
        assert s.memtier_hash() == s.MEMTIER_SHA256
        path = o.output/'mixed.trace'
        b.save(path, b.trace(mixed=True))
        s.kub('cp', str(path), 'client:/results/mixed.trace', '-c', 'memtier')
        for cpu in o.cpus:
            for version in protocol['order'][str(cpu)]:
                out = o.output/f'c{cpu}-{version}'
                out.mkdir()
                host, engine, topology, instances = b.start(o, cpu, version, out)
                result = b.remote_job(['python3', '-c', b.ZSET_CHECK, host, 'preload'], '/results/preload-'+out.name, 600)
                b.save(out/'preload.json', result.stdout)
                assert result.returncode == 0, result.stderr
                before = b.ena(out, 'before', instances)
                rows = [b.phase(o, out, host, engine, topology, 'mixed', 'warmup', 10, 4, 16)]
                for repeat in (1, 2, 3):
                    rows.append(b.phase(o, out, host, engine, topology, 'mixed', f'confirm-r{repeat}', 60, 4, 16))
                after = b.ena(out, 'after', instances)
                deltas = {role: {k: after[role][k]-v for k, v in counters.items()} for role, counters in before.items()}
                result = b.remote_job(['python3', '-c', b.ZSET_CHECK, host, 'verify', str(sum(r['writes'] for r in rows))], '/results/verify-'+out.name, 600)
                b.save(out/'verify.json', result.stdout)
                assert result.returncode == 0, result.stderr
                b.save(out/'complete.json', dict(cpu=cpu, version=version, rows=rows, network_deltas=deltas,
                    valid=all(r['client_valid'] for r in rows) and all(v == 0 for counters in deltas.values() for v in counters.values()),
                    pass_5ms=all(r['pass_5ms'] for r in rows[1:])))
                print('COMPLETE', out.name, flush=True)
    finally:
        result = s.run(s.K+['delete', '-f', str(o.output/'resources.json'), '--ignore-not-found', '--wait=false'], check=False)
        b.save(o.output/'cleanup-request.log', result.stdout+result.stderr)
        print('CLEANUP', result.returncode, flush=True)


if __name__ == '__main__':
    main()
