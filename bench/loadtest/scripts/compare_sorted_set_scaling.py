#!/usr/bin/env python3
"""Paired frozen-binary sorted-set scaling on disposable, isolated AWS nodes."""
import argparse
import hashlib
import json
import pathlib
import time

import compare_aws_engines as s
from dragonfly_hypotheses import ZSET_CHECK, trace
from realistic_aws import client_snapshot
from realistic_workloads import validate_result
from scaling_aws import remote_job

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'bench/loadtest/fixtures/aws-scaling'
BUILD = ROOT / 'bench/loadtest/runs/2026-09-27-sorted-set-sharding'
SERVER_TYPE = 'c6gn.8xlarge'
ENA_COMMAND = json.loads((SOURCE / 'source/ena-read-command.json').read_text())


def save(path, value):
    s.write(path, value)


def ena(out, phase, instances):
    cmd = json.loads(s.run(['aws', 'ssm', 'send-command', '--region', 'ap-south-1',
        '--document-name', 'AWS-RunShellScript', '--instance-ids', *instances.values(),
        '--parameters', json.dumps(ENA_COMMAND), '--comment', 'Read-only benchmark ENA allowance counters', '--output', 'json']).stdout)
    save(out / (phase + '-ena-command.json'), cmd)
    values = {}
    for role, instance in instances.items():
        for _ in range(30):
            result = s.run(['aws', 'ssm', 'get-command-invocation', '--region', 'ap-south-1',
                '--command-id', cmd['Command']['CommandId'], '--instance-id', instance, '--output', 'json'], check=False)
            if result.returncode == 0:
                row = json.loads(result.stdout)
                if row['Status'] == 'Success':
                    break
                assert row['Status'] not in ('Failed', 'Cancelled', 'TimedOut'), row
            time.sleep(2)
        else:
            raise RuntimeError('ENA snapshot did not finish')
        save(out / (phase + '-ena-' + role + '.json'), row)
        counters = {}
        for line in row['StandardOutputContent'].splitlines():
            parts = line.split()
            if len(parts) == 2 and 'allowance_exceeded' in parts[0]:
                counters[parts[0].rstrip(':')] = int(parts[1])
        assert counters, row
        values[role] = counters
    return values


def prepare(o):
    o.output.mkdir(parents=True, exist_ok=False)
    old = json.loads((SOURCE / 'resources.json').read_text())['items'][0]['metadata']['name']
    for name in ('resources.json', 'holder.manifest.json', 'client-48.json'):
        obj = json.loads((SOURCE / name).read_text().replace(old, o.namespace))
        if name == 'resources.json':
            for req in obj['items'][1]['spec']['template']['spec']['requirements']:
                if req['key'] == 'node.kubernetes.io/instance-type':
                    req['values'] = [SERVER_TYPE]
        if name == 'resources.json':
            for item in obj['items'][1:]:
                for req in item['spec']['template']['spec']['requirements']:
                    if req['key'] == 'topology.kubernetes.io/zone':
                        req['values'] = [getattr(o, 'zone', 'ap-south-1a')]
        save(o.output / name, obj)
    o.template = json.loads((SOURCE / 'server-candidate-16.json').read_text().replace(old, o.namespace))
    # The pinned Python runtime has a shell. Static binaries need no image libc.
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    hashes=[]
    for version in ('before', 'after'):
        path = BUILD / ('static-' + version) / 'bin/vex'
        assert path.is_file(), path
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(set(hashes))==2, 'baseline and candidate binaries must differ'
    protocol = dict(zone=getattr(o, 'zone', 'ap-south-1a'), server=SERVER_TYPE, client='c7g.16xlarge',
        cpu_quotas=getattr(o, 'cpus', [4, 8, 16]), workers='same as CPU quota, unpinned', memory='8Gi',
        baseline='indexed sorted-set candidate immediately before partitioning',
        dataset='64 sorted sets x 4096 existing members', mixed='80% ZINCRBY / 20% ZRANK',
        arrival_model='uncapped closed-loop concurrency sweep; service latency, not scheduled-arrival latency',
        search='32 threads x 1, 4, 16, 64 connections, pipeline 4; 15 seconds each',
        confirmation='three 60-second repetitions at the fastest tested passing concurrency; lower concurrency if confirmation fails',
        primary_budget_ms=5, pure_update_control='three 30-second runs, 32 x 32 connections, pipeline 16',
        client_gate='max thread <0.9 cores, aggregate <80% of 48-CPU quota; no OOM or restarts',
        network_gate='zero increase in server/client ENA allowance-exceeded counters across each cell',
        binaries={v: hashlib.sha256((BUILD / ('static-' + v) / 'bin/vex').read_bytes()).hexdigest() for v in ('before','after')})
    if o.three_engines:
        inventory = json.loads((SOURCE / 'engines.json').read_text())
        protocol.update(engines=['redis', 'dragonfly', 'after'],
            baseline='equal-resource Redis, Dragonfly and frozen partitioned Vex candidate',
            binaries={**{k: inventory[k]['binary_sha256'] for k in ('redis', 'dragonfly')},
                      'after': protocol['binaries']['after']},
            mixed_pressure_control='three 60-second runs, 32 x 32 connections, pipeline 16; no latency qualification',
            search='32 threads x 1, 4, 16, 64 connections, pipelines 4 and 16; 15 seconds each',
            confirmation='three 60-second repetitions at the fastest tested passing setting; next passing setting if confirmation fails')
        save(o.output / 'engines.json', inventory)
    save(o.output / 'protocol.json', protocol)
    save(o.output / 'runner.py', pathlib.Path(__file__).read_text())


