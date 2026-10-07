"""Consolidate confirmed P1 evidence without inventing missing measurements.

Run from any directory; reads saved cohort summaries, not AWS or raw archives.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = {
    'r5': 'docs/benchmarks/2026-10-01/request-scaling-r5/summary.json',
    'r4': 'docs/benchmarks/2026-10-01/request-scaling/summary.json',
    'r6': 'docs/benchmarks/2026-10-05/request-scaling-r6/summary.json',
}
OUTPUT = ROOT / 'docs/benchmarks/2026-10-05/request-scaling'
NAMES = {'get': 'GET', 'hash': 'Hash', 'zset': 'Sorted set'}


def consolidate(summaries):
    rows, scaling, seen = [], [], set()
    selected = {}
    protocols = [summary['protocol'] for summary in summaries.values()]
    # Order of allocation sweeps differs, but the measurement protocol must match.
    assert all({k: v for k, v in p.items() if k != 'cpus'} ==
               {k: v for k, v in protocols[0].items() if k != 'cpus'}
               for p in protocols), 'incompatible protocols'
    for cohort, summary in summaries.items():
        for row in summary['rows']:
            if not row['valid']:
                continue
            key = (cohort, row['cpu'], row['workload'])
            assert key not in seen, f'duplicate cell: {key}'
            seen.add(key)
            rows.append(dict(row, cohort=cohort,
                             ops_per_allocated_cpu=row['ops_per_sec'] / row['cpu']))
            # Later sources take display priority; retain every cohort below.
            selected[(row['cpu'], row['workload'])] = rows[-1]
        for workload in NAMES:
            group = sorted((r for r in rows if r['cohort'] == cohort and
                            r['workload'] == workload), key=lambda r: r['cpu'])
            for before, after in zip(group, group[1:]):
                ratio = after['ops_per_sec'] / before['ops_per_sec']
                scaling.append(dict(cohort=cohort, workload=workload,
                                    from_cpu=before['cpu'], to_cpu=after['cpu'],
                                    throughput_ratio=ratio,
                                    throughput_gain_pct=(ratio - 1) * 100,
                                    quota_scaling_efficiency_pct=ratio / (after['cpu'] / before['cpu']) * 100))
    all_rows = rows
    rows = sorted(selected.values(), key=lambda r: (r['cpu'], list(NAMES).index(r['workload'])))
    missing = [dict(cpu=cpu, workload=workload)
               for cpu in sorted(protocols[0]['cpus']) for workload in NAMES
               if (cpu, workload) not in selected]
    return dict(protocol=protocols[0], completed=len(rows), expected=len(rows) + len(missing),
                rows=rows, cohort_rows=all_rows, same_cohort_scaling=scaling, missing=missing)


def main():
    result = consolidate({name: json.loads((ROOT / path).read_text())
                          for name, path in SOURCES.items()})
    result['sources'] = SOURCES
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    with (OUTPUT / 'results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result['rows'][0]))
        writer.writeheader()
        writer.writerows(result['rows'])
    lines = [
        '# Current Vex request benchmarks — updated 5 October 2026', '',
        f"**{result['completed']}/{result['expected']} CPU/workload combinations have confirmed measurements.** These are",
        'October 1 (r4/r5) and October 5 (r6) measurements of the same frozen binary.',
        'The r6 run fills gaps and repeats comparison points on one new host pair.',
        'For repeated cells, the table selects the newest confirmed cohort, regardless of',
        'which throughput is higher. All cohort measurements remain in the JSON.', '',
        '## Confirmed throughput', '',
        'Operations per second, median of three 60-second confirmations; pipeline 1.',
        'Each confirmation passed the 5 ms p99 and client/network gates. Rates are',
        'confirmed lower bounds, not proven maxima.', '',
        '| CPU quota | GET ops/s | Hash ops/s | Sorted-set ops/s | Host cohort |',
        '|---:|---:|---:|---:|---|',
    ]
    lookup = {(r['cpu'], r['workload']): r for r in result['rows']}
    for cpu in sorted(result['protocol']['cpus']):
        cells = [lookup.get((cpu, workload)) for workload in NAMES]
        values = [f"{r['ops_per_sec']:,.0f}" if r else 'Not measured' for r in cells]
        cohorts = ', '.join(sorted({r['cohort'] for r in cells if r})) or '—'
        lines.append(f"| {cpu} | {' | '.join(values)} | {cohorts} |")
    lines += ['', '**Each cohort used different physical hosts.** The matrix is a coverage',
              'inventory, not a single same-host scaling curve. Missing cells are not',
              'interpolated; preliminary search trials are not confirmations.', '',
              '## Calculated scaling on the same hosts', '',
              '| Workload | CPU quota change | Cohort | Throughput ratio | Throughput gain | Quota scaling efficiency |',
              '|---|---|---|---:|---:|---:|']
    for row in result['same_cohort_scaling']:
        lines.append(f"| {NAMES[row['workload']]} | {row['from_cpu']} → {row['to_cpu']} | {row['cohort']} | "
                     f"{row['throughput_ratio']:.3f}× | +{row['throughput_gain_pct']:.1f}% | "
                     f"{row['quota_scaling_efficiency_pct']:.1f}% |")
    lines += ['', 'Throughput ratio = higher-allocation ops/s ÷ lower-allocation ops/s.',
              'Quota scaling efficiency = throughput ratio ÷ CPU-quota ratio × 100%.',
              'This measures how closely throughput follows allocation growth, not physical-core',
              'utilization or cloud cost. Connections are selected separately for each allocation;',
              'these are not fixed-concurrency comparisons. Only the within-cohort pairs above',
              'support scaling calculations; combining cohorts does not establish a 1 → 16 curve.', '',
              '## Latency and calculated throughput per CPU', '',
              '| CPU quota | Workload | Connections | Median p99 ms | Ops/s per allocated CPU | Observed ops/s range | Cohort |',
              '|---:|---|---:|---:|---:|---|---|']
    for row in result['rows']:
        lines.append(f"| {row['cpu']} | {NAMES[row['workload']]} | {row['connections']} | "
                     f"{row['p99_ms']:.3f} | {row['ops_per_allocated_cpu']:,.0f} | "
                     f"{row['ops_min']:,.0f}–{row['ops_max']:,.0f} | {row['cohort']} |")
    lines += ['', 'Ops/s per allocated CPU = measured ops/s ÷ CPU quota. This is allocation',
              'efficiency, not throughput per consumed CPU second. Original cgroup CPU figures',
              'remain in the source summaries and CSV; see the [accounting audit](../README.md)',
              'before interpreting them. Delayed collection brackets prevent trustworthy host-cost',
              'attribution for the 16-CPU sorted-set trials; no replacement CPU-cost estimate is made.', '',
              '## Coverage and remaining comparisons', '',
              *[f"- {r['cpu']} CPUs: {NAMES[r['workload']]}." for r in result['missing']],
              *(['All 15 planned CPU/workload cells now have confirmed measurements.', ''] if not result['missing'] else []),
              '- SET-only workload and current-build matched Redis/Dragonfly comparisons.', '',
              'Additional comparisons require real runs on matched hosts. A complete inventory',
              'across cohorts does not establish a same-host curve across all CPU counts.',
              'The earlier 32-connection optimization percentages compared',
              'builds at the same CPU allocation, not scaling between allocations.', '',
              '## Protocol and provenance', '',
              '- Server c6gn.8xlarge; separate c7g.16xlarge client with a 48-CPU quota.',
              '- Server memory 8 GiB; workers equal CPU quota, unpinned; 256 partitions;',
              '  adaptive ownership disabled; no TTL or persistence.',
              '- GET: one million 256-byte values, uniform reads.',
              '- Hash: 4,096 hashes × 16 fields × 256 bytes; 80% HSET / 20% HGET.',
              '- Sorted set: 64 sets × 4,096 members; 80% ZINCRBY / 20% ZRANK.',
              '- Uncapped closed-loop load; pipeline 1; service latency. Search 32–1,024',
              '  connections, then confirm the fastest passing setting with three 60-second trials.', '',
              f"Measured binary SHA-256: `{result['protocol']['binary_sha256']}`.",
              'This is the measured request-path build, not the later October 5 packaging build.', '',
              'Sources: [r5](../../2026-10-01/request-scaling-r5/comparison.md),',
              '[r4](../../2026-10-01/request-scaling/comparison.md), and',
              '[r6](../request-scaling-r6/comparison.md). Raw evidence is retained locally.',
              'r4 cleanup was verified; r5 resources were verified absent and cleaned up on October 5.',
              'r6 resumed on the original hosts after credential refresh and completed all nine cells.',
              '[Cleanup verification](../request-scaling-r6/cleanup-verification.json) confirms both',
              'instances terminated and all run-owned Kubernetes resources were removed.', '',
              'The [September three-engine comparison](../../../benchmarks.md#september-29-three-engine-comparison)',
              'uses an older adaptive build and pipeline 16. Its competitor numbers cannot be',
              'combined with this pipeline-1 table to claim a current Redis/Dragonfly advantage.', '',
              'Reproduce from the committed source summaries:', '', '```sh',
              'python3 bench/loadtest/scripts/summarize_current_request_scaling.py', '```', '',
              '[Machine-readable results](summary.json) · [CSV](results.csv)', '']
    (OUTPUT / 'comparison.md').write_text('\n'.join(lines))


if __name__ == '__main__':
    main()
