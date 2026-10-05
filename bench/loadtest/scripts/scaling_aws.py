#!/usr/bin/env python3
"""CPU-quota scaling with explicit offered load, scheduled latency, and client gates."""
import argparse
import copy
import hashlib
import json
import pathlib
import subprocess
import time

import compare_aws_engines as s
from realistic_aws import client_snapshot, engine_order
from realistic_workloads import validate_result

HERE = pathlib.Path(__file__).resolve().parent


def passing(row):
    return (row['client_valid'] and row['slo_violation_fraction'] <= .01
            and row['completed_in_window'] >= .99 * row['offered']
            and row['errors'] == 0 and row['uncompleted'] == 0)


def latency_budget(row, milliseconds):
    """Score offered requests; histogram cutoffs conservatively include equality."""
    assert milliseconds in (1, 2, 5)
    assert len(row['latency_histogram_10us']) == 10001 and sum(row['latency_histogram_10us']) == row['completed']
    late = row['over_1ms'] if milliseconds == 1 else sum(row['latency_histogram_10us'][milliseconds*100:])
    fraction = (late + row['uncompleted']) / row['offered']
    return {'over_budget_or_uncompleted_fraction': fraction,
            'pass': row['client_valid'] and fraction <= .01 and row['errors'] == 0
                    and row['uncompleted'] == 0 and row['completed_in_window'] >= .99*row['offered']}


def prepare(o):
    o.output.mkdir(parents=True, exist_ok=False)
    s.BASE, s.NS = o.output, o.namespace
    s.K = ['kubectl', '--context', s.CTX, '-n', o.namespace]
    source = o.source
    old = json.loads((source/'resources.json').read_text())['items'][0]['metadata']['name']
    engines = json.loads((source/'engines.json').read_text())
    s.write(o.output/'engines.json', engines)
    for name in ('resources.json', 'client.manifest.json', 'holder.manifest.json'):
        data = json.loads((source/name).read_text().replace(old, o.namespace))
        if name == 'resources.json':
            server = data['items'][1]['spec']
            server['limits'] = {'cpu': '32', 'memory': '64Gi'}
            for req in server['template']['spec']['requirements']:
                if req['key'] == 'node.kubernetes.io/instance-type':
                    req['values'] = ['c7g.8xlarge']
        s.write(o.output/name, data)
    for cpu in o.cpus:
        for name, item in engines.items():
            data = json.loads((source/f'server-{name}.manifest.json').read_text().replace(old,o.namespace))
            engine = data['spec']['containers'][0]
            engine['resources'] = {k: {'cpu': str(cpu), 'memory': '4Gi'} for k in ('requests','limits')}
            cmd = engine['command']
            if name == 'candidate': cmd[cmd.index('--workers')+1] = str(cpu)
            elif name == 'redis': cmd[cmd.index('--io-threads')+1] = str(cpu)
            else: engine['command'] = [f'--proactor_threads={cpu}' if x.startswith('--proactor_threads=') else x for x in cmd]
            s.write(o.output/f'server-{name}-{cpu}.json', data)
    s.write(o.output/'protocol.json', {**{k:str(v) if isinstance(v,pathlib.Path) else v for k,v in vars(o).items()},
        'cpu_scaling':'equal CPU quota and worker count on one unchanged c7g.8xlarge server; not an instance-price comparison',
        'slo':'at most 1% of all offered requests >1ms or uncompleted; zero errors/loss; >=99% completed inside arrival window',
        'client_gate':'dispatch p99 <=0.25ms, CPU <80% of 24 cores, throttling <1% wall time',
        'workload':'100k preloaded 256-byte values; random uniform 80% GET/20% SET; no TTL; 1024 connections; pipeline 1',
        'started_unix':time.time(), 'binary_sha256':hashlib.sha256(o.binary.read_bytes()).hexdigest()})
    for path in (HERE/'scaling_aws.py', HERE/'compare_aws_engines.py', HERE/'realistic_aws.py', *list((HERE.parent/'openloop').glob('*.go'))):
        s.write(o.output/'source'/path.name,path.read_text())
    return engines