def start(o, cpu, version, out, *, partitions=None, adaptive=False):
    s.kub('delete', 'pod', 'server', '--ignore-not-found', '--wait=true', '--timeout=60s')
    manifest = json.loads(json.dumps(o.template))
    primary = manifest['spec']['containers'][0]
    primary['command'] = ['/bin/sh', '-c', f'while [ ! -f /results/ready ]; do sleep 1; done; exec /results/vex --reactor --workers {cpu} --no-persistence --port 6379']
    if partitions is not None:
        assert version not in ('redis', 'dragonfly')
        assert 1 <= partitions <= 4096 and partitions & (partitions - 1) == 0
        primary['command'][-1] += f' --sorted-set-partitions {partitions}'
    primary['env'] = [{'name': 'VEX_PIN_WORKERS', 'value': '0'}]
    if adaptive:
        assert version not in ('redis', 'dragonfly')
        primary['env'].append({'name': 'VEX_EXPERIMENTAL_ADAPTIVE_OWNER', 'value': '1'})
    external = version in ('redis', 'dragonfly')
    if external:
        original = json.loads((SOURCE / f'server-{version}-16.json').read_text())['spec']['containers'][0]
        primary['image'] = original['image']
        primary['command'] = original['command']
        if version == 'redis':
            primary['command'][primary['command'].index('--io-threads') + 1] = str(cpu)
        else:
            primary['command'] = [f'--proactor_threads={cpu}' if x.startswith('--proactor_threads=') else x for x in primary['command']]
        primary.pop('env', None)
    primary['resources'] = {k: {'cpu':str(cpu), 'memory':'8Gi'} for k in ('requests','limits')}
    save(out / 'server.json', manifest)
    s.kub('create', '-f', str(out / 'server.json'))
    for _ in range(90):
        pod = s.pod('server')
        if any(x['name']=='sampler' and x.get('state',{}).get('running') for x in pod.get('status',{}).get('containerStatuses',[])):
            break
        time.sleep(2)
    else:
        raise RuntimeError('server sampler did not start')
    if not external:
        binary = BUILD / ('static-' + version) / 'bin/vex'
        # Engine waits for /results/ready; overwriting a partial copy is safe.
        s.kub('cp', str(binary), 'server:/results/vex', '-c', 'sampler', retry_safe=True)
        s.ex('server', 'sampler', 'chmod', '755', '/results/vex')
        assert s.ex('server','sampler','sha256sum','/results/vex').split()[0] == hashlib.sha256(binary.read_bytes()).hexdigest()
        s.ex('server','sampler','touch','/results/ready')
    s.kub('wait','--for=condition=Ready','pod/server','--timeout=180s',timeout=190)
    nodes = json.loads(s.kub('get','nodes','-l','bench.vex.dev/run='+o.namespace,'-o','json'))
    if not (o.output/'nodes.json').exists():
        save(o.output/'nodes.json',nodes)
    initial = json.loads((o.output/'nodes.json').read_text())
    boots = lambda ns: {(x['metadata']['uid'],x['status']['nodeInfo']['bootID']) for x in ns['items']}
    assert boots(nodes)==boots(initial)
    engine = dict(image=primary['image'], command=primary['command'], process_name='vex', binary_sha256=None if external else hashlib.sha256(binary.read_bytes()).hexdigest())
    if external:
        expected = json.loads((SOURCE / 'engines.json').read_text())[version]
        engine.update(process_name=expected['process_name'], binary_sha256=expected['binary_sha256'])
    save(out / 'engine.json', engine)
    server,client = s.validate_pods(engine,nodes,server_cpu=cpu,server_machine=SERVER_TYPE,client_cpu=48,client_machine='c7g.16xlarge',server_memory='8Gi',zone=getattr(o, 'zone', 'ap-south-1a'))
    identity=s.snapshot_server(engine['process_name'])
    assert identity['exe_sha256']==engine['binary_sha256']
    if not external:
        assert all(len(t['affinity'])>1 for t in identity['process']['tasks'])
    quota,period=map(int,identity['cgroup']['cpu_max'].split());assert quota/period==cpu
    topology=s.live_topology(server,client,identity)
    save(out/'identity.json',identity);save(out/'topology.json',topology)
    instances={role:topology[role+'_node']['provider_id'].split('/')[-1] for role in ('server','client')}
    return server['status']['podIP'],engine,topology,instances


