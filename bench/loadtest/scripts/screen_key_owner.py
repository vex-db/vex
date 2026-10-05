#!/usr/bin/env python3
"""Local A/B screen; shared client/server host, full-pipeline round-trip latency."""
import argparse
import gzip
import hashlib
import json
import os
import pathlib
import resource
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tests/integration'))
from sorted_set_owner import server
from sorted_set_concurrency import Client


def cpu_seconds(pid):
    raw = subprocess.check_output(['ps', '-o', 'time=', '-p', str(pid)], text=True).strip()
    value = 0.0
    for part in raw.split(':'):
        value = value * 60 + float(part)
    return value


def percentile(hist, q):
    target = sum(hist) * q
    count = 0
    for i, n in enumerate(hist):
        count += n
        if count >= target:
            assert i < len(hist)-1, 'latency histogram overflow'
            return (i+1) / 100  # milliseconds, upper bucket edge


def preload(address, sets=64, members=4096):
    with Client(address) as c:
        fields=[v for m in range(members) for v in (0,f'm:{m}')]
        for key in range(sets):
            assert c.call('ZADD', f'lb:{key}', *fields)==members


def verify(address, phases, sets=64, members=4096):
    with Client(address) as c:
        for key in range(sets):
            values=c.call('ZRANGE',f'lb:{key}',0,-1,'WITHSCORES')
            pairs=[(values[i],float(values[i+1])) for i in range(0,len(values),2)]
            assert len(pairs)==members and {p[0] for p in pairs}=={f'm:{m}'.encode() for m in range(members)}
            assert pairs==sorted(pairs,key=lambda p:(p[1],p[0]))
            expected=sum(c['Writes'][key] for phase in phases for c in phase['Clients'])
            assert sum(p[1] for p in pairs)==expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    parser.add_argument('--client', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--seconds', type=int, default=5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--workers', type=int, nargs='+', default=[4,8])
    parser.add_argument('--shapes', nargs='+', choices=['hot','mixed','uniform64','uniform1024'], default=['hot','mixed'])
    parser.add_argument('--modes', nargs='+', choices=['off','fixed','adaptive'], default=['off','fixed'])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = dict(binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                    workers=args.workers, shapes=args.shapes, modes=args.modes, connections=32, pipeline=16,
                    mixed='24 hot connections, 8 cold connections across 63 other sets',
                    command_mix='80% ZINCRBY / 20% ZRANK', dataset={shape: ('1024 sets x 256 members' if shape == 'uniform1024' else '64 sets x 4096 members') for shape in args.shapes},
                    warmup_seconds=2, measure_seconds=args.seconds, repeats=args.repeats,
                    client_gomaxprocs=4, latency='full pipeline round trip',
                    limitation='local shared host, no CPU quotas, not an AWS capacity result')
    (args.output/'protocol.json').write_text(json.dumps(protocol, indent=2)+'\n')
    rows=[]
    for workers in args.workers:
        for shape in args.shapes:
            sets,members=(1024,256) if shape=='uniform1024' else (64,4096)
            for repeat in range(args.repeats):
                for mode in (args.modes if repeat%2==0 else list(reversed(args.modes))):
                    owner=mode!='off'
                    name=f'{shape}-w{workers}-{mode}-r{repeat+1}'
                    with server(args.binary, 'lb:0' if mode=='fixed' else None, workers,
                                adaptive=mode=='adaptive',log_path=args.output/f'{name}.server.log') as (address, process):
                        preload(address,sets,members)
                        phases=[]
                        for label, seconds in [('warmup',2),('measure',args.seconds)]:
                            before=cpu_seconds(process.pid)
                            usage=resource.getrusage(resource.RUSAGE_CHILDREN)
                            command=[str(args.client.resolve()),'-host',f'{address[0]}:{address[1]}','-seconds',str(seconds)]
                            if shape=='mixed':command+=['-mixed']
                            command+=['-sets',str(sets),'-members',str(members)]
                            if shape.startswith('uniform'):command+=['-uniform']
                            result=subprocess.run(command,env=dict(os.environ,GOMAXPROCS='4'),capture_output=True,check=True,timeout=seconds+30)
                            after=cpu_seconds(process.pid)
                            end_usage=resource.getrusage(resource.RUSAGE_CHILDREN)
                            raw=json.loads(result.stdout);phases.append(raw)
                            with gzip.open(args.output/f'{name}-{label}.json.gz','wb') as file:file.write(result.stdout)
                            if label=='measure':
                                operations=sum(c['Ops'] for c in raw['Clients'])
                                row=dict(workers=workers,shape=shape,owner=owner,mode=mode,repeat=repeat+1,
                                         ops_s=operations/raw['Seconds'],cpu_us_op=(after-before)*1e6/operations,
                                         client_cores=(end_usage.ru_utime+end_usage.ru_stime-usage.ru_utime-usage.ru_stime)/raw['Seconds'])
                                for cold in (False,True):
                                    group=[c for c in raw['Clients'] if c['Cold']==cold]
                                    if not group:continue
                                    hist=[sum(c['Histogram'][i] for c in group) for i in range(20001)]
                                    prefix='cold' if cold else 'hot'
                                    row[prefix+'_ops_s']=sum(c['Ops'] for c in group)/raw['Seconds']
                                    row[prefix+'_batch_p99_ms']=percentile(hist,.99)
                        verify(address,phases,sets,members)
                        row['verified']=True
                    rows.append(row)
                    (args.output/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
                    print(name,json.dumps(row),flush=True)


if __name__=='__main__':main()
