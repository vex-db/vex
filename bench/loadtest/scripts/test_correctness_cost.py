"""Offline contract check for the paired workload generator."""
import collections
import hashlib
import json
import pathlib
import shlex
import tempfile
import types
from unittest.mock import patch

import compare_correctness_cost_aws as c
from summarize_correctness_cost import metrics


def test_traces():
    for workload, expected in [('incr', {'INCR'}), ('hash', {'HSET', 'HGET'}),
                               ('list', {'LSET', 'LINDEX'}), ('set-member', {'SISMEMBER'}),
                               ('zset', {'ZINCRBY', 'ZRANK'})]:
        trace = c.trace(workload)
        assert trace == c.trace(workload)
        commands = [shlex.split(line.split('] ', 1)[1]) for line in trace.splitlines()]
        counts = collections.Counter(command[0] for command in commands)
        assert len(commands) == 32768 and set(counts) == expected
        keys = {command[1] for command in commands}
        if workload == 'zset':
            assert keys == {f'lb:{i}' for i in range(64)}
        else:
            assert keys <= {f'cost:{i}' for i in range(4096)} and len(keys) > 4055
        if len(expected) == 2:
            assert sorted(counts.values()) == [6554, 26214]
        for command in commands:
            if command[0] in ('HSET', 'LSET'):
                assert command[-1] == 'v' * 256
    compile(c.CHECK, 'remote-check', 'exec')


def test_summary_gates():
    row = dict(client_valid=True, pass_5ms=True, ops_per_sec=100, p99_ms=1,
               cpu_us_per_op=2, rss_mib=3)
    cell = dict(network_valid=True, confirmed=[dict(row) for _ in range(3)])
    assert metrics(cell, 'confirmed')['ops_per_sec'] == 100
    cell['confirmed'][1]['pass_5ms'] = False
    assert metrics(cell, 'confirmed') is None
    cell['confirmed'][1]['pass_5ms'] = True
    cell['network_valid'] = False
    assert metrics(cell, 'confirmed') is None


def test_resume_rejects_replaced_host_before_mutation():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        for version in ('before', 'after'):
            binary = root / f'frozen/static-{version}/bin/vex'
            binary.parent.mkdir(parents=True); binary.write_bytes(version.encode())
        protocol = dict(cpus=[4], zone='test', max_runtime_seconds=18000,
                        binaries={v: hashlib.sha256(v.encode()).hexdigest() for v in ('before', 'after')})
        (root / 'protocol.json').write_text(json.dumps(protocol))
        (root / 'resources.json').write_text(json.dumps(dict(items=[dict(kind='Namespace', metadata=dict(name='vex-scaling-test'))])))
        nodes = dict(items=[dict(metadata=dict(uid=str(i)), spec=dict(providerID=str(i)),
                                status=dict(nodeInfo=dict(bootID=str(i)))) for i in range(2)])
        (root / 'nodes.json').write_text(json.dumps(nodes))
        nodes['items'][0]['status']['nodeInfo']['bootID'] = 'replaced'
        def kub(*args):
            assert args[0] == 'get', 'resume mutated resources before validating hosts'
            if args[1] == 'nodes': return json.dumps(nodes)
            return json.dumps(dict(metadata=dict(labels={'bench.vex.dev/run': 'vex-scaling-test'})))
        options = types.SimpleNamespace(output=root, cpus=[4], zone='test', namespace='vex-scaling-test',
                                        before=root / 'frozen/static-before/bin/vex', after=root / 'frozen/static-after/bin/vex')
        with patch.object(c.s, 'kub', kub), patch.object(c.b, 'BUILD', c.b.BUILD):
            try:
                c.resume(options)
            except AssertionError as error:
                assert 'hosts changed' in str(error), error
            else:
                raise AssertionError('resume accepted a replaced host')


if __name__ == '__main__':
    test_traces()
    test_summary_gates()
    test_resume_rejects_replaced_host_before_mutation()
    print('Paired workload contracts passed')
