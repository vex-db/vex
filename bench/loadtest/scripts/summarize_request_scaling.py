#!/usr/bin/env python3
"""Summarize only completed, validated request-scaling cells."""
import argparse
import datetime
import json
import pathlib
import statistics


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=pathlib.Path)
    p.add_argument('output', type=pathlib.Path)
    o = p.parse_args(); o.output.mkdir(parents=True, exist_ok=True)
    protocol = json.loads((o.run / 'protocol.json').read_text())
    cells = [json.loads(f.read_text()) for f in sorted(o.run.glob('c*-after-*/complete.json'))]
    rows = []
    for cell in sorted(cells, key=lambda x: (x['cpu'], x['workload'])):
        group = cell['confirmed']
        valid = cell['network_valid'] and len(group) == 3 and all(r['pass_5ms'] and r['client_valid'] for r in group)
        row = dict(cpu=cell['cpu'], workload=cell['workload'], valid=valid)
        if valid:
            row.update({k: statistics.median(r[k] for r in group) for k in ('ops_per_sec', 'p99_ms', 'cpu_us_per_op')})
            row.update(connections=group[0]['connections'],
                       ops_min=min(r['ops_per_sec'] for r in group), ops_max=max(r['ops_per_sec'] for r in group),
                       server_cpu_cores=statistics.median(r['ops_per_sec'] * r['cpu_us_per_op'] / 1e6 for r in group))
        rows.append(row)
    expected = len(protocol['cpus']) * len(protocol['workloads'])
    (o.output / 'summary.json').write_text(json.dumps(dict(completed=len(cells), expected=expected, protocol=protocol, rows=rows), indent=2)+'\n')
    date = datetime.date.fromisoformat(o.run.name[:10]).strftime('%-d %B %Y')
    lines=[f'# Vex request scaling — {date}', '',
           f'{len(cells)}/{expected} workload/CPU cells complete. Results include only validated three-run confirmations.', '',
           '| CPUs | Workload | Confirmed ops/s | Connections | Median p99 ms | CPU µs/op | CPU cores used | Scaling vs 1 CPU |',
           '|---:|---|---:|---:|---:|---:|---:|---:|']
    for row in rows:
        if not row['valid']:
            lines.append(f"| {row['cpu']} | {row['workload']} | Not confirmed | — | — | — | — | — |")
            continue
        base = next((r for r in rows if r['cpu']==1 and r['workload']==row['workload'] and r['valid']), None)
        scale = f"{row['ops_per_sec']/base['ops_per_sec']:.2f}×" if base else '—'
        lines.append(f"| {row['cpu']} | {row['workload']} | {row['ops_per_sec']:,.0f} | {row['connections']} | {row['p99_ms']:.3f} | {row['cpu_us_per_op']:.2f} | {row['server_cpu_cores']:.2f} | {scale} |")
    lines += ['',
        'Same dedicated c6gn.8xlarge server host, CPU quotas and worker counts of ' + '/'.join(map(str, sorted(protocol['cpus']))) + '; unpinned. Separate c7g.16xlarge load generator with 48-CPU quota. Same availability zone, 8 GiB server memory, 256 partitions, adaptive ownership disabled.', '',
        'Each cell sweeps 32/64/128/256/512/1024 connections for 15 seconds each, then confirms the fastest passing setting with three 60-second runs (falling back if necessary). Pipeline 1, uncapped closed-loop load; p99 is service latency. Every confirmation must pass the 5 ms budget and client-headroom gates. Reported rates are confirmed lower bounds, not proven maxima.', '',
        'GET: one million 256-byte values, uniform reads. Hash: 4,096 hashes × 16 fields × 256 bytes, 80% HSET / 20% HGET. Sorted set: 64 sets × 4,096 members, 80% ZINCRBY / 20% ZRANK. No TTLs or persistence. These workload differences prevent interpreting cross-workload rates as equivalent work.', '',
        'Every cell verifies process/host continuity, memory health and ENA allowance counters. Hash/sorted-set data are checked exactly; GET uses the existing sampled-value validation. Client limits: each thread below 0.9 CPU, aggregate below 38.4 CPUs, throttling below 1% of trial duration.', '',
        'CPU figures cover engine cgroup CPU time; host networking CPU is not fully attributed to that cgroup. CPU quotas are not dedicated physical-core pinning. This run varies connections and CPU allocation, not key cardinality; it cannot independently identify hot-key or internal lock contention.', '',
        f"Binary SHA-256: `{protocol['binary_sha256']}`.", '',
        f'Raw evidence: `{o.run.resolve()}`. Cleanup state is recorded in the run directory; a deletion request alone does not prove EC2 termination.', '']
    (o.output / 'comparison.md').write_text('\n'.join(lines))
    print(f'{len(cells)}/{expected} completed cells summarized')


if __name__ == '__main__': main()
