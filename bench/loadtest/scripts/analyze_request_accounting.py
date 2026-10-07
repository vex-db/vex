#!/usr/bin/env python3
"""Compare saved cgroup and host accounting; never convert unrecorded USER_HZ."""
import argparse
import json
import pathlib
import statistics


def counters(raw, before, after):
    stats = raw['ALL STATS']; runtime = stats['Runtime']
    assert runtime['Time unit'] == 'MILLISECONDS'
    assert str(runtime['Interrupted']).lower() == 'false'
    seconds = runtime['Total duration'] / 1000
    count = stats['Totals']['Count']
    assert count > 0 and seconds > 0
    assert before['boot'] == after['boot']
    assert all(before['process'][k] == after['process'][k] for k in ('pid', 'start'))
    bracket = after['monotonic'] - before['monotonic']
    if not seconds <= bracket <= seconds * 1.25:
        return dict(accepted=False, reason='accounting bracket outside measured duration to +25%',
                    measured_seconds=seconds, bracket_seconds=bracket)
    def host_cpu(snapshot):
        line = next(line for line in snapshot['node']['stat'].splitlines() if line.startswith('cpu '))
        return list(map(int, line.split()[1:]))
    old, new = host_cpu(before), host_cpu(after)
    assert len(old) >= 8 and len(new) >= 8
    delta = [new[i]-old[i] for i in range(8)]
    assert min(delta) >= 0
    result = dict(accepted=True, measured_seconds=seconds, bracket_seconds=bracket,
        host_busy_ticks_per_million_ops=(sum(delta)-delta[3]-delta[4])*1e6/count,
        host_system_softirq_ticks_per_million_ops=(delta[2]+delta[6])*1e6/count)
    for field in ('usage_usec', 'user_usec', 'system_usec'):
        change = after['cgroup']['cpu'][field] - before['cgroup']['cpu'][field]
        assert change >= 0
        result[field.replace('_usec', '_us_per_op')] = change / count
    return result


def collect(run):
    rows = []
    boots = set()
    for complete in sorted(run.glob('*/complete.json')):
        cell = json.loads(complete.read_text())
        if not cell['network_valid']: continue
        for trial in cell.get('confirmed', cell.get('rows', [])):
            if not trial.get('measurement_valid', True) or not trial['client_valid']: continue
            label = trial['label']; directory = complete.parent
            load = lambda suffix: json.loads((directory / (label + suffix)).read_text())
            before, after = load('.server-before.json'), load('.server-after.json')
            boots.add(before['boot'])
            assert len(boots) == 1, 'refusing to aggregate different server boots'
            result = counters(load('.raw.json'), before, after)
            rows.append(dict(cell=directory.name, label=label, connections=trial['connections'],
                             pipeline=trial.get('pipeline', 1), **result))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=pathlib.Path); p.add_argument('output', type=pathlib.Path)
    o = p.parse_args(); rows = collect(o.run); o.output.mkdir(parents=True, exist_ok=True)
    (o.output/'accounting.json').write_text(json.dumps(dict(source=str(o.run.resolve()), rows=rows), indent=2)+'\n')
    lines = ['# Request CPU accounting', '',
        'Diagnostic context only. Whole-host counters include background services and wider measurement brackets. Host counters are reported in ticks because USER_HZ was not recorded for this host; do not compare their numeric values directly with cgroup microseconds.', '',
        '| Cell | Connections × pipeline | Accepted / saved | Vex user µs/op | Vex system µs/op | Host system + softirq ticks / million ops |',
        '|---|---:|---:|---:|---:|---:|']
    for key in sorted({(r['cell'],r['connections'],r['pipeline']) for r in rows}):
        group = [r for r in rows if (r['cell'],r['connections'],r['pipeline']) == key]
        valid = [r for r in group if r['accepted']]
        fields = ('user_us_per_op', 'system_us_per_op', 'host_system_softirq_ticks_per_million_ops')
        values = [f'{statistics.median(r[k] for r in valid):.3f}' if valid else '—' for k in fields]
        lines.append(f'| {key[0]} | {key[1]} × {key[2]} | {len(valid)} / {len(group)} | '+ ' | '.join(values)+' |')
    lines += ['', 'Only completed cells with valid network checks and valid client measurements are used. Samples whose accounting bracket exceeds the measured runtime by 25% are excluded here, not erased from the original throughput evidence. Small accepted groups do not establish statistical significance. Compare host tick costs only within the same unchanged host cohort.', '',
              f'Raw source: `{o.run.resolve()}`.', '']
    (o.output/'accounting.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__': main()
