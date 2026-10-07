#!/usr/bin/env python3
"""Matched 8-CPU adaptive Vex/Redis/Dragonfly comparison on isolated AWS hosts."""
import argparse
import gzip
import hashlib
import json
import pathlib
import shutil
import time

import compare_sorted_set_scaling as b
from screen_key_owner import percentile
from sweep_sorted_set_partitions_aws import CHECK
s = b.s
SHAPES = {'hot': (64, 4096), 'mixed': (64, 4096), 'uniform64': (64, 4096), 'uniform1024': (1024, 256)}

# Sample the load generator's actual OS threads. The resulting CPU bounds are
# trial-average diagnostics, not proof that every possible client bottleneck is absent.
WRAPPER = r'''import json,os,pathlib,subprocess,sys,time
base=pathlib.Path(sys.argv[1]); command=sys.argv[2:]; usage={};hz=os.sysconf('SC_CLK_TCK')
with open(str(base)+'.raw','w') as out,open(str(base)+'.err','w') as err:
 p=subprocess.Popen(command,stdout=out,stderr=err)
 while p.poll() is None:
  for path in pathlib.Path('/proc/'+str(p.pid)+'/task').glob('*/stat'):
   try:
    q=path.read_text().rsplit(')',1)[1].split();key=path.parent.name+':'+q[19]
    usage[key]=max(usage.get(key,0),(int(q[11])+int(q[12]))/hz)
   except (OSError,ValueError,IndexError):pass
  time.sleep(.2)
result=json.loads(pathlib.Path(str(base)+'.raw').read_text())
result['ClientThreadCPUSeconds']=usage
result['ClientStderr']=pathlib.Path(str(base)+'.err').read_text()
print(json.dumps(result));sys.exit(p.returncode)
'''


def summarize(raw, before, after, cb, ca):
    assert 32 == len(raw['Clients'])
    assert all(not c['Error'] for c in raw['Clients']) and not raw['ClientStderr']
    seconds=raw['Seconds'];ops=sum(c['Ops'] for c in raw['Clients']);assert seconds>0 and ops>0
    cpu=(after['cgroup']['cpu']['usage_usec']-before['cgroup']['cpu']['usage_usec'])/ops
    client_cpu=sum(raw['ClientThreadCPUSeconds'].values())/seconds
    max_thread=max(raw['ClientThreadCPUSeconds'].values())/seconds
    throttle=(ca['cgroup']['cpu']['throttled_usec']-cb['cgroup']['cpu']['throttled_usec'])/1e6
    row=dict(ops_s=ops/seconds,cpu_us_op=cpu,client_cores=client_cpu,max_client_thread_cores=max_thread,
             client_throttled_seconds=throttle,client_valid=client_cpu<12.8 and max_thread<.9 and throttle<seconds*.01,
             writes=sum(sum(c['Writes']) for c in raw['Clients']),seconds=seconds)
    for cold in (False,True):
        group=[c for c in raw['Clients'] if c['Cold']==cold]
        if not group:continue
        prefix='cold' if cold else 'hot'
        hist=[sum(c['Histogram'][i] for c in group) for i in range(20001)]
        row[prefix+'_ops_s']=sum(c['Ops'] for c in group)/seconds
        row[prefix+'_batch_p99_ms']=percentile(hist,.99)
    return row


