#!/usr/bin/env python3
"""Summarize only complete, verified cells from the matched ownership AWS run."""
import argparse
import csv
import json
import pathlib
import statistics


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run',type=pathlib.Path)
    args=p.parse_args();rows=[]
    for cell in sorted(args.run.glob('*/complete.json')):
        value=json.loads(cell.read_text());assert value['verified']
        trials=value['rows'][1:];assert len(trials)==3
        row={'workload':value['shape'],'engine':{'after':'Vex adaptive'}.get(value['engine'],value['engine']),
             'valid':value['valid']}
        for key in ['ops_s','cpu_us_op','hot_ops_s','hot_batch_p99_ms','cold_ops_s','cold_batch_p99_ms','client_cores','max_client_thread_cores']:
            if key in trials[0]:row[key]=statistics.median(t[key] for t in trials)
        row['min_ops_s']=min(t['ops_s'] for t in trials);row['max_ops_s']=max(t['ops_s'] for t in trials)
        row['adaptive_transitions']=(cell.parent/'final.log').read_text().count('adaptive-owner activate')
        rows.append(row)
    (args.run/'analysis.json').write_text(json.dumps(rows,indent=2)+'\n')
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (args.run/'analysis.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    for row in rows:
        print(row['workload'],row['engine'],f"{row['ops_s']/1e6:.3f}M",f"{row['cpu_us_op']:.3f} us/op",row['hot_batch_p99_ms'],'valid',row['valid'])


if __name__=='__main__':main()