def remote_job(command, remote, timeout):
    """Persist long-running output on the client; exec-stream loss cannot kill a trial."""
    def control(args, seconds):
        try:
            return s.run(args, timeout=seconds, check=False)
        except subprocess.TimeoutExpired:
            # The remote process may still be running. Preserve its unique job
            # directory and retry observation, never the workload launch.
            return subprocess.CompletedProcess(args, 124, '', 'control-plane request timed out')

    worker = """import json,pathlib,subprocess,sys,os
p=pathlib.Path(sys.argv[1]);command=json.loads(sys.argv[2])
env=dict(os.environ,GOMAXPROCS='20')
with (p/'stdout').open('w') as out,(p/'stderr').open('w') as err:
 r=subprocess.run(command,stdout=out,stderr=err,env=env)
(p/'exit').write_text(str(r.returncode))
"""
    launch = """import pathlib,subprocess,sys
p=pathlib.Path(sys.argv[1])
try:p.mkdir(parents=True,exist_ok=False)
except FileExistsError:sys.exit(0)
subprocess.Popen(['python3','-c',sys.argv[2],str(p),sys.argv[3]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
"""
    result=control(s.K+['exec','client','-c','memtier','--','python3','-c',launch,remote,worker,json.dumps(command)],60)
    # Launch might have succeeded even if its response was lost. Poll the unique
    # result directory instead of launching a duplicate workload.
    limit=time.monotonic()+timeout
    poll="import pathlib,sys;p=pathlib.Path(sys.argv[1])/'exit';print(p.read_text() if p.exists() else 'running')"
    while time.monotonic()<limit:
        status=control(s.K+['exec','client','-c','memtier','--','python3','-c',poll,remote],30)
        if status.returncode==0 and status.stdout.strip()!='running':
            code=int(status.stdout.strip())
            values=[]
            for name in ('stdout','stderr'):
                for attempt in range(4):
                    value=control(s.K+['exec','client','-c','memtier','--','cat',remote+'/'+name],60)
                    if value.returncode==0:break
                    time.sleep(2)
                assert value.returncode==0,value.stderr
                values.append(value.stdout)
            return subprocess.CompletedProcess(command,code,values[0],values[1])
        time.sleep(2)
    raise RuntimeError('remote job did not finish/export within its bound: '+remote+'; launch: '+result.stderr+'; last poll: '+status.stderr)


