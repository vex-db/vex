#!/usr/bin/env python3
"""Export confirmed GET/SET points; missing or invalid cells are never zero capacity."""
import argparse
import csv
import hashlib
import json
import pathlib
import statistics

ENGINES = ('vex', 'redis', 'dragonfly')


def collect(runs):
    """Combine only whole CPU groups when a transport interruption replaced hosts."""
    protocols = [json.loads((run/'protocol.json').read_text()) for run in runs]
    comparable = lambda p: {k:v for k,v in p.items() if k != 'cpu_quotas'}
    assert all(comparable(p) == comparable(protocols[0]) for p in protocols), 'cohort protocols differ'
    protocol = dict(protocols[0], cpu_quotas=sorted({c for p in protocols for c in p['cpu_quotas']}))
    entries = []; cohorts = []; excluded = []
    group = {(w,e) for w in ('get','set') for e in ENGINES}
    for run in runs:
        cells = [(f, json.loads(f.read_text())) for f in sorted(run.glob('c*-*/complete.json'))]
        complete_cpus = {c for c in protocol['cpu_quotas']
                         if {(d['workload'],d['engine']) for _,d in cells if d['cpu']==c} == group}
        selected = cells if len(runs)==1 else [(f,d) for f,d in cells if d['cpu'] in complete_cpus]
        excluded += [str(run.name+'/'+str(f.relative_to(run))) for f,d in cells if len(runs)>1 and d['cpu'] not in complete_cpus]
        entries += [(run,f,d) for f,d in selected]
        if selected:
            nodes = json.loads((run/'nodes.json').read_text())
            cohorts.append(dict(run=run.name, cpu_quotas=sorted({d['cpu'] for _,d in selected}),
                hosts=sorted(n['spec']['providerID'] for n in nodes['items'])))
    keys = [(d['cpu'],d['workload'],d['engine']) for _,_,d in entries]
    assert len(keys)==len(set(keys)), 'overlapping complete CPU groups; choose one matched cohort'
    if len(runs)>1:
        protocol['cpu_allocation'] = 'cgroup quotas; matched hosts within each CPU allocation; separate host cohorts across allocations'
    return protocol, entries, cohorts, excluded


def row(cell):
    result = {k: cell[k] for k in ('cpu', 'engine', 'workload', 'classification', 'valid')}
    group = cell['confirmations']
    if not cell['valid']:
        return dict(result, ops_s=None)
    assert cell['network_valid'] and len(group) == 3
    assert all(r['client_valid'] and r['pass_5ms'] and r['p99_ms'] <= 5 for r in group)
    assert len({r['connections'] for r in group}) == 1
    values = [r['ops_per_sec'] for r in group]
    median = statistics.median(values)
    assert median == cell['confirmed_ops_per_sec']
    return dict(result, ops_s=median, min_ops_s=min(values), max_ops_s=max(values),
        p99_ms=statistics.median(r['p99_ms'] for r in group),
        max_p99_ms=max(r['p99_ms'] for r in group),
        cpu_us_op=statistics.median(r['cpu_us_per_op'] for r in group),
        connections=group[0]['connections'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=pathlib.Path); p.add_argument('output', type=pathlib.Path)
    p.add_argument('--allow-partial', action='store_true')
    p.add_argument('--additional-run', type=pathlib.Path, action='append', default=[])
    o = p.parse_args()
    protocol, entries, cohorts, excluded = collect([o.run, *o.additional_run])
    rows = [row(d) for _,_,d in entries]
    rows.sort(key=lambda r:(r['cpu'], r['workload'], ENGINES.index(r['engine'])))
    by = {(r['cpu'], r['workload'], r['engine']): r for r in rows}
    expected = {(c,w,e) for c in protocol['cpu_quotas'] for w in ('get','set') for e in ENGINES}
    assert len(by) == len(rows) and set(by) <= expected
    assert set(by) == expected or o.allow_partial, 'Run incomplete; use --allow-partial for an explicitly partial report'
    data = dict(complete=set(by)==expected, completed_cells=len(rows), expected_cells=len(expected),
        valid_cells=sum(r['valid'] for r in rows), protocol=protocol, rows=rows,
        cohorts=cohorts, excluded_incomplete_cohort_cells=excluded,
        evidence={run.name+'/'+str(f.relative_to(run)): hashlib.sha256(f.read_bytes()).hexdigest() for run,f,_ in entries})
    o.output.mkdir(parents=True, exist_ok=True)
    (o.output/'results.json').write_text(json.dumps(data, indent=2)+'\n')
    fields = ['cpu','engine','workload','valid','ops_s','min_ops_s','max_ops_s','p99_ms','max_p99_ms','cpu_us_op','connections','classification']
    with (o.output/'results.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    lines = ['# GET/SET CPU scaling — 29 September 2026', '',
        f"{data['completed_cells']}/{data['expected_cells']} cells complete; {data['valid_cells']} valid. " + ('Complete sweep.' if data['complete'] else '**Partial results.**'), '',
        'Median throughput from three 60-second runs, each at or below 5 ms p99 service latency. Highest confirmed settings in a closed-loop concurrency sweep; lower bounds, not universal maximum capacities.', '',
        '1 million keys, 256-byte values, pipeline 1, persistence off. c6gn.8xlarge server; 8 GiB per engine. Separate c7g.16xlarge client. These are not exclusive physical-core allocations.', '',
        'Client threads: '+protocol.get('client_threads', '32')+'. Total connection search: '+', '.join(map(str,protocol['search_connections']))+'.', '']
    for cohort in cohorts:
        lines += [f"Host group `{cohort['run']}`: CPU quotas {', '.join(map(str,cohort['cpu_quotas']))}; hosts {', '.join(cohort['hosts'])}.", '']
    if len(cohorts)>1:
        lines += ['All three engines and both workloads at a CPU allocation use the same host pair. Host pairs differ between groups after a setup network interruption; cross-group scaling includes possible host variation. The incomplete eight-CPU group from the interrupted run is excluded.', '']
    for workload in ('get','set'):
        lines += ['## '+workload.upper(), '', '| CPU quota | Vex ops/s | Redis ops/s | Dragonfly ops/s |', '|---:|---:|---:|---:|']
        for cpu in protocol['cpu_quotas']:
            values = [by.get((cpu,workload,e),{}).get('ops_s') for e in ENGINES]
            lines.append(f'| {cpu} | '+' | '.join(f'{v:,.0f}' if v is not None else '—' for v in values)+' |')
        lines.append('')
    lines += ['## Variation and limits', '', '| CPUs | Workload | Engine | Min–max ops/s | Median / max p99 ms | Connections | Search classification |', '|---:|---|---|---:|---:|---:|---|']
    for r in rows:
        spread = f"{r['min_ops_s']:,.0f}–{r['max_ops_s']:,.0f}" if r['valid'] else 'unconfirmed'
        latency = f"{r['p99_ms']:.3f} / {r['max_p99_ms']:.3f}" if r['valid'] else '—'
        lines.append(f"| {r['cpu']} | {r['workload'].upper()} | {r['engine']} | {spread} | {latency} | {r.get('connections','—')} | {r['classification']} |")
    lines += ['', 'A rejected search trial is excluded from capacity calculations. The frozen Vex binary predates the dynamic descriptor-table fix. Connection-accept bursts can also reject trials below the old descriptor ceiling. Raw trials, errors, hashes, topology, CPU and ENA snapshots remain in the run directory.', '']
    (o.output/'README.md').write_text('\n'.join(lines))
    print(o.output)


if __name__ == '__main__': main()