def prepare(o):
    o.output.mkdir(parents=True,exist_ok=False)
    old=json.loads((b.SOURCE/'resources.json').read_text())['items'][0]['metadata']['name']
    for name in ('resources.json','holder.manifest.json','client-48.json'):
        obj=json.loads((b.SOURCE/name).read_text().replace(old,o.namespace))
        if name=='resources.json':
            for item in obj['items'][1:]:
                for req in item['spec']['template']['spec']['requirements']:
                    if req['key']=='topology.kubernetes.io/zone':req['values']=[o.zone]
                    if req['key']=='node.kubernetes.io/instance-type' and item['metadata']['name'].endswith('-server'):req['values']=[b.SERVER_TYPE]
        b.save(o.output/name,obj)
    o.template=json.loads((b.SOURCE/'server-candidate-16.json').read_text().replace(old,o.namespace))
    o.template['spec']['containers'][0]['image']=o.template['spec']['containers'][1]['image']
    b.BUILD=o.output/'frozen'
    for source,dest in [(o.binary,b.BUILD/'static-after/bin/vex'),(o.client,o.output/'key-owner-client')]:
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    engines=['after','redis','dragonfly'];order=[]
    for index,shape in enumerate(SHAPES):
        if shape not in o.shapes:continue
        rotated=engines[index%3:]+engines[:index%3]
        order.extend(dict(shape=shape,engine=e) for e in rotated)
    protocol=dict(server=b.SERVER_TYPE,client='c7g.16xlarge',zone=o.zone,cpu_quota=8,memory='8Gi',workers=8,
        persistence=False,partitions=256,unpinned=True,shapes={name:SHAPES[name] for name in o.shapes},order=order,
        connections=32,pipeline=16,client_gomaxprocs=16,client_cpu_quota=48,
        warmup_seconds=10,measure_seconds=60,repeats=3,
        mix='approximately 80% ZINCRBY / 20% ZRANK; deterministic modular key/member sequences',
        mixed='24 hot connections; 8 cold connections across the other 63 sets; achieved operation shares vary',
        latency='closed-loop full-pipeline round trip; not per-command service latency',
        client_gate='max sampled thread average <0.9 cores, aggregate <12.8 cores, throttling <1% duration',
        network_gate='zero ENA allowance-exceeded deltas',
        binary_sha256=hashlib.sha256(o.binary.read_bytes()).hexdigest(),
        client_sha256=hashlib.sha256(o.client.read_bytes()).hexdigest())
    b.save(o.output/'protocol.json',protocol)
    b.save(o.output/'engines.json',json.loads((b.SOURCE/'engines.json').read_text()))
    for name in ('check.py','driver_wrapper.py'):
        b.save(o.output/name,CHECK if name=='check.py' else WRAPPER)
    for path in pathlib.Path(__file__).parent.glob('*.py'):
        dest=o.output/'source'/path.name;dest.parent.mkdir(exist_ok=True);shutil.copy2(path,dest)
    for name in ('src/server/worker.zig','src/server/tcp.zig','src/server/adaptive_owner.zig','src/engine/types/sorted_set.zig','bench/loadtest/diagnostics/key_owner.go','tests/integration/sorted_set_owner.py','tests/integration/sorted_set_concurrency.py'):
        dest=o.output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(b.ROOT/name,dest)
    return protocol


def resume(o):
    protocol=json.loads((o.output/'protocol.json').read_text())
    resources=json.loads((o.output/'resources.json').read_text())
    assert resources['items'][0]['metadata']['name']==o.namespace
    assert protocol['zone']==o.zone
    b.BUILD=o.output/'frozen'
    assert hashlib.sha256((b.BUILD/'static-after/bin/vex').read_bytes()).hexdigest()==protocol['binary_sha256']
    assert hashlib.sha256((o.output/'key-owner-client').read_bytes()).hexdigest()==protocol['client_sha256']
    initial=json.loads((o.output/'nodes.json').read_text())
    current=json.loads(s.kub('get','nodes','-l','bench.vex.dev/run='+o.namespace,'-o','json'))
    identity=lambda obj:{(n['metadata']['uid'],n['status']['nodeInfo']['bootID'],n['spec']['providerID']) for n in obj['items']}
    assert len(current['items'])==2 and identity(current)==identity(initial),'benchmark hosts changed'
    for item in resources['items']:
        live=json.loads(s.kub('get',item['kind'],item['metadata']['name'],'-o','json'))
        assert live['metadata']['labels']['bench.vex.dev/run']==o.namespace
        assert not live['metadata'].get('deletionTimestamp'),'cleanup already underway'
    old=json.loads((b.SOURCE/'resources.json').read_text())['items'][0]['metadata']['name']
    o.template=json.loads((b.SOURCE/'server-candidate-16.json').read_text().replace(old,o.namespace))
    o.template['spec']['containers'][0]['image']=o.template['spec']['containers'][1]['image']
    stamp=str(time.time_ns())
    for cell in protocol['order']:
        path=o.output/(cell['shape']+'-'+cell['engine'])
        if path.exists() and not (path/'complete.json').exists():
            archive=o.output/'interrupted';archive.mkdir(exist_ok=True)
            path.rename(archive/(path.name+'-'+stamp))
    snapshot=o.output/('resume-source-'+stamp);snapshot.mkdir()
    for name in ('compare_adaptive_aws.py','compare_sorted_set_scaling.py','test_adaptive_aws.py'):
        shutil.copy2(pathlib.Path(__file__).parent/name,snapshot/name)
    return protocol