def phase(o, cell, host, engine, topology, rate, seconds, label):
    out = cell/label
    out.mkdir()
    before=s.snapshot_server(engine['process_name']); cb=client_snapshot()
    s.write(out/'server-before.json',before);s.write(out/'client-before.json',cb)
    command=['/results/openloop','-host',host+':6379','-rate',str(rate),'-seconds',str(seconds)]
    command += getattr(o, 'driver_args', [])
    if hasattr(o, 'gomaxprocs'):
        command = ['env', 'GOMAXPROCS='+str(o.gomaxprocs), *command]
    s.write(out/'command.json',command)
    result=remote_job(command,'/results/scaling/'+o.output.name+'/'+cell.name+'/'+label,seconds+120)
    s.write(out/'stdout.json',result.stdout);s.write(out/'stderr.log',result.stderr)
    assert result.returncode==0,result.stderr
    row=json.loads(result.stdout)
    ca=client_snapshot(); final,after=s.assert_final_topology(topology,engine)
    s.write(out/'server-after.json',after);s.write(out/'client-after.json',ca);s.write(out/'topology-after.json',final)
    s.assert_memory(before,after);s.assert_memory(cb,ca);assert cb['boot']==ca['boot']
    assert row['offered']==rate*seconds and row['completed']+row['uncompleted']==row['offered']
    assert row['enqueued']+row['dropped']==row['offered'] and row['sent']<=row['enqueued']
    for who,a,b in [('server',before,after),('client',cb,ca)]:
        delta=s.aws.sat.cpu_delta(a['cgroup']['cpu'],b['cgroup']['cpu'])
        elapsed=b['monotonic']-a['monotonic']
        row[who+'_cpu']={'cores':delta['usage_usec']/1e6/elapsed,'throttled_fraction':delta['throttled_usec']/1e6/elapsed,
            'cores_upper_bound_in_window':delta['usage_usec']/1e6/seconds,
            'throttled_upper_bound_in_window':delta['throttled_usec']/1e6/seconds,'delta':delta}
    quota,period=map(int,cb['cgroup']['cpu_max'].split())
    assert quota/period==getattr(o,'client_cpus',24) and cb['cgroup']['cpu_max']==ca['cgroup']['cpu_max']
    row['client_cpu_quota']=quota/period
    row['client_valid']=(isinstance(row['dispatch_p99_ms'],(int,float)) and row['dispatch_p99_ms']<=.25
                         and row['client_cpu']['cores_upper_bound_in_window']<.8*row['client_cpu_quota'] and row['client_cpu']['throttled_upper_bound_in_window']<.01)
    row['pass']=passing(row)
    if hasattr(o, 'latency_budgets'):
        row['latency_budgets']={str(ms):latency_budget(row,ms) for ms in o.latency_budgets}
        row['primary_budget_ms']=o.primary_budget_ms
        row['pass']=row['latency_budgets'][str(o.primary_budget_ms)]['pass']
        row['one_ms_violation_fraction']=row['slo_violation_fraction']
        row['slo_violation_fraction']=row['latency_budgets'][str(o.primary_budget_ms)]['over_budget_or_uncompleted_fraction']
    for key in list(row):
        if key.endswith('histogram_10us'): del row[key] # retained in stdout.json
    row.pop('connections',None) # detailed connection output remains in stdout.json
    row.update(rate=rate,seconds=seconds,label=label,peak_rss_mib=after['process']['hwm_bytes']/2**20)
    s.write(out/'summary.json',row)
    print('PHASE',cell.name,label,'rate',rate,'p99',row['p99_ms'],'dispatch',row['dispatch_p99_ms'],'pass',row['pass'],'client_valid',row['client_valid'],flush=True)
    return row


def uncapped(o, out, host):
    target=out/'uncapped';target.mkdir()
    assert s.memtier_hash()==s.MEMTIER_SHA256
    command=['memtier_benchmark','-s',host,'-p','6379','-t','32','-c','32',
        '--pipeline','1','--ratio','1:4','--key-prefix','vex:scale:',
        '--key-minimum','1','--key-maximum','100000','--key-pattern','R:R',
        '--data-size','256','--distinct-client-seed','--test-time','10',
        '--hide-histogram','--print-percentiles','50,95,99,99.9',
        '--json-out-file','/results/uncapped.json']
    s.write(target/'command.json',command)
    result=remote_job(command,'/results/scaling/'+o.output.name+'/'+out.name+'/uncapped',120)
    assert result.returncode==0,result.stderr
    s.write(target/'stdout.log',result.stdout);s.write(target/'stderr.log',result.stderr)
    data=json.loads(s.ex('client','memtier','cat','/results/uncapped.json'))
    s.write(target/'raw.json',data)
    cfg=data['configuration']
    assert cfg['rate_limit']==0 and cfg['threads']==32 and cfg['clients']==32 and cfg['pipeline']==1
    row=validate_result(data,result.stderr,10,0,False)
    row['client_headroom_ok']=(max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values())<.9
                               and row['client_cpu']['cpu_cores_used']<19.2)
    s.write(target/'summary.json',row)
    print('UNCAPPED',out.name,'ops',round(row['ops_per_sec']),'p99',row['p99_ms'],flush=True)
    return row


