#!/usr/bin/env python3
"""Summarize completed pairs; never substitute an unconfirmed search result."""
import argparse
import datetime
import json
import pathlib
import statistics

from compare_correctness_cost_aws import WORKLOADS


def metrics(cell, group):
    rows = cell[group]
    if not cell['network_valid'] or len(rows) != 3:
        return None
    if not all(r['client_valid'] and (group != 'confirmed' or r['pass_5ms']) for r in rows):
        return None
    return {k: statistics.median(r[k] for r in rows)
            for k in ('ops_per_sec', 'p99_ms', 'cpu_us_per_op', 'rss_mib')}


def summarize(root):
    protocol = json.loads((root / 'protocol.json').read_text())
    cells = {}
    for path in root.glob('c*-*/complete.json'):
        cell = json.loads(path.read_text())
        cells[cell['cpu'], cell['workload'], cell['version']] = cell
    pairs = []
    for cpu in protocol['cpus']:
        for workload in WORKLOADS:
            if not all((cpu, workload, v) in cells for v in ('before', 'after')):
                continue
            pair = dict(cpu=cpu, workload=workload)
            pair['loaded_rss_mib'] = {}
            for version in ('before', 'after'):
                snapshot = root / f'c{cpu}-{version}-{workload}/warmup.server-before.json'
                pair['loaded_rss_mib'][version] = json.loads(snapshot.read_text())['process']['rss_bytes'] / 2**20 if snapshot.exists() else None
            for group in ('confirmed', 'control'):
                pair[group] = {v: metrics(cells[cpu, workload, v], group) for v in ('before', 'after')}
            before, after = (pair['confirmed'][v] for v in ('before', 'after'))
            pair['relative_cost_per_operation'] = before['ops_per_sec'] / after['ops_per_sec'] if before and after else None
            pairs.append(pair)
    return dict(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                completed_cells=len(cells), expected_cells=len(protocol['cpus']) * len(WORKLOADS) * 2, pairs=pairs)


def report(data):
    lines = ['# Cost impact of the key-lifecycle correctness fix', '',
        f"Completed {data['completed_cells']}/{data['expected_cells']} cells as of {data['generated_at']}. Only completed before/after pairs appear below.", '',
        'Baseline: integer fixes already applied. Candidate: key-type, lifecycle and locking fixes. '
        'Both are Linux arm64 ReleaseFast binaries, unpinned, with adaptive ownership disabled. '
        'CPU allocations are quotas on the same c6gn.8xlarge host, with a separate c7g.16xlarge client.', '',
        '## Confirmed throughput below 5 ms p99', '',
        'Medians of three 60-second repetitions; every repetition must pass the latency and client gates. '
        'These are confirmed lower bounds from a connection sweep, not proven global maxima.', '',
        '| CPUs | Workload | Before ops/s | After ops/s | Change | Before / after p99 ms | Relative cost/op |',
        '|---:|---|---:|---:|---:|---:|---:|']
    for pair in data['pairs']:
        a, z = (pair['confirmed'][v] for v in ('before', 'after'))
        if a and z:
            lines.append(f"| {pair['cpu']} | {pair['workload']} | {a['ops_per_sec']:,.0f} | {z['ops_per_sec']:,.0f} | {z['ops_per_sec']/a['ops_per_sec']-1:+.1%} | {a['p99_ms']:.2f} / {z['p99_ms']:.2f} | {pair['relative_cost_per_operation']:.2f}× |")
        else:
            lines.append(f"| {pair['cpu']} | {pair['workload']} | — | — | — | No valid confirmed pair | — |")
    lines += ['', 'Relative cost/op assumes the same hourly server price and sustained operation at these confirmed rates. '
        'It excludes client cost and is not an actual invoice or a prediction of the number of instances needed.', '',
        '## Fixed-concurrency control', '',
        'Medians of three 30-second repetitions at 128 connections and pipeline 1. '
        'Both builds receive identical client settings; achieved request rates can differ. '
        'CPU includes total engine cgroup CPU, divided by completed operations. '
        'Loaded RSS is measured immediately after preload, before the connection sweep. '
        'Post-trial RSS is retained in JSON but can reflect different connection/allocator histories.', '',
        '| CPUs | Workload | Before / after ops/s | Before / after CPU µs/op | Before / after loaded RSS MiB | Before / after p99 ms |',
        '|---:|---|---:|---:|---:|---:|']
    for pair in data['pairs']:
        a, z = (pair['control'][v] for v in ('before', 'after'))
        if a and z:
            memory = ' / '.join(f'{value:.1f}' if value is not None else 'unavailable' for value in pair['loaded_rss_mib'].values())
            lines.append(f"| {pair['cpu']} | {pair['workload']} | {a['ops_per_sec']:,.0f} / {z['ops_per_sec']:,.0f} | {a['cpu_us_per_op']:.2f} / {z['cpu_us_per_op']:.2f} | {memory} | {a['p99_ms']:.2f} / {z['p99_ms']:.2f} |")
        else:
            lines.append(f"| {pair['cpu']} | {pair['workload']} | Invalid control pair | — | — | — |")
    lines += ['', 'Workloads: GET and SET each use one million 256-byte values; INCR uses 4,096 independent counters. '
        'Hashes and lists use 4,096 keys with 16 elements each and 80% updates / 20% reads. '
        'Set membership is read-only. Sorted sets use 64 sets × 4,096 members with 80% ZINCRBY / 20% ZRANK.', '',
        'No TTLs or persistence are enabled; these measurements do not quantify expiry metadata or expired-key reclamation. '
        'The baseline has known correctness bugs and is a measurement control, not a safe rollback recommendation.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    data = summarize(args.run)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'comparison.json').write_text(json.dumps(data, indent=2) + '\n')
    (args.output / 'comparison.md').write_text(report(data))
    print(f"{data['completed_cells']}/{data['expected_cells']} cells; {len(data['pairs'])} completed pairs")


if __name__ == '__main__': main()
