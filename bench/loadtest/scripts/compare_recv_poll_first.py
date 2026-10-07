#!/usr/bin/env python3
"""Reproduce the rejected poll-first ABBA experiment on explicitly prepared nodes.

Requires the archived recv-poll-first-rejected.patch and its experiment binary;
the production implementation was removed after a sub-threshold result.
"""
import hashlib
import json
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE / 'vex/bench/loadtest/scripts'))
import value_size_sweep as v
import scrum_benchmark as b
from scrum_results import memory

v.NS = 'vex-optimize-20260922'
v.OUT = pathlib.Path(__file__).resolve().parents[1] / 'runs/recv-poll-first'
v.configure()
original_apply = b.apply
mode = 'off'

def apply(obj):
    if obj['kind'] == 'Pod' and obj['metadata']['name'] == 'benchmark-server':
        obj['spec']['containers'][0]['env'] = [
            {'name': 'VEX_RECV_POLL_FIRST', 'value': '1' if mode == 'on' else '0'}]
    return original_apply(obj)

b.apply = apply

def cpu(pod, container):
    return {k: int(value) for k, value in
            (line.split() for line in b.execpod(pod, container, 'cat', '/sys/fs/cgroup/cpu.stat').splitlines())}

if __name__ == '__main__':
    binary = v.OUT / 'vex-linux-amd64'
    assert binary.exists(), 'Build and validate the experiment binary first'
    b.save('settings.json', {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'order': ['off', 'on', 'on', 'off'], 'value_bytes': 256, 'keys': 1000000,
        'key_bytes': 16, 'pipeline': 1, 'connections': 128, 'warmup_seconds': 10,
        'measured_seconds': 60, 'workload': '80% GET / 20% SET',
        'cpu_counters': 'Snapshots bracket the timed memtier invocation, including tool overhead.',
        'purpose': 'Rejection/continuation gate; insufficient alone to enable by default.'})
    rows = []
    client = json.loads(b.kub('get', 'pod', b.LOADGEN, '-o', 'json'))
    b.save('client-pod.json', client)
    expected = json.loads((v.OUT.parent / 'value-size-followup/4096-published-pod.json').read_text())['spec']['nodeName']
    for index, mode in enumerate(['off', 'on', 'on', 'off']):
        stem = f'{index+1}-{mode}'
        print('START', stem, flush=True)
        b.kub('delete', 'pod', 'benchmark-server', '--ignore-not-found', '--wait=true', '--timeout=60s')
        b.start('vex', 'c6a.2xlarge', 6)
        startup_log = b.kub('logs', 'benchmark-server', '-c', 'server')
        b.save(stem+'-startup.log', startup_log)
        backend_lines = [line for line in startup_log.splitlines() if 'io_uring backend active' in line]
        expected_flag = 'recv_poll_first=' + ('true' if mode == 'on' else 'false')
        assert len(backend_lines) == 6 and all(expected_flag in line for line in backend_lines), startup_log
        current_client = json.loads(b.kub('get', 'pod', b.LOADGEN, '-o', 'json'))
        assert current_client['metadata']['uid'] == client['metadata']['uid']
        assert current_client['spec']['nodeName'] == client['spec']['nodeName']
        pod = json.loads(b.kub('get', 'pod', 'benchmark-server', '-o', 'json'))
        assert pod['spec']['nodeName'] == expected
        b.save(stem+'-pod.json', pod)
        b.save(stem+'-placement.txt', b.execpod('benchmark-server', 'server', 'sh', '-c',
            'uname -a; cat /sys/fs/cgroup/cpuset.cpus.effective; cat /proc/net/dev; cat /proc/net/snmp'))
        v.mt(stem+'-preload', 256, '1:0', preload=True)
        assert int(b.cli('DBSIZE')) == 1000000
        b.save(stem+'-wire.json', v.verify_values(256))
        loaded = v.state(stem)
        v.mt(stem+'-warmup', 256, '1:4', 10)
        before = cpu('benchmark-server', 'server')
        client_before = cpu(b.LOADGEN, 'loadgen')
        result = v.mt(stem+'-mixed', 256, '1:4', 60)
        after = cpu('benchmark-server', 'server')
        raw = json.loads((v.OUT / result['file']).read_text())
        cfg = raw['configuration']
        expected_config = {'pipeline':1, 'threads':32, 'clients':4, 'data_size':256,
            'ratio':'1:4', 'test_time':60, 'key_prefix':'vex:key::',
            'key_minimum':1000000, 'key_maximum':1999999}
        assert all(cfg[k] == value for k, value in expected_config.items()), cfg
        runtime = raw['ALL STATS']['Runtime']
        assert runtime['Time unit'] == 'MILLISECONDS'
        assert 59000 <= runtime['Total duration'] <= 65000, runtime
        client_after = cpu(b.LOADGEN, 'loadgen')
        mp = v.OUT / (stem+'-memory.jsonl')
        mp.write_text(b.execpod('benchmark-server', 'sampler', 'cat', '/results/process.jsonl'))
        result.update(memory(mp, result['start'], result['end']))
        result.update(mode=mode, order=index+1, loaded_rss_mib=loaded,
            cpu_delta={k: after[k]-before[k] for k in before},
            client_cpu_delta={k: client_after[k]-client_before[k] for k in client_before})
        b.save(stem+'-cpu-snapshots.json', {'server_before':before,'server_after':after,
            'client_before':client_before,'client_after':client_after})
        b.save(stem+'-server.log', b.kub('logs','benchmark-server','-c','server'))
        b.save(stem+'-final-wire.json', v.verify_values(256))
        assert int(b.cli('DBSIZE')) == 1000000
        rows.append(result)
        b.save('comparison.json', rows)
        print('DONE', stem, round(result['ops']), result['p99_ms'], flush=True)
