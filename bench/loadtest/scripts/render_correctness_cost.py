#!/usr/bin/env python3
"""Render one completed CPU cohort from the cost comparison summary."""
import argparse
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary')
    parser.add_argument('output')
    parser.add_argument('--cpus', type=int, default=4)
    args = parser.parse_args()
    data = json.load(open(args.summary))
    rows = [row for row in data['pairs'] if row['cpu'] == args.cpus]
    assert len(rows) == 7 and all(row[group][version] for row in rows
        for group in ('confirmed', 'control') for version in ('before', 'after'))
    labels = dict(get='GET', set='SET', incr='INCR', hash='Hash updates + reads',
                  list='List updates + reads', **{'set-member': 'Set membership', 'zset': 'Sorted-set updates + ranks'})
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'text.color': '#26364d',
                         'axes.labelcolor': '#26364d', 'xtick.color': '#52627a', 'ytick.color': '#26364d'})
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), gridspec_kw={'width_ratios': [1.15, 1]})
    fig.subplots_adjust(left=.18, right=.96, top=.72, bottom=.18, wspace=.3)
    colors = {'before': '#94a3b8', 'after': '#0d9488'}
    for ax, group, metric, title in zip(axes, ('confirmed', 'control'), ('ops_per_sec', 'cpu_us_per_op'),
        ('Throughput below 5 ms p99\nMedian of 3 × 60-second confirmations',
         'CPU time per operation\nMedian of 3 × 30 seconds at 128 connections')):
        largest = max(row[group][v][metric] for row in rows for v in colors)
        for version, offset in [('before', -.17), ('after', .17)]:
            values = [row[group][version][metric] for row in rows]
            bars = ax.barh([i + offset for i in range(len(rows))], values, height=.3,
                           color=colors[version], zorder=3)
            for bar, value in zip(bars, values):
                label = (f'{value / 1e6:.2f}M' if value >= 1e6 else f'{value / 1000:.1f}k') if metric == 'ops_per_sec' else f'{value:.2f}'
                ax.text(value + largest * .018, bar.get_y() + bar.get_height() / 2,
                        label, va='center', fontsize=9)
        ax.set_xlim(0, largest * 1.2)
        ax.set_ylim(len(rows) - .5, -.5)
        ax.set_yticks(range(len(rows)), [labels[row['workload']] for row in rows] if ax is axes[0] else [])
        ax.set_title(title, loc='left', fontsize=11, fontweight='bold', pad=16)
        ax.set_xlabel('Operations / second (higher is better)' if metric == 'ops_per_sec' else 'CPU µs / operation (lower is better)', labelpad=10)
        if metric == 'ops_per_sec':
            scale, unit = (1e6, 'M') if largest >= 1e6 else (1e3, 'k')
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'{value / scale:g}{unit}'))
        ax.grid(axis='x', color='#e2e8f0', zorder=0)
        ax.tick_params(axis='both', length=0)
        for spine in ax.spines.values(): spine.set_visible(False)
    fig.text(.055, .94, f'Vex correctness fixes: {args.cpus}-CPU performance impact', fontsize=23, fontweight='bold')
    fig.text(.055, .895, 'Same AWS host and datasets • ReleaseFast builds • Unpinned • 256 partitions', fontsize=11)
    fig.legend(handles=[Patch(color=colors[v], label=label) for v, label in
               [('before', 'Before: integer fixes'), ('after', 'After: key-lifecycle fixes')]],
               loc='upper left', bbox_to_anchor=(.048, .855), ncol=2, frameon=False)
    fig.text(.055, .085, 'Throughput uses each build’s best confirmed tested setting; CPU uses identical client settings. Pipeline 1; no TTLs or persistence.', fontsize=9)
    fig.text(.055, .05, 'Confirmed rates are lower bounds, not proven maxima. Server cost per operation also depends on capacity and the workload mix.', fontsize=9)
    fig.savefig(args.output, dpi=160, facecolor='white')
    plt.close(fig)


if __name__ == '__main__': main()