def cell(o, engines, name, cpu, index):
    out=o.output/'cells'/f'{cpu}cpu-{name}';out.mkdir(parents=True)
    manifest=json.loads((o.output/f'server-{name}-{cpu}.json').read_text())
    engine=copy.deepcopy(engines[name]);engine['command']=manifest['spec']['containers'][0]['command']
    s.kub('delete','pod','server','--ignore-not-found','--wait=true','--timeout=60s')
    s.kub('create','-f',str(o.output/f'server-{name}-{cpu}.json'))
    s.kub('wait','--for=condition=Ready','pod/server','--timeout=180s',timeout=190)
    nodes=json.loads(s.kub('get','nodes','-l',f'bench.vex.dev/run={o.namespace}','-o','json'))
    initial=json.loads((o.output/'nodes-active.json').read_text())
    assert {n['metadata']['uid'] for n in nodes['items']}=={n['metadata']['uid'] for n in initial['items']}
    server,client=s.validate_pods(engine,nodes,server_cpu=cpu,server_machine='c7g.8xlarge')
    identity=s.snapshot_server(engine['process_name']);assert identity['exe_sha256']==engine['binary_sha256']
    quota,period=map(int,identity['cgroup']['cpu_max'].split());assert quota/period==cpu
    topology=s.live_topology(server,client,identity)
    s.write(out/'server-pod.json',server);s.write(out/'client-pod.json',client);s.write(out/'topology.json',topology)
    host=server['status']['podIP'];backend='vex' if name=='candidate' else name
    s.postload_checks(backend,host,out,'engine-before.json',io_threads=cpu)
    preload=remote_job(['/results/openloop','-host',host+':6379','-preload','100000'],'/results/scaling/'+o.output.name+'/'+out.name+'/preload',180)
    s.write(out/'preload.stdout',preload.stdout);s.write(out/'preload.stderr',preload.stderr)
    assert preload.returncode==0,preload.stderr
    phase(o,out,host,engine,topology,16000,5,'warmup')
    rows=[];low=0;high=None;rate=getattr(o,'starting_rates',{}).get(name,32000)
    for stage in range(9):
        row=phase(o,out,host,engine,topology,rate,o.seconds,f'ramp-{stage}')
        rows.append(row)
        if not row['client_valid']:
            # Never label a driver-limited result as database saturation.
            high=rate;break
        if not row['pass']:high=rate;break
        low=rate;rate=getattr(o,'upper_hints',{}).get(name,rate*2) if stage==0 else rate*2
        if rate>o.max_rate:break
    if high and not low and all(r['client_valid'] for r in rows):
        for lower in dict.fromkeys([max(1000,rate//(2**step)) for step in range(1,9)]+[1000]):
            row=phase(o,out,host,engine,topology,lower,o.seconds,f'lower-{lower}');rows.append(row)
            if not row['client_valid']:break
            if row['pass']:low=lower;break
            high=lower
    if high and low:
        for step in range(2):
            mid=((low+high)//2//1000)*1000
            row=phase(o,out,host,engine,topology,mid,o.seconds,f'refine-{step}');rows.append(row)
            if not row['client_valid']:
                high=mid
                continue
            if row['pass']:low=mid
            else:high=mid
    exploratory=low;confirmations=[];confirmation_history=[]
    if low:
        for reduction in range(3):
            confirmations=[]
            for attempt in range(3):
                row=phase(o,out,host,engine,topology,low,o.confirm_seconds,f'confirm-{reduction}-{attempt}');confirmations.append(row)
            confirmation_history.extend(confirmations)
            if all(r['pass'] for r in confirmations) or reduction==2:break
            lower=[r['rate'] for r in rows if r['pass'] and r['rate']<low]
            low=max(lower) if lower else max(1000,(low*8//10//1000)*1000)

    boundary=phase(o,out,host,engine,topology,high,o.confirm_seconds,'boundary-check') if high else None
    peak=uncapped(o,out,host)
    s.postload_checks(backend,host,out,'engine-after.json',io_threads=cpu)
    valid=all(r['client_valid'] for r in confirmations+([boundary] if boundary else rows[-1:]))
    result={'engine':name,'cpus':cpu,'highest_exploratory_pass_rate':exploratory,'first_failing_or_client_limited_rate':high,
            'confirmed_rate':low if len(confirmations)==3 and all(r['pass'] for r in confirmations) else None,
            'client_valid':valid,'classification':'client-limited/inconclusive' if not valid else 'unstable boundary' if boundary and boundary['pass'] else 'bracketed' if high else 'lower bound only',
            'phases':rows,'confirmations':confirmations,'confirmation_history':confirmation_history,'uncapped_crosscheck':peak,'boundary_check':boundary}
    s.write(out/'complete.json',result)
    print('COMPLETE',out.name,'confirmed',result['confirmed_rate'],'classification',result['classification'],flush=True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--namespace',required=True);p.add_argument('--binary',type=pathlib.Path,required=True)
    p.add_argument('--cpus',type=int,nargs='+',default=[1,2,4,8,16])
    p.add_argument('--engines',nargs='+',default=['candidate','redis','dragonfly'],choices=['candidate','redis','dragonfly'])
    p.add_argument('--seconds',type=int,default=10);p.add_argument('--confirm-seconds',type=int,default=30)
    p.add_argument('--max-rate',type=int,default=4096000)
    p.add_argument('--seed-run',type=pathlib.Path,help='Prior completed cells supply search hints only; every result is measured again')
    o=p.parse_args();o.output=o.output.resolve()
    assert o.namespace.startswith('vex-scaling-') and set(o.cpus)<=set([1,2,4,8,16])
    assert min(o.seconds,o.confirm_seconds,o.max_rate)>0
    seeds={}
    if o.seed_run:
        for f in o.seed_run.glob('cells/*/complete.json'):
            row=json.loads(f.read_text())
            if row['confirmed_rate']:
                seeds[(row['cpus'],row['engine'])]=(row['confirmed_rate'],row['first_failing_or_client_limited_rate'])
    engines=prepare(o)
    for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
        assert not s.kub('get',kind,name,'--ignore-not-found','-o','name').strip()
    try:
        s.kub('create','-f',str(o.output/'resources.json'))
        s.kub('create','-f',str(o.output/'holder.manifest.json'));s.kub('create','-f',str(o.output/'client.manifest.json'))
        print('Waiting for dedicated nodes',flush=True)
        s.kub('wait','--for=condition=Ready','pod/client','pod/server-holder','--timeout=600s',timeout=610)
        s.write(o.output/'nodes-active.json',json.loads(s.kub('get','nodes','-l',f'bench.vex.dev/run={o.namespace}','-o','json')))
        s.kub('cp',str(o.binary),'client:/results/openloop','-c','memtier',timeout=120)
        s.ex('client','memtier','chmod','755','/results/openloop')
        live=s.ex('client','memtier','sha256sum','/results/openloop').split()[0]
        assert live==hashlib.sha256(o.binary.read_bytes()).hexdigest()
        rows=[];o.starting_rates={};o.upper_hints={}
        for index,cpu in enumerate(o.cpus):
            for name in engine_order(o.engines,1,index):
                if (cpu,name) in seeds:
                    low,high=seeds[(cpu,name)];o.starting_rates[name]=low
                    if high:o.upper_hints[name]=high
                else:o.upper_hints.pop(name,None)
                result=cell(o,engines,name,cpu,index);rows.append(result)
                if result['confirmed_rate']:o.starting_rates[name]=result['confirmed_rate']
        s.write(o.output/'summary.json',rows)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        s.write(o.output/'cleanup-request.log',result.stdout+result.stderr)
        print('Cleanup request exit:',result.returncode,flush=True)

if __name__=='__main__':main()
