#!/usr/bin/env python3
"""Render the validated GET/SET capacity export with repetition ranges."""
import argparse
import json
import math
import pathlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('results', type=pathlib.Path)
p.add_argument('output', type=pathlib.Path, help='Output path without extension')
o = p.parse_args()
data = json.loads(o.results.read_text())
cpus = data['protocol']['cpu_quotas']
rows = {(r['cpu'],r['workload'],r['engine']):r for r in data['rows']}
valid = [r for r in rows.values() if r['valid']]
assert valid and len(rows) == len(data['rows'])
assert all(r['min_ops_s'] <= r['ops_s'] <= r['max_ops_s'] and r['max_p99_ms'] <= 5 for r in valid)
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':12,'svg.fonttype':'none'})
fig, axes = plt.subplots(1, 2, figsize=(15,8), sharey=True)
fig.subplots_adjust(left=.085,right=.98,top=.73,bottom=.30,wspace=.15)
ink='#263c36';muted='#536760'
fig.text(.07,.94,'Redis vs Vex vs Dragonfly: GET / SET scaling',size=24,weight='bold',color=ink)
fig.text(.07,.885,'Highest confirmed throughput at or below 5 ms p99 · no pipelining',size=14,color=muted)
upper = max(r['max_ops_s'] for r in valid)/1e6*1.18
for ax,workload in zip(axes,('get','set')):
    ax.set_title(workload.upper(),loc='left',weight='bold',color=ink,pad=16)
    ax.set_xlim(.5,max(cpus)+.5);ax.set_ylim(0,upper)
    ax.set_xticks(cpus);ax.set_xlabel('CPU quota',labelpad=12)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y,_:f'{y:g}M'))
    ax.grid(axis='y',color='#e1e8e4',linewidth=.8)
    ax.spines[['top','right','left']].set_visible(False)
    ax.spines['bottom'].set_color('#ccd8d1');ax.tick_params(length=0,pad=8)
    for engine,label,color in [('redis','Redis','#df424b'),('vex','Vex','#08734f'),('dragonfly','Dragonfly','#7752cc')]:
        selected=[rows.get((c,workload,engine),{}) for c in cpus]
        y=[r['ops_s']/1e6 if r.get('valid') else math.nan for r in selected]
        ax.plot(cpus,y,color=color,marker='o',markersize=6,linewidth=2.5,label=label)
        lows=[v-r['min_ops_s']/1e6 if r.get('valid') else math.nan for v,r in zip(y,selected)]
        highs=[r['max_ops_s']/1e6-v if r.get('valid') else math.nan for v,r in zip(y,selected)]
        ax.errorbar(cpus,y,yerr=[lows,highs],fmt='none',ecolor=color,capsize=3,linewidth=1)
        for cpu,value,row in zip(cpus,y,selected):
            if row.get('valid') and row['classification'] == 'client-limited lower bound':
                ax.annotate('client-limited', (cpu,value), xytext=(-30,30),
                            textcoords='offset points', ha='right', fontsize=10, color=color,
                            arrowprops=dict(arrowstyle='->', color=color))
axes[0].set_ylabel('Operations / second',labelpad=14)
fig.legend(*axes[0].get_legend_handles_labels(),loc='upper left',bbox_to_anchor=(.06,.845),ncol=3,frameon=False)
notes=[
    '1 million keys · 256-byte values · persistence off · median of 3 × 60 seconds; whiskers show min–max.',
    ('Matched hosts within each CPU allocation; two host groups · c6gn.8xlarge server / c7g.16xlarge client · 8 GiB.'
     if len(data.get('cohorts',[]))>1 else
     'Same c6gn.8xlarge server across quotas · 8 GiB per engine · separate c7g.16xlarge load generator.'),
    'Closed-loop service latency. Confirmed results are lower bounds; rejected trials are excluded.',
    'CPU quotas are not exclusive physical cores. Frozen Vex development build; Redis 8.10.1; Dragonfly 2.0.0.',
    f"29 September 2026 · {data['valid_cells']} valid cells out of {data['expected_cells']} planned."
]
if not data['complete']:notes[-1]='PARTIAL RESULTS — '+notes[-1]
for i,note in enumerate(notes):fig.text(.07,.20-i*.029,note,size=10.5,color=muted)
o.output.parent.mkdir(parents=True,exist_ok=True)
for ext in ('png','svg'):fig.savefig(str(o.output)+'.'+ext,dpi=160,facecolor='white')
plt.close(fig)
print(str(o.output)+'.png')