def phase(o,out,host,engine,topology,shape,label,seconds):
    boards,members=SHAPES[shape]
    remote='/results/'+out.name+'-'+label
    command=['env','GOMAXPROCS=16','python3','/results/driver_wrapper.py',remote,
             '/results/key-owner-client','-host',host+':6379','-seconds',str(seconds),'-sets',str(boards),'-members',str(members)]
    if shape=='mixed':command+=['-mixed']
    if shape.startswith('uniform'):command+=['-uniform']
    before=s.snapshot_server(engine['process_name']);cb=b.client_snapshot()
    result=b.remote_job(command,remote+'-job',seconds+180)
    b.save(out/(label+'.stderr'),result.stderr)
    assert result.returncode==0,(result.stderr,result.stdout[-1000:])
    raw=json.loads(result.stdout)
    with gzip.open(out/(label+'.raw.json.gz'),'wt') as f:f.write(result.stdout)
    _,after=s.assert_final_topology(topology,engine);ca=b.client_snapshot()
    assert cb['boot']==ca['boot'];s.assert_memory(before,after);s.assert_memory(cb,ca)
    for name,value in [('command',command),('server-before',before),('server-after',after),('client-before',cb),('client-after',ca)]:b.save(out/(label+'.'+name+'.json'),value)
    row=summarize(raw,before,after,cb,ca);row['label']=label
    assert seconds<=row['seconds']<=seconds+10
    b.save(out/(label+'.summary.json'),row)
    print(out.name,label,round(row['ops_s']),'p99',row['hot_batch_p99_ms'],'client_valid',row['client_valid'],flush=True)
    return row


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--binary',type=pathlib.Path,required=True)
    p.add_argument('--client',type=pathlib.Path,required=True)
    p.add_argument('--namespace',required=True)
    p.add_argument('--zone',default='ap-south-1b')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--shapes',nargs='+',choices=list(SHAPES),default=list(SHAPES))
    o=p.parse_args();o.output=o.output.resolve()
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws','sts','get-caller-identity','--output','json']).stdout)['Account']=='208168340597'
    s.BASE,s.NS=o.output,o.namespace;s.K=['kubectl','--context',s.CTX,'-n',o.namespace]
    if o.resume:
        protocol=resume(o)
    else:
        for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
            assert not s.kub('get',kind,name,'--ignore-not-found','-o','name').strip()
        protocol=prepare(o)
    try:
        if o.resume:
            # Destroy any detached interrupted client before reusing remote paths.
            s.kub('delete','pod','client','--ignore-not-found','--wait=true','--timeout=60s')
            s.kub('create','-f',str(o.output/'client-48.json'))
        else:
            for name in ('resources.json','holder.manifest.json','client-48.json'):s.kub('create','-f',str(o.output/name))
        s.kub('wait','--for=condition=Ready','pod/client','--timeout=600s',timeout=610)
        for name in ('check.py','driver_wrapper.py','key-owner-client'):
            s.kub('cp',str(o.output/name),'client:/results/'+name,'-c','memtier')
        s.ex('client','memtier','chmod','755','/results/key-owner-client')
        assert s.ex('client','memtier','sha256sum','/results/key-owner-client').split()[0]==protocol['client_sha256']
        for number,cell in enumerate(protocol['order'],1):
            shape,version=cell['shape'],cell['engine'];boards,members=SHAPES[shape]
            out=o.output/(shape+'-'+version)
            if (out/'complete.json').exists():
                assert o.resume
                print('RETAIN',number,len(protocol['order']),out.name,flush=True)
                continue
            out.mkdir()
            print('START',number,len(protocol['order']),out.name,flush=True)
            host,engine,topology,instances=b.start(o,8,version,out,partitions=256 if version=='after' else None,adaptive=version=='after')
            b.save(out/'startup.log',s.kub('logs','server','-c','engine'))
            preload=b.remote_job(['python3','/results/check.py',host,'preload',str(boards),str(members)],'/results/preload-'+out.name,600)
            b.save(out/'preload.json',preload.stdout);assert preload.returncode==0,preload.stderr
            before=b.ena(out,'before',instances)
            rows=[phase(o,out,host,engine,topology,shape,'warmup',10)]
            for repeat in (1,2,3):rows.append(phase(o,out,host,engine,topology,shape,'repeat-'+str(repeat),60))
            after=b.ena(out,'after',instances)
            deltas={role:{k:after[role][k]-v for k,v in counts.items()} for role,counts in before.items()}
            verify=b.remote_job(['python3','/results/check.py',host,'verify',str(boards),str(members),str(sum(r['writes'] for r in rows))],'/results/verify-'+out.name,600)
            b.save(out/'verify.json',verify.stdout);assert verify.returncode==0,verify.stderr
            logs=s.kub('logs','server','-c','engine');b.save(out/'final.log',logs)
            b.save(out/'complete.json',dict(**cell,rows=rows,network_deltas=deltas,verified=True,
                valid=all(r['client_valid'] for r in rows) and all(v==0 for counts in deltas.values() for v in counts.values())))
            print('COMPLETE',number,len(protocol['order']),out.name,flush=True)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        b.save(o.output/f'cleanup-request-{time.time_ns()}.log',result.stdout+result.stderr)
        print('CLEANUP',result.returncode,flush=True)


if __name__=='__main__':main()
