#!/usr/bin/env python3
"""Offline protocol/command checks; no AWS resources are created."""
import json
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import compare_getset_capacity_aws as r

for workload, ratio in [('get', '0:1'), ('set', '1:0')]:
    args = r.command('127.0.0.1', '/results/test', workload, 16, 60)
    assert args[args.index('--ratio') + 1] == ratio
    assert args[args.index('--pipeline') + 1] == '1'
    assert args[args.index('-c') + 1] == '16'
    assert '--rate-limiting' not in args
    assert args[args.index('--key-maximum') + 1] == '1999999'
assert r.passing(dict(client_valid=True, p99_ms=5))
assert not r.passing(dict(client_valid=False, p99_ms=1))
assert not r.passing(dict(client_valid=True, p99_ms=5.01))
args = r.command('127.0.0.1', '/results/test', 'set', 8, 60, threads=64)
assert args[args.index('-t') + 1] == '64' and args[args.index('-c') + 1] == '8'
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp); binary = root / 'vex'; binary.write_bytes(b'test frozen binary')
    o = SimpleNamespace(output=root/'run', binary=binary, namespace='vex-scaling-offline-test', zone='ap-south-1b', cpus=[1,2,4,8,16])
    protocol = r.prepare(o)
    resources = json.loads((o.output/'resources.json').read_text())
    assert len(resources['items']) == 3
    assert all(i['metadata']['labels']['bench.vex.dev/run'] == o.namespace for i in resources['items'])
    assert protocol['cpu_quotas'] == [1,2,4,8,16]
    assert protocol['primary_p99_ms'] == 5 and protocol['adaptive_owner'] is False
    assert (o.output/'frozen/static-after/bin/vex').read_bytes() == binary.read_bytes()
    o.output = root/'refined'; o.refine = True
    refined = r.prepare(o)
    assert refined['search_connections'] == [32,64,128,256,384,512,768,1024,1536,2048,3072,3840]
    assert set(protocol['search_connections']) < set(refined['search_connections'])
    assert refined['engines'] == protocol['engines'] and refined['primary_p99_ms'] == 5
    assert refined['max_runtime_seconds'] == 27000 and refined['node_expiry_hours'] == 8
    assert refined['client_threads'] == '32 at 32 connections; 64 otherwise'
    resources = json.loads((o.output/'resources.json').read_text())
    assert all(i['spec']['template']['spec']['expireAfter'] == '8h' for i in resources['items'][1:])
    assert json.loads((o.output/'client-48.json').read_text())['spec']['activeDeadlineSeconds'] == 28800
print('GET/SET protocol checks passed')

from summarize_getset_capacity import row
trials = [dict(client_valid=True, pass_5ms=True, p99_ms=.3, connections=32, ops_per_sec=n, cpu_us_per_op=2) for n in (100,110,120)]
cell = dict(cpu=1, engine='vex', workload='get', classification='lower bound', valid=True,
            network_valid=True, confirmations=trials, confirmed_ops_per_sec=110)
result = row(cell)
assert (result['ops_s'],result['min_ops_s'],result['max_ops_s']) == (110,100,120)
trials[2]['client_valid'] = False
try:
    row(cell)
except AssertionError:
    pass
else:
    raise AssertionError('accepted invalid confirmation')
cell['valid'] = False
assert row(cell)['ops_s'] is None
print('GET/SET export validity and repetition-range checks passed')

# A resumed run must never retain measurements from replacement hosts.
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp); binary = root/'vex'; binary.write_bytes(b'frozen')
    o = SimpleNamespace(output=root/'run', binary=binary, namespace='vex-scaling-resume-test', zone='ap-south-1b', cpus=[1])
    r.prepare(o)
    node = lambda uid: dict(metadata=dict(uid=uid), status=dict(nodeInfo=dict(bootID=uid)), spec=dict(providerID=uid))
    original = dict(items=[node('server'), node('client')])
    (o.output/'nodes.json').write_text(json.dumps(original))
    partial = o.output/'c1-after-get'; partial.mkdir()
    calls = []; replaced = dict(items=[node('replacement'), node('client')])
    kub = r.s.kub
    r.s.kub = lambda *args, **kwargs: calls.append(args) or json.dumps(replaced)
    try:
        try: r.resume(o)
        except AssertionError: pass
        else: raise AssertionError('accepted replacement benchmark host')
        assert all(args[0] == 'get' for args in calls) and partial.exists()
    finally:
        r.s.kub = kub
print('GET/SET resume rejects replacement hosts before mutation')

with patch.object(r.s, 'run', side_effect=[RuntimeError('read: operation timed out'), SimpleNamespace(stdout='copied')]) as run, patch.object(r.s.time, 'sleep'):
    assert r.s.kub('cp', 'frozen-vex', 'server:/results/vex', retry_safe=True) == 'copied'
    assert run.call_count == 2 and run.call_args_list[0] == run.call_args_list[1]
with patch.object(r.s, 'run', side_effect=RuntimeError('Unauthorized')) as run:
    try: r.s.kub('cp', 'frozen-vex', 'server:/results/vex', retry_safe=True)
    except RuntimeError: pass
    else: raise AssertionError('ignored authentication failure')
    assert run.call_count == 1
print('Safe setup copy retries transient transport errors only')

from summarize_getset_capacity import collect
with tempfile.TemporaryDirectory() as temp:
    a,b = [pathlib.Path(temp)/name for name in ('first','replacement')]
    for run,cpus in ((a,[1,8]),(b,[8])):
        run.mkdir()
        (run/'protocol.json').write_text(json.dumps(dict(cpu_quotas=cpus, primary_p99_ms=5)))
        (run/'nodes.json').write_text(json.dumps(dict(items=[dict(spec=dict(providerID=run.name))])))
    def add(run,cpu,engine,workload):
        dest = run/f'c{cpu}-{engine}-{workload}'; dest.mkdir()
        (dest/'complete.json').write_text(json.dumps(dict(cpu=cpu, engine=engine, workload=workload)))
    for run,cpu in ((a,1),(b,8)):
        for engine in ('vex','redis','dragonfly'):
            for workload in ('get','set'): add(run,cpu,engine,workload)
    add(a,8,'vex','get')
    protocol,entries,cohorts,excluded = collect([a,b])
    assert len(entries)==12 and len(excluded)==1 and protocol['cpu_quotas']==[1,8]
    assert [c['cpu_quotas'] for c in cohorts]==[[1],[8]]
    assert all(run==b for run,_,d in entries if d['cpu']==8)
    (b/'protocol.json').write_text(json.dumps(dict(cpu_quotas=[8],primary_p99_ms=10)))
    try: collect([a,b])
    except AssertionError: pass
    else: raise AssertionError('combined different latency protocols')
print('Cohort export excludes partial CPU groups and rejects protocol mismatches')
