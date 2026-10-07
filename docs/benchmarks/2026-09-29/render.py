#!/usr/bin/env python3
"""Regenerate the published sorted-set chart from the checked-in result rows."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
ENGINES = ['redis', 'Vex adaptive', 'dragonfly']
COLORS = ['#df424b', '#00867a', '#7854cc']
WORKLOADS = ['hot', 'mixed', 'uniform64', 'uniform1024']
LABELS = ['Single hot key', 'Mixed hot/cold', 'Uniform · 64 sets', 'Uniform · 1,024 sets']


def main():
    data = json.loads((ROOT / 'results.json').read_text())
    rows = {(r['workload'], r['engine']): r for r in data['rows']}
    assert data['completed_measurements'] == 36 and len(rows) == len(data['rows']) == 12
    assert set(rows) == {(w, e) for w in WORKLOADS for e in ENGINES}
    assert all(r['valid'] and r['min_ops_s'] <= r['ops_s'] <= r['max_ops_s'] for r in rows.values())
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 12, 'svg.fonttype': 'none'})
    fig, axes = plt.subplots(2, 1, figsize=(14, 10))
    fig.patch.set_facecolor('#ffffff')
    fig.subplots_adjust(left=.08, right=.98, top=.80, bottom=.19, hspace=.53)
    fig.text(.06, .95, 'Sorted-set performance on matched AWS hardware', size=23, weight='bold', color='#22334a')
    fig.text(.06, .915, '8-CPU quota · 8 GiB per engine · separate client host · persistence disabled', size=13, color='#536174')
    handles = []
    for axis, field, scale, title, unit in [
        (axes[0], 'ops_s', 1e6, 'Throughput — higher is better', 'Million ops/s'),
        (axes[1], 'cpu_us_op', 1, 'Server CPU cost — lower is better', 'µs per operation')]:
        axis.set_title(title, loc='left', weight='bold', pad=16, color='#22334a')
        axis.set_axisbelow(True)
        axis.grid(axis='y', color='#e0e6ec', linewidth=.8)
        axis.spines[['top', 'right', 'left']].set_visible(False)
        axis.spines['bottom'].set_color('#d1d9e2')
        axis.tick_params(axis='both', length=0, pad=9)
        axis.set_xticks(range(4), LABELS)
        axis.set_ylabel(unit)
        axis.set_ylim(0, 4.35 if field == 'ops_s' else 5.1)
        for i, (engine, color) in enumerate(zip(ENGINES, COLORS)):
            values = [rows[w, engine][field] / scale for w in WORKLOADS]
            positions = [x + (i - 1) * .24 for x in range(4)]
            bars = axis.bar(positions, values, width=.21, color=color, label=engine)
            axis.bar_label(bars, labels=[f'{v:.2f}' for v in values], padding=6, size=11, weight='bold', color='#22334a')
            if field == 'ops_s':
                low = [v - rows[w, engine]['min_ops_s'] / scale for w, v in zip(WORKLOADS, values)]
                high = [rows[w, engine]['max_ops_s'] / scale - v for w, v in zip(WORKLOADS, values)]
                axis.errorbar(positions, values, yerr=[low, high], fmt='none', ecolor='#22334a', capsize=3, linewidth=1)
                handles.append(bars)
    fig.legend(handles, ['Redis', 'Vex adaptive (experimental)', 'Dragonfly'], loc='upper left', bbox_to_anchor=(.06, .89), ncol=3, frameon=False)
    notes = [
        'Medians of 3 × 60 seconds · 32 connections · pipeline 16 · ~80% ZINCRBY / 20% ZRANK.',
        'Throughput whiskers: min–max. Mixed traffic: 24 hot connections; 8 cold connections across 63 other keys.',
        '262,144 members per dataset. 1,024-set tests used a fresh matched host pair of the same types.',
        'Adaptive ownership is off by default and stayed inactive in uniform tests. Fixed concurrency, not peak capacity.',
        'September 28–29, 2026 · All 36 measurements passed data, client CPU and network checks.'
    ]
    for i, note in enumerate(notes):fig.text(.06, .135 - i * .022, note, size=10, color='#536174')
    for ext in ['png', 'svg']:fig.savefig(ROOT / ('comparison.' + ext), dpi=160, facecolor='white')
    plt.close(fig)


if __name__ == '__main__':
    main()
