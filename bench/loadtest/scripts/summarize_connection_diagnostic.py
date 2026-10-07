#!/usr/bin/env python3
"""Report paired connection/pipeline trials without treating pipelined p99 as equivalent."""
import argparse
import json
import pathlib
import statistics


def summarize(run):
    cell = run / 'connection-diagnostic'
    data = json.loads((cell / 'complete.json').read_text())
    rows = []
    for connections, pipeline in ((32, 1), (32, 16), (512, 1)):
        group = [r for r in data['rows'] if r['connections'] == connections and r['pipeline'] == pipeline]
        valid = data['network_valid'] and len(group) == 3 and all(r.get('measurement_valid', True) and r['client_valid'] for r in group)
        row = dict(connections=connections, pipeline=pipeline, valid=valid, repetitions=len(group))
        if valid:
            row.update({k: statistics.median(r[k] for r in group) for k in ('ops_per_sec', 'p99_ms', 'cpu_us_per_op')})
            row['server_cpu_cores'] = statistics.median(r['ops_per_sec'] * r['cpu_us_per_op'] / 1e6 for r in group)
            task_samples = []
            for r in group:
                before = json.loads((cell / (r['label'] + '.server-before.json')).read_text())
                after = json.loads((cell / (r['label'] + '.server-after.json')).read_text())
                previous = {t['tid']: t for t in before['process']['tasks']}
                deltas = sorted((t['cpu_seconds']-previous[t['tid']]['cpu_seconds'] for t in after['process']['tasks'] if t['tid'] in previous), reverse=True)
                busiest = deltas[:16]
                assert len(busiest) == 16 and min(busiest) >= 0
                count = json.loads((cell / (r['label'] + '.raw.json')).read_text())['ALL STATS']['Totals']['Count']
                costs = {k.replace('_seconds', '_us_per_op'): sum(t[k]-previous[t['tid']][k] for t in after['process']['tasks'] if t['tid'] in previous)*1e6/count
                         for k in ('user_seconds', 'system_seconds')}
                task_samples.append(dict(label=r['label'], all_task_cpu_seconds=deltas,
                    busiest16_cv=statistics.pstdev(busiest)/statistics.mean(busiest), **costs))
            row['busiest16_thread_cpu_cv'] = statistics.median(t['busiest16_cv'] for t in task_samples)
            for key in ('user_us_per_op', 'system_us_per_op'):
                row[key] = statistics.median(t[key] for t in task_samples)
            row['task_samples'] = task_samples
        rows.append(row)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=pathlib.Path); p.add_argument('output', type=pathlib.Path)
    o = p.parse_args(); rows = summarize(o.run); o.output.mkdir(parents=True, exist_ok=True)
    (o.output / 'summary.json').write_text(json.dumps(rows, indent=2)+'\n')
    lines = ['# Connection concurrency versus pipelining', '',
        '16 Vex workers / 16-CPU quota on the same c6gn.8xlarge host. Separate 48-CPU client; 32 load-generator threads in every configuration. Uniform GET, one million 256-byte values, no persistence. Three 60-second trials per configuration, rotating their order.', '',
        '| Connections | Pipeline | Maximum in flight | Median ops/s | Median p99 ms | CPU µs/op | Server CPUs used | Busiest 16 threads: CPU CV |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        prefix = f"| {r['connections']} | {r['pipeline']} | {r['connections']*r['pipeline']} |"
        lines.append(prefix + (f" {r['ops_per_sec']:,.0f} | {r['p99_ms']:.3f} | {r['cpu_us_per_op']:.2f} | {r['server_cpu_cores']:.2f} | {r['busiest16_thread_cpu_cv']:.3f} |" if r['valid'] else ' Invalid/unconfirmed | — | — | — | — |'))
    if all(r['valid'] for r in rows):
        baseline, pipelined, connections = rows
        lines += ['', f"With the same 512-request ceiling, 32 connections at pipeline 16 delivered {pipelined['ops_per_sec']/connections['ops_per_sec']:.2f}× the throughput and {100*(1-pipelined['cpu_us_per_op']/connections['cpu_us_per_op']):.1f}% less CPU per operation than 512 connections at pipeline 1. Relative to 32 connections at pipeline 1, throughput rose {pipelined['ops_per_sec']/baseline['ops_per_sec']:.2f}×. This is a configuration effect on the same binary, not an engine code optimization."]
    lines += ['', '| Connections × pipeline | User CPU µs/op | Kernel CPU µs/op |', '|---|---:|---:|']
    for r in rows:
        if r['valid']:
            lines.append(f"| {r['connections']} × {r['pipeline']} | {r['user_us_per_op']:.2f} | {r['system_us_per_op']:.2f} |")
    lines += ['', 'User/kernel CPU split comes from process-thread counters; it excludes networking work attributed elsewhere on the host. It identifies a cost category, not a particular syscall or proven engine defect.', '', 'Only measurements passing client CPU/headroom, process continuity, memory, data validation and ENA allowance checks are reported. These diagnostic rates are not selected as 5 ms capacity maxima.', '',
        'The two 512-in-flight configurations have the same maximum outstanding request count, but pipelining also batches work and changes latency accounting. Similar throughput supports insufficient concurrency as an explanation; a gap alone does not prove a connection-management defect.', '',
        'Thread CV is the standard deviation divided by the mean CPU-time delta among the 16 busiest process threads, not an instrumented worker-identity measure. Lower values indicate more balanced CPU work. Cgroup CPU includes measurement-boundary overhead and excludes some host networking work.', '',
        f'Raw evidence: `{o.run.resolve()}`.', '']
    cleanup = o.run / 'cleanup-verification.json'
    if cleanup.exists() and json.loads(cleanup.read_text()).get('verified'):
        lines += ['Cleanup verified: both EC2 instances terminated; no run-owned Kubernetes resources or nodes remain.', '']
    (o.output / 'comparison.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__': main()
