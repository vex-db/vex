#!/usr/bin/env python3
"""Report each diagnostic repetition, preserving invalid trials and instrumentation."""
import argparse
import json
import pathlib
import statistics


def extract(run):
    rows=[]
    for complete in sorted((run/'cells').glob('r*/complete.json')):
        cell=json.loads(complete.read_text())
        for phase in cell['phases']:
            folder=complete.parent/phase['label']
            before=json.loads((folder/'server-before.json').read_text())
            after=json.loads((folder/'server-after.json').read_text())
            prior={t['tid']:t for t in before['process']['tasks']}
            workers=[]
            for task in after['process']['tasks']:
                if task['tid'] not in prior:continue
                delta={k:task[k]-prior[task['tid']][k] for k in ['cpu_seconds','user_seconds','system_seconds']}
                if delta['cpu_seconds'] < phase['seconds']*.05:continue
                workers.append(dict(tid=task['tid'],affinity=task['affinity'],**delta))
            def cpus(point):
                return {p[0]:list(map(int,p[1:])) for line in point['node']['stat'].splitlines() if (p:=line.split())[0].startswith('cpu')}
            a,b=cpus(after),cpus(before)
            softirq={k:a[k][6]-b[k][6] for k in a if k!='cpu'}
            raw=json.loads((folder/'stdout.json').read_text())
            connections=raw['connections']
            for key in ['offered','sent','completed','errors','uncompleted','over_1ms']:
                assert sum(x[key] for x in connections)==raw[key],(folder,key)
            rows.append(dict(cell=complete.parent.name,condition=cell['condition'],round=cell['round'],
                rate=phase['rate'],p99_ms=phase['p99_ms'],service_p99_ms=phase['service_p99_ms'],
                dispatch_p99_ms=phase['dispatch_p99_ms'],passed=phase['pass'],client_valid=phase['client_valid'],
                client_cpu_upper_bound=phase['client_cpu']['cores_upper_bound_in_window'],client_cpu_quota=phase['client_cpu_quota'],
                errors=phase['errors'],uncompleted=phase['uncompleted'],workers=workers,
                worker_cpu_max_min=max(x['cpu_seconds'] for x in workers)/min(x['cpu_seconds'] for x in workers),
                node_softirq_ticks=softirq,seconds=phase['seconds']))
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=pathlib.Path);p.add_argument('report',type=pathlib.Path);o=p.parse_args()
    rows=extract(o.run)
    groups={}
    for row in rows:groups.setdefault((row['condition'],row['rate']),[]).append(row)
    text=['# Four-to-eight-worker diagnostics','','Completed cells only; absent rows are not results. Each repeated p99 is shown separately. Instrumented probe trials are excluded.','','| Condition | Offered ops/s | Repetitions | Scheduled p99 ms | Passes | Client-valid trials | Median worker CPU max/min |','|---|---:|---:|---|---:|---:|---:|']
    for (condition,rate),group in groups.items():
        text.append(f"| {condition} | {rate:,} | {len(group)} | {', '.join(str(x['p99_ms']) for x in group)} | {sum(x['passed'] for x in group)} | {sum(x['client_valid'] for x in group)} | {statistics.median(x['worker_cpu_max_min'] for x in group):.2f} |")
    text+=['','Worker CPU ratios use counters over the same snapshot bracket, which includes launch/export overhead. Node softirq ticks are per-core host accounting, not engine-exclusive time. Client CPU values are conservative upper bounds over the nominal arrival window. A client-invalid trial cannot establish a server limit.','', 'Raw records: `'+str(o.run.resolve())+'`.']
    o.report.parent.mkdir(parents=True,exist_ok=True);o.report.write_text('\n'.join(text)+'\n')
    (o.run/'diagnostic-analysis.json').write_text(json.dumps(rows,indent=2)+'\n')
    print(f'{len(rows)} completed phase records summarized')

if __name__=='__main__':main()
