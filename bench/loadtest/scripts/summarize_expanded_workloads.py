#!/usr/bin/env python3
"""Summarize completed larger-dataset cells without hiding invalid repetitions."""
import argparse
import json
import pathlib
import statistics


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=pathlib.Path)
    parser.add_argument('report',type=pathlib.Path)
    args=parser.parse_args()
    protocol=json.loads((args.run/'protocol.json').read_text())
    assert protocol['suite']=='workloads' and protocol['arrival_model'].startswith('uncapped closed-loop')
    groups={};rows=[];seen=set()
    for path in sorted((args.run/'cells').glob('r*/complete.json')):
        cell=json.loads(path.read_text());phase=cell['phases'][0]
        key=(cell['profile'],cell['engine'],cell['round'])
        assert key not in seen and cell['cpus']==4 and cell['keys']==protocol['keys']
        seen.add(key)
        assert phase['duration_seconds']>=protocol['seconds']*.9 and phase['miss_fraction']==0
        row=dict(profile=cell['profile'],engine=cell['engine'],round=cell['round'],**phase)
        rows.append(row);groups.setdefault(key[:2],[]).append(row)
    expected=9*protocol['rounds']
    assert len(rows)<=expected
    text=['# Larger-dataset four-CPU comparison','',
          f"Completed cells: {len(rows)}/{expected}. Each cell measures {protocol['seconds']} seconds after preload and warmup. Missing cells are not results.",'',
          'Uncapped closed-loop throughput and service latency; these p99 values do not establish a scheduled-arrival 1 ms SLO. All engines use four CPU quota, 4 GiB memory, one million preloaded keys, 80/20 GET/SET, 1,024 connections and pipeline one. The same c7g.8xlarge server and c7g.16xlarge client are used throughout, with 48 client CPUs.','',
          '| Profile | Engine | Repetitions | Ops/s, each repetition | Median ops/s | Service p99 ms, each repetition | Client-valid |',
          '|---|---|---:|---|---:|---|---:|']
    aggregated=[]
    for (profile,engine),group in sorted(groups.items()):
        group.sort(key=lambda x:x['round'])
        rates=[x['ops_per_sec'] for x in group]
        valid=all(x['client_headroom_ok'] for x in group)
        confirmed=len(group)==protocol['rounds'] and valid
        median=statistics.median(rates)
        aggregated.append(dict(profile=profile,engine=engine,repetitions=len(group),median_ops=median,
                               min_ops=min(rates),max_ops=max(rates),all_clients_valid=valid,confirmed=confirmed))
        text.append(f"| {profile} | {engine} | {len(group)} | {', '.join(f'{round(x):,}' for x in rates)} | {round(median):,} | {', '.join(str(x['p99_ms']) for x in group)} | {sum(x['client_headroom_ok'] for x in group)}/{len(group)} |")
    text += ['', 'Baseline uses 256-byte values. Mixed uses 32/256/1,024/4,096-byte values at 50/30/15/5% weights. Hot uses the same sizes with a Gaussian key distribution centered in the dataset and standard deviation equal to 1% of the key count.', '',
             'Client validity requires CPU below 80% of quota, every client thread below 0.9 CPU, and throttling below 1% of the measurement window. Invalid repetitions remain visible and cannot establish an engine capacity claim.', '',
             'Raw records: `'+str(args.run.resolve())+'`.']
    args.report.write_text('\n'.join(text)+'\n')
    (args.run/'workload-analysis.json').write_text(json.dumps(dict(rows=rows,groups=aggregated),indent=2)+'\n')
    print(f'{len(rows)}/{expected} completed cells summarized; {sum(g["confirmed"] for g in aggregated)}/9 groups confirmed')


if __name__=='__main__':main()
