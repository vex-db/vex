#!/usr/bin/env python3
"""Revalidate completed cells and summarize the runtime partition AWS sweep."""
import argparse
import csv
import json
import pathlib
import statistics


def summarize(root, partial=False):
    protocol = json.loads((root/'protocol.json').read_text())
    rows = []
    for cell in protocol['order']:
        path = root/f"{cell['shape']}-c{cell['cpu']}-p{cell['partitions']}"
        if partial and not (path/'complete.json').exists(): continue
        load = lambda name: json.loads((path/name).read_text())
        complete = load('complete.json')
        assert all(complete[k] == v for k,v in cell.items())
        identity = load('identity.json')
        assert identity['exe_sha256'] == protocol['binary_sha256']
        quota,period = map(int, identity['cgroup']['cpu_max'].split())
        assert quota/period == cell['cpu']
        assert all(len(t['affinity']) > 1 for t in identity['process']['tasks'])
        command = load('server.json')['spec']['containers'][0]['command'][-1]
        assert f"--sorted-set-partitions {cell['partitions']}" in command
        assert f"sorted-set-partitions={cell['partitions']}" in (path/'startup.log').read_text()
        trials = complete['rows'];assert len(trials) == 4
        assert [t['label'] for t in trials] == ['warmup','confirm-r1','confirm-r2','confirm-r3']
        total_writes = 0;cpu_cost = [];server_cores = []
        for trial in trials:
            label = trial['label'];raw=load(label+'.raw.json');stats=raw['ALL STATS'];cfg=raw['configuration']
            assert all(cfg[k] == v for k,v in dict(threads=32,clients=4,pipeline=16,rate_limit=0).items())
            assert trial['ops_per_sec'] == stats['Totals']['Ops/sec']
            assert trial['p99_ms'] == stats['Totals']['Percentile Latencies']['p99.00']
            writes = sum(v['Count'] for k,v in stats.items() if k.lower().rstrip('s') == 'zincrby')
            ranks = sum(v['Count'] for k,v in stats.items() if k.lower().rstrip('s') == 'zrank')
            assert writes == trial['writes'] and writes+ranks == stats['Totals']['Count']
            assert .19 < ranks/(writes+ranks) < .21
            total_writes += writes
            before,after=load(label+'.server-before.json'),load(label+'.server-after.json')
            for point in (before,after):
                assert point['exe_sha256']==protocol['binary_sha256'] and point['boot']==identity['boot']
                assert not any(point['cgroup']['memory_events'].get(k,0) for k in ('high','max','oom','oom_kill'))
            cb,ca=load(label+'.client-before.json'),load(label+'.client-after.json')
            assert cb['boot']==ca['boot']
            seconds=protocol['warmup_seconds'] if label=='warmup' else protocol['measure_seconds']
            throttle=(ca['cgroup']['cpu']['throttled_usec']-cb['cgroup']['cpu']['throttled_usec'])/1e6
            valid=(max(t['cores_used'] for t in trial['client_cpu']['Per Thread'].values())<.9 and trial['client_cpu']['cpu_cores_used']<38.4 and throttle<.01*seconds)
            assert trial['client_valid']==valid
            assert trial['pass_5ms']==(valid and trial['p99_ms']<=5)
            if label!='warmup':
                used=after['cgroup']['cpu']['usage_usec']-before['cgroup']['cpu']['usage_usec']
                cpu_cost.append(used/stats['Totals']['Count'])
                server_cores.append(used/1e6/(after['monotonic']-before['monotonic']))
        verification=load('verify.json')
        assert verification['expected']==verification['score_sum']==total_writes
        boards,members,_=protocol['shapes'][cell['shape']]
        assert verification['cardinalities']==[members]*boards
        for role in ('server','client'):
            snapshots=[]
            for moment in ('before','after'):
                invocation=load(moment+'-ena-'+role+'.json');assert invocation['Status']=='Success'
                counters={}
                for line in invocation['StandardOutputContent'].splitlines():
                    parts=line.split()
                    if len(parts)==2 and 'allowance_exceeded' in parts[0]: counters[parts[0].rstrip(':')]=int(parts[1])
                assert counters;snapshots.append(counters)
            assert snapshots[0].keys()==snapshots[1].keys()
            assert complete['network_deltas'][role]=={k:snapshots[1][k]-v for k,v in snapshots[0].items()}
        network_ok=all(v==0 for c in complete['network_deltas'].values() for v in c.values())
        valid=all(t['client_valid'] for t in trials) and network_ok
        assert complete['valid']==valid
        measured=trials[1:]
        rows.append(dict(**cell,valid=valid,pass_5ms=network_ok and all(t['pass_5ms'] for t in measured),
            ops_s=statistics.median(t['ops_per_sec'] for t in measured),
            min_ops_s=min(t['ops_per_sec'] for t in measured),max_ops_s=max(t['ops_per_sec'] for t in measured),
            p99_ms=statistics.median(t['p99_ms'] for t in measured),max_p99_ms=max(t['p99_ms'] for t in measured),
            cpu_us_per_op=statistics.median(cpu_cost),server_cores=statistics.median(server_cores),
            rss_mib=statistics.median(t['rss_mib'] for t in measured)))
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=pathlib.Path)
    parser.add_argument('--partial',action='store_true')
    args=parser.parse_args();rows=summarize(args.root,args.partial)
    print(json.dumps(rows,indent=2))
    if not args.partial:
        (args.root/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
        with (args.root/'summary.csv').open('w') as file:
            writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
