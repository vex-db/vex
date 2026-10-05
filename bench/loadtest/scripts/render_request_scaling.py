#!/usr/bin/env python3
"""Render confirmed CPU scaling and the full connection/latency searches."""
import argparse
import json
import pathlib

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

LABELS = {'get': 'GET', 'hash': 'Hash updates + reads', 'zset': 'Sorted-set updates + ranks'}
COLORS = {'get': '#059669', 'hash': '#2563eb', 'zset': '#7c3aed'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=pathlib.Path)
    p.add_argument('summary', type=pathlib.Path)
    p.add_argument('output', type=pathlib.Path)
    o = p.parse_args(); o.output.mkdir(parents=True, exist_ok=True)
    report = json.loads(o.summary.read_text()); rows = report['rows']
    plt.rcParams.update({'font.family':'DejaVu Sans', 'font.size':10, 'text.color':'#334155', 'axes.labelcolor':'#334155'})
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.8))
    fig.subplots_adjust(top=.76, bottom=.20, left=.08, right=.97, wspace=.25)
    for workload in LABELS:
        data = sorted((r for r in rows if r['valid'] and r['workload']==workload), key=lambda r:r['cpu'])
        if not data: continue
        xs=[r['cpu'] for r in data]; ys=[r['ops_per_sec'] for r in data]
        axes[0].errorbar(xs, ys, yerr=[[y-r['ops_min'] for r,y in zip(data,ys)], [r['ops_max']-y for r,y in zip(data,ys)]],
                         marker='o', capsize=3, color=COLORS[workload], label=LABELS[workload])
        axes[1].plot(xs, [r['cpu_us_per_op'] for r in data], marker='o', color=COLORS[workload])
    for ax in axes:
        ax.set_xticks(sorted(report['protocol']['cpus'])); ax.set_xlabel('CPU quota / worker count'); ax.grid(alpha=.2)
        ax.spines[['top','right']].set_visible(False)
    axes[0].set_ylim(bottom=0); axes[0].set_ylabel('Operations / second')
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda v,p: f'{v/1e6:g}M' if v >= 1e6 else f'{v/1000:g}k'))
    axes[1].set_ylim(bottom=0); axes[1].set_ylabel('Server CPU µs / operation')
    axes[0].legend(frameon=False, fontsize=9)
    fig.suptitle('Vex: throughput below 5 ms p99', x=.06, ha='left', fontsize=21, fontweight='bold')
    fig.text(.06,.86, f"AWS • 32–1,024 connection sweep • {report['completed']}/{report['expected']} cells complete", fontsize=11)
    fig.text(.06,.09,'Three 60-second confirmations per point; whiskers show throughput range. Pipeline 1; no TTLs or persistence.',fontsize=9)
    fig.text(.06,.05,'Unpinned CPU quotas on one c6gn.8xlarge host; separate c7g.16xlarge client. Confirmed lower bounds, not proven maxima.',fontsize=9)
    fig.savefig(o.output/'cpu-scaling.png',dpi=160,facecolor='white'); plt.close(fig)

    cells=[json.loads(f.read_text()) for f in o.run.glob('c*-after-*/complete.json')]
    fig, axes=plt.subplots(2,3,figsize=(15,9),sharex=True)
    fig.subplots_adjust(top=.84,bottom=.14,hspace=.25,wspace=.25)
    palette={1:'#94a3b8',2:'#0ea5e9',4:'#059669',8:'#7c3aed',16:'#e11d48'}
    for column,w in enumerate(LABELS):
        axes[0,column].set_title(LABELS[w],fontweight='bold')
        for cell in sorted((c for c in cells if c['workload']==w and c['network_valid']),key=lambda c:c['cpu']):
            data=sorted((r for r in cell['search'] if r.get('measurement_valid',True) and r['client_valid']),key=lambda r:r['connections'])
            xs=[r['connections'] for r in data]
            for row,metric in [(0,'ops_per_sec'),(1,'p99_ms')]:
                axes[row,column].plot(xs,[r[metric] for r in data],marker='o',color=palette[cell['cpu']],label=f"{cell['cpu']} CPUs")
        axes[0,column].yaxis.set_major_formatter(FuncFormatter(lambda v,p: f'{v/1e6:g}M' if v >= 1e6 else f'{v/1000:g}k'))
        axes[0,column].set_ylim(bottom=0)
        axes[1,column].axhline(5,color='#ef4444',linestyle='--',linewidth=1)
        axes[1,column].set_xlabel('Connections'); axes[1,column].set_yscale('log')
        for ax in axes[:,column]:
            ax.set_xscale('log',base=2); ax.set_xticks([32,64,128,256,512,1024],['32','64','128','256','512','1024'])
            ax.grid(alpha=.2); ax.spines[['top','right']].set_visible(False)
    axes[0,0].set_ylabel('Operations / second'); axes[1,0].set_ylabel('p99 latency (ms, log scale)')
    axes[0,0].legend(frameon=False,fontsize=9)
    fig.suptitle('More connections: capacity and latency',x=.07,ha='left',fontsize=21,fontweight='bold')
    fig.text(.07,.89,'15-second search trials • Dashed red line = 5 ms budget • Client-limited trials excluded',fontsize=11)
    fig.text(.07,.055,'Search points are exploratory; confirmed capacity is shown separately. Pipeline 1, closed-loop service latency; same AWS hosts.',fontsize=10)
    fig.savefig(o.output/'connection-sweep.png',dpi=160,facecolor='white'); plt.close(fig)


if __name__=='__main__': main()