def phase(o,out,host,engine,topology,profile,label,seconds,clients,pipeline):
    remote='/results/'+o.output.name+'/'+out.name+'/'+label
    s.ex('client','memtier','mkdir','-p',remote)
    cmd=['memtier_benchmark','-s',host,'-p','6379','-t','32','-c',str(clients),'--pipeline',str(pipeline),
         '--test-time',str(seconds),'--distinct-client-seed','--hide-histogram','--print-percentiles','50,95,99,99.9',
         '--monitor-input','/results/'+profile+'.trace','--command','__monitor_line@__','--monitor-pattern','R',
         '--json-out-file',remote+'/result.json']
    before=s.snapshot_server(engine['process_name']);cb=client_snapshot()
    save(out/(label+'.command.json'),cmd)
    result=remote_job(cmd,remote+'/job',seconds+120)
    save(out/(label+'.stdout'),result.stdout);save(out/(label+'.stderr'),result.stderr)
    assert result.returncode==0,result.stderr
    raw=json.loads(s.ex('client','memtier','cat',remote+'/result.json'))
    save(out/(label+'.raw.json'),raw)
    row=validate_result(raw,result.stderr,seconds,0,False)
    cfg=raw['configuration']
    expected=dict(server=host,port=6379,threads=32,clients=clients,pipeline=pipeline,rate_limit=0)
    assert all(cfg.get(k)==v for k,v in expected.items()),cfg
    _,after=s.assert_final_topology(topology,engine);ca=client_snapshot()
    s.assert_memory(before,after);s.assert_memory(cb,ca);assert cb['boot']==ca['boot']
    for name,value in [('server-before',before),('server-after',after),('client-before',cb),('client-after',ca)]:
        save(out/(label+'.'+name+'.json'),value)
    stats=raw['ALL STATS'];count=stats['Totals']['Count']
    writes=sum(v['Count'] for k,v in stats.items() if k.lower().rstrip('s')=='zincrby')
    ranks=sum(v['Count'] for k,v in stats.items() if k.lower().rstrip('s')=='zrank')
    assert count==writes+ranks
    assert .19<ranks/count<.21 if profile=='mixed' else ranks==0
    row.update(label=label,clients_per_thread=clients,pipeline=pipeline,writes=writes,
        client_valid=max(t['cores_used'] for t in row['client_cpu']['Per Thread'].values())<.9 and row['client_cpu']['cpu_cores_used']<38.4,
        rss_mib=after['process']['rss_bytes']/2**20)
    row['client_throttled_seconds'] = (ca['cgroup']['cpu']['throttled_usec'] - cb['cgroup']['cpu']['throttled_usec']) / 1e6
    row['client_valid'] &= row['client_throttled_seconds'] < .01 * seconds
    row['pass_5ms']=row['client_valid'] and row['p99_ms']<=5
    save(out/(label+'.summary.json'),row)
    print(out.name,label,round(row['ops_per_sec']),'p99',row['p99_ms'],'client',row['client_valid'],flush=True)
    return row


