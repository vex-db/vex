#!/usr/bin/env python3
"""Revalidate and summarize all six cells of the paired sorted-set scaling run."""
import argparse
import csv
import json
import statistics


def summarize(root, cpus=None):
    protocol = json.loads((root / 'protocol.json').read_text())
    cpus = protocol['cpu_quotas'] if cpus is None else cpus
    assert set(cpus) <= set(protocol['cpu_quotas'])
    versions = protocol.get('engines', ['before', 'after'])
    fixed_settings = protocol.get('search', '') is None
    profiles = ['mixed'] if fixed_settings else (['mixed', 'pressure', 'update'] if 'engines' in protocol else ['mixed', 'update'])
    rows = []
    for cpu in cpus:
        for version in versions:
            cell = root / f'c{cpu}-{version}'
            complete = json.loads((cell / 'complete.json').read_text())
            identity = json.loads((cell / 'identity.json').read_text())
            assert identity['exe_sha256'] == protocol['binaries'][version]
            assert complete['cpu'] == cpu and complete['version'] == version
            quota, period = map(int, identity['cgroup']['cpu_max'].split())
            assert quota / period == cpu
            if version in ('before', 'after'):
                assert all(len(t['affinity']) > 1 for t in identity['process']['tasks'])
            phases = [json.loads(p.read_text()) for p in cell.glob('*.summary.json')]
            total_writes = 0
            for phase in phases:
                label = phase['label']
                raw = json.loads((cell / (label + '.raw.json')).read_text())
                assert phase['ops_per_sec'] == raw['ALL STATS']['Totals']['Ops/sec']
                assert phase['p99_ms'] == raw['ALL STATS']['Totals']['Percentile Latencies']['p99.00']
                if 'engines' in protocol:
                    cb = json.loads((cell / (label + '.client-before.json')).read_text())
                    ca = json.loads((cell / (label + '.client-after.json')).read_text())
                    throttle = (ca['cgroup']['cpu']['throttled_usec'] - cb['cgroup']['cpu']['throttled_usec']) / 1e6
                    budget = 10 if label == 'warmup' else 15 if label.startswith('search-') else 30 if label.startswith('update-') else 60
                    valid = (max(t['cores_used'] for t in phase['client_cpu']['Per Thread'].values()) < .9
                        and phase['client_cpu']['cpu_cores_used'] < 38.4 and throttle < .01 * budget)
                    assert phase['client_valid'] == valid
                    assert phase['pass_5ms'] == (valid and phase['p99_ms'] <= 5)
                    config = raw['configuration']
                    assert config['threads'] == 32 and config['clients'] == phase['clients_per_thread']
                    assert config['pipeline'] == phase['pipeline'] and config['rate_limit'] == 0
                writes = sum(v['Count'] for k, v in raw['ALL STATS'].items() if k.lower().rstrip('s') == 'zincrby')
                assert writes == phase['writes']
                total_writes += writes
                for moment in ('before', 'after'):
                    server = json.loads((cell / (label + '.server-' + moment + '.json')).read_text())
                    client = json.loads((cell / (label + '.client-' + moment + '.json')).read_text())
                    assert server['exe_sha256'] == protocol['binaries'][version]
                    assert server['boot'] == identity['boot']
                    for point in (server, client):
                        assert not any(point['cgroup']['memory_events'].get(k, 0) for k in ('high', 'max', 'oom', 'oom_kill'))
            verified = json.loads((cell / 'verify.json').read_text())
            assert verified['expected'] == verified['score_sum'] == total_writes
            assert verified['cardinalities'] == [4096] * 64
            for role in ('server', 'client'):
                snapshots = []
                for moment in ('before', 'after'):
                    invocation = json.loads((cell / (moment + '-ena-' + role + '.json')).read_text())
                    assert invocation['Status'] == 'Success'
                    counters = {}
                    for line in invocation['StandardOutputContent'].splitlines():
                        parts = line.split()
                        if len(parts) == 2 and 'allowance_exceeded' in parts[0]:
                            counters[parts[0].rstrip(':')] = int(parts[1])
                    snapshots.append(counters)
                assert snapshots[0] and snapshots[0].keys() == snapshots[1].keys()
                assert complete['network_deltas'][role] == {k:snapshots[1][k]-v for k,v in snapshots[0].items()}
            network_valid = all(v==0 for counts in complete['network_deltas'].values() for v in counts.values())
            assert complete.get('network_valid', complete.get('valid')) == network_valid
            for profile in profiles:
                trials = complete['rows'][1:] if fixed_settings else complete[{'mixed':'confirmed', 'pressure':'pressure', 'update':'updates'}[profile]]
                assert len(trials) == 3, (cell, profile, 'missing three-run confirmation')
                cpu_cost = []
                for trial in trials:
                    label = trial['label']
                    before = json.loads((cell / (label + '.server-before.json')).read_text())
                    after = json.loads((cell / (label + '.server-after.json')).read_text())
                    raw = json.loads((cell / (label + '.raw.json')).read_text())
                    cpu_cost.append((after['cgroup']['cpu']['usage_usec'] - before['cgroup']['cpu']['usage_usec']) / raw['ALL STATS']['Totals']['Count'])
                ops = [r['ops_per_sec'] for r in trials]
                latencies = [r['p99_ms'] for r in trials]
                assert all(r['client_valid'] for r in trials), (cell, profile, 'client limit')
                if profile == 'mixed' and not fixed_settings:
                    assert all(r['pass_5ms'] for r in trials)
                rows.append(dict(cpu=cpu, version=version, profile=profile,
                    median_ops_s=statistics.median(ops), min_ops_s=min(ops), max_ops_s=max(ops),
                    median_p99_ms=statistics.median(latencies), min_p99_ms=min(latencies), max_p99_ms=max(latencies),
                    median_rss_mib=statistics.median(r['rss_mib'] for r in trials),
                    median_cpu_us_per_op=statistics.median(cpu_cost),
                    clients_per_thread=trials[0]['clients_per_thread'], pipeline=trials[0]['pipeline'],
                    network_valid=network_valid, all_pass_5ms=all(r['pass_5ms'] for r in trials)))
    comparisons = []
    for profile in profiles:
        for cpu in cpus:
            for version in versions[1:]:
                a, b = [next(r for r in rows if r['cpu'] == cpu and r['version'] == v and r['profile'] == profile) for v in (versions[0], version)]
                comparisons.append(dict(cpu=cpu, profile=profile, baseline=versions[0], candidate=version,
                    median_gain=b['median_ops_s']/a['median_ops_s'],
                    nonoverlapping_ranges=b['min_ops_s'] > a['max_ops_s'],
                    network_valid=a['network_valid'] and b['network_valid']))
    scaling = {}
    for profile in profiles:
        for version in versions:
            points = {r['cpu']: r['median_ops_s'] for r in rows if r['version']==version and r['profile']==profile}
            scaling[profile+'-'+version] = {f'{a}_to_{b}':points[b]/points[a] for a,b in ((4,8),(8,16),(4,16)) if a in points and b in points}
    return dict(results=rows, comparisons=comparisons, scaling=scaling)


if __name__ == '__main__':
    import pathlib
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=pathlib.Path)
    args = parser.parse_args()
    data = summarize(args.run)
    (args.run/'analysis.json').write_text(json.dumps(data, indent=2)+'\n')
    with (args.run/'comparison.csv').open('w') as f:
        writer=csv.DictWriter(f, fieldnames=list(data['results'][0]))
        writer.writeheader();writer.writerows(data['results'])
    print(json.dumps(data, indent=2))