def resume(o):
    """Resume only the recorded, still-live hosts; discard interrupted cells."""
    resources = json.loads((o.output / 'resources.json').read_text())
    assert resources['items'][0]['metadata']['name'] == o.namespace
    protocol = json.loads((o.output / 'protocol.json').read_text())
    assert o.cpus == protocol['cpu_quotas']
    assert o.zone == protocol.get('zone', 'ap-south-1a')
    assert o.three_engines and protocol['engines'] == ['redis', 'dragonfly', 'after']
    assert protocol['binaries']['after'] == hashlib.sha256((BUILD/'static-after/bin/vex').read_bytes()).hexdigest()
    for item in resources['items']:
        live = json.loads(s.kub('get', item['kind'], item['metadata']['name'], '-o', 'json'))
        assert live['metadata']['labels']['bench.vex.dev/run'] == o.namespace
        assert not live['metadata'].get('deletionTimestamp'), 'cleanup already underway'
    nodes = json.loads(s.kub('get', 'nodes', '-l', 'bench.vex.dev/run='+o.namespace, '-o', 'json'))
    initial = json.loads((o.output/'nodes.json').read_text())
    boots = lambda ns: {(x['metadata']['uid'], x['status']['nodeInfo']['bootID']) for x in ns['items']}
    assert len(nodes['items']) == 2 and boots(nodes) == boots(initial), 'host changed; new run required'
    old = json.loads((SOURCE/'resources.json').read_text())['items'][0]['metadata']['name']
    o.template = json.loads((SOURCE/'server-candidate-16.json').read_text().replace(old, o.namespace))
    o.template['spec']['containers'][0]['image'] = o.template['spec']['containers'][1]['image']
    stamp = str(int(time.time()))
    save(o.output/f'resume-{stamp}.json', dict(nodes=nodes, reason='AWS credential interruption; fresh client and interrupted server cell'))
    save(o.output/f'runner-resume-{stamp}.py', pathlib.Path(__file__).read_text())
    # No detached workload may survive into a fresh measurement.
    s.kub('delete', 'pod', 'client', '--wait=true', '--timeout=90s')
    s.kub('create', '-f', str(o.output/'client-48.json'))
    for cell in o.output.glob('c*-*'):
        if cell.is_dir() and not (cell/'complete.json').exists():
            excluded = o.output/'interrupted';excluded.mkdir(exist_ok=True)
            cell.rename(excluded/(cell.name+'-'+stamp))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    parser.add_argument('--namespace',required=True)
    parser.add_argument('--three-engines', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--zone', choices=('ap-south-1a','ap-south-1b'), default='ap-south-1a')
    parser.add_argument('--cpus', type=int, nargs='+', choices=(4,8,16), default=[4,8,16])
    o=parser.parse_args();o.output=o.output.resolve()
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws','sts','get-caller-identity','--output','json']).stdout)['Account']=='208168340597'
    s.BASE,s.NS=o.output,o.namespace;s.K=['kubectl','--context',s.CTX,'-n',o.namespace]
    if not o.resume:
        for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
            assert not s.kub('get',kind,name,'--ignore-not-found','-o','name').strip()
        prepare(o)
    try:
        if o.resume:
            resume(o)
        else:
            s.kub('create','-f',str(o.output/'resources.json'))
            s.kub('create','-f',str(o.output/'holder.manifest.json'))
            s.kub('create','-f',str(o.output/'client-48.json'))
        s.kub('wait','--for=condition=Ready','pod/client','--timeout=600s',timeout=610)
        assert s.memtier_hash()==s.MEMTIER_SHA256
        for profile in ('mixed','update'):
            path=o.output/(profile+'.trace')
            if not path.exists():save(path,trace(mixed=profile=='mixed'))
            assert path.read_text() == trace(mixed=profile=='mixed')
            s.kub('cp',str(path),'client:/results/'+profile+'.trace','-c','memtier')
        for cpu in o.cpus:
            order = ['redis', 'dragonfly', 'after'] if o.three_engines else ['before', 'after']
            shift = (4, 8, 16).index(cpu) % len(order)
            for version in order[shift:] + order[:shift]:
                out=o.output/f'c{cpu}-{version}'
                if o.resume and (out/'complete.json').exists():
                    print('RETAIN validated',out.name,flush=True);continue
                out.mkdir()
                host,engine,topology,instances=start(o,cpu,version,out)
                result=remote_job(['python3','-c',ZSET_CHECK,host,'preload'],'/results/preload-'+out.name,600)
                save(out/'preload.json',result.stdout);assert result.returncode==0,result.stderr
                net_before=ena(out,'before',instances)
                rows=[]
                rows.append(phase(o,out,host,engine,topology,'mixed','warmup',10,4,4))
                trials=[phase(o,out,host,engine,topology,'mixed',f'search-p{p}-c{c}',15,c,p) for p in ((4,16) if o.three_engines else (4,)) for c in (1,4,16,64)]
                rows+=trials
                candidates=sorted((r for r in trials if r['pass_5ms']),key=lambda r:r['ops_per_sec'],reverse=True)
                confirmed=[]
                for candidate in candidates:
                    c=candidate['clients_per_thread'];p=candidate['pipeline']
                    group=[phase(o,out,host,engine,topology,'mixed',f'confirm-p{p}-c{c}-r{r}',60,c,p) for r in (1,2,3)]
                    rows+=group
                    if all(r['pass_5ms'] for r in group):
                        confirmed=group;break
                pressure = [phase(o,out,host,engine,topology,'mixed',f'pressure-r{r}',60,32,16) for r in (1,2,3)] if o.three_engines else []
                rows += pressure
                updates=[phase(o,out,host,engine,topology,'update',f'update-r{r}',30,32,16) for r in (1,2,3)]
                rows+=updates
                net_after=ena(out,'after',instances)
                deltas={role:{k:net_after[role][k]-v for k,v in counters.items()} for role,counters in net_before.items()}
                result=remote_job(['python3','-c',ZSET_CHECK,host,'verify',str(sum(r['writes'] for r in rows))],'/results/verify-'+out.name,600)
                save(out/'verify.json',result.stdout);assert result.returncode==0,result.stderr
                save(out/'complete.json',dict(cpu=cpu,version=version,confirmed=confirmed,pressure=pressure,updates=updates,network_deltas=deltas,
                    network_valid=all(v==0 for counters in deltas.values() for v in counters.values())))
                print('COMPLETE',out.name,'network',deltas,flush=True)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        save(o.output/('cleanup-request-'+str(int(time.time()))+'.log'),result.stdout+result.stderr)
        print('CLEANUP requested:',result.returncode,flush=True)


if __name__=='__main__':
    main()
