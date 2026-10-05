#!/usr/bin/env python3
"""Paired 4/8-worker diagnostics, or longer three-engine workload comparisons."""
import argparse
import copy
import hashlib
import json
import pathlib
import subprocess
import time

import scaling_aws as scaling
from realistic_aws import client_snapshot, engine_order
from realistic_workloads import command as workload_command, validate_result, SIZES

s = scaling.s
HERE = pathlib.Path(__file__).resolve().parent


def diagnostics(round_no):
    # Rotate order; change only one server/client setting relative to 8p-wide.
    cases = [('4p-wide', 4, True, 48, [640000, 720000, 800000], 0),
             ('8p-wide', 8, True, 48, [640000, 720000, 800000], 0),
             ('8p-client24', 8, True, 24, [800000], 0),
             ('8unpinned-wide', 8, False, 48, [800000], 0),
             ('8p-shifted-wide', 8, True, 48, [800000], 8)]
    offset = (round_no - 1) % len(cases)
    return cases[offset:] + cases[:offset]


def prepare(o):
    o.cpus = [4, 16] if o.suite in ('hypotheses','hypotheses-mixed') else [4, 8]
    o.engines = ['candidate', 'redis', 'dragonfly']
    o.confirm_seconds = o.seconds
    o.max_rate = 1024000
    engines = scaling.prepare(o)
    resources = json.loads((o.output/'resources.json').read_text())
    client_pool = resources['items'][2]['spec']
    client_pool['limits'] = {'cpu': '64', 'memory': '128Gi'}
    for requirement in client_pool['template']['spec']['requirements']:
        if requirement['key'] == 'node.kubernetes.io/instance-type':
            requirement['values'] = ['c7g.16xlarge']
    (o.output/'resources.json').write_text(json.dumps(resources, indent=2)+'\n')
    protocol = json.loads((o.output/'protocol.json').read_text())
    protocol.update(classification='diagnostic experiment' if o.suite == 'diagnostics' else 'repeated closed-loop workload comparison',
                    client_machine='c7g.16xlarge', server_machine='c7g.8xlarge',
                    client_gate='CPU <80% of actual cgroup quota; open-loop dispatch p99 <=0.25ms and throttling <1%; closed-loop max client thread <0.9 cores',
                    workload='100k x 256B, 80/20 GET/SET, 1024 connections' if o.suite == 'diagnostics' else f'{o.keys} keys; baseline 256B or mixed sizes {SIZES}; uniform or Gaussian hot keys; 80/20 GET/SET; uncapped 1024 connections',
                    interpretation='Instrumented probe trials are excluded from performance claims; closed-loop latency is not scheduled-arrival latency')
    if o.suite=='workloads':
        protocol.update(slo=None, cpu_scaling='Fixed four-CPU comparison; no scheduled-arrival capacity claim',
                        arrival_model='uncapped closed-loop; service latency only')
    elif o.suite in ('hypotheses','hypotheses-mixed'):
        from dragonfly_hypotheses import configure
        configure(o, protocol)
    elif o.suite=='budgets':
        protocol.update(classification='three-engine scheduled-arrival latency-budget comparison',
                        cpu_scaling='Fixed four-CPU quota on one unchanged server; Vex unpinned',
                        arrival_model='independently scheduled arrivals, including client wait and loss',
                        primary_budget_ms=5,latency_budgets_ms=[1,2,5],confirmation_seconds=60,cpus=[4],max_rate=1200000,
                        slo='At most 1% over budget or uncompleted; no errors/loss; >=99% completed within arrival window',
                        workload=f'{o.keys} x 256B keys; uniform 80/20 GET/SET; 1024 connections; pipeline 1',
                        search='1.5x ramp to 5ms failure; two bisections per budget; confirm at 97% of exploratory passing rate, three x 60s; at most three lower-rate confirmation groups',
                        histogram_scoring='2/5ms use conservative 10us bucket cutoffs; 1ms uses exact driver counter')
    (o.output/'protocol.json').write_text(json.dumps(protocol, indent=2)+'\n')
    for path in [pathlib.Path(__file__), HERE/'realistic_workloads.py']:
        s.write(o.output/'source'/path.name, path.read_text())
    return engines


def ensure_client(o, cpus):
    if getattr(o, 'active_client_cpus', None) == cpus:
        return
    s.kub('delete', 'pod', 'client', '--ignore-not-found', '--wait=true', '--timeout=60s')
    manifest = json.loads((o.output/'client.manifest.json').read_text())
    for kind in ['requests', 'limits']:
        manifest['spec']['containers'][0]['resources'][kind]['cpu'] = str(cpus)
    path = o.output/f'client-{cpus}.json'
    if not path.exists():
        s.write(path, manifest)
    s.kub('create', '-f', str(path))
    s.kub('wait', '--for=condition=Ready', 'pod/client', '--timeout=600s', timeout=610)
    s.kub('cp', str(o.binary), 'client:/results/openloop', '-c', 'memtier', timeout=120)
    s.ex('client', 'memtier', 'chmod', '755', '/results/openloop')
    assert s.ex('client', 'memtier', 'sha256sum', '/results/openloop').split()[0] == hashlib.sha256(o.binary.read_bytes()).hexdigest()
    o.active_client_cpus = o.client_cpus = cpus
    o.gomaxprocs = 20 if cpus == 24 else 40
    o.driver_args = ['-connection-stats']


def start(o, engines, label, cpu, name='candidate', pin=True, offset=0):
    out = o.output/'cells'/label
    out.mkdir(parents=True)
    manifest = json.loads((o.output/f'server-{name}-{cpu}.json').read_text())
    if name == 'candidate':
        manifest['spec']['containers'][0]['env'] = [{'name': 'VEX_PIN_WORKERS', 'value': '1' if pin else '0'}]
    s.write(out/'server.manifest.json', manifest)
    engine = copy.deepcopy(engines[name])
    engine['command'] = manifest['spec']['containers'][0]['command']
    s.kub('delete', 'pod', 'server', '--ignore-not-found', '--wait=true', '--timeout=60s')
    s.kub('create', '-f', str(out/'server.manifest.json'))
    s.kub('wait', '--for=condition=Ready', 'pod/server', '--timeout=180s', timeout=190)
    nodes = json.loads(s.kub('get', 'nodes', '-l', 'bench.vex.dev/run='+o.namespace, '-o', 'json'))
    if not (o.output/'nodes-active.json').exists():
        s.write(o.output/'nodes-active.json', nodes)
    initial = json.loads((o.output/'nodes-active.json').read_text())
    assert {n['metadata']['uid'] for n in nodes['items']} == {n['metadata']['uid'] for n in initial['items']}
    server, client = s.validate_pods(engine, nodes, server_cpu=cpu, server_machine='c7g.8xlarge',
                                     client_cpu=o.client_cpus, client_machine='c7g.16xlarge',
                                     server_memory='8Gi' if o.suite in ('hypotheses','hypotheses-mixed') else '4Gi')
    identity = s.snapshot_server(engine['process_name'])
    assert identity['exe_sha256'] == engine['binary_sha256']
    quota, period = map(int, identity['cgroup']['cpu_max'].split())
    assert quota/period == cpu
    if name == 'candidate':
        pinned = [t for t in identity['process']['tasks'] if len(t['affinity']) == 1]
        assert len(pinned) == (cpu if pin else 0), 'Worker affinity does not match experiment'
        if pin and o.suite=='diagnostics':
            assert {t['affinity'][0] for t in pinned} == set(range(cpu))
            assignments=[(t['tid'],t['affinity'][0]+offset) for t in pinned]
            # Drop identity only in this short command. The sampler retains its
            # original privileges for hash/proc reads. Offset zero preflights the
            # exact affinity operation before any timed workload is launched.
            code='import os,json,sys;os.setuid(int(sys.argv[1]))\nfor tid,core in json.loads(sys.argv[2]):os.sched_setaffinity(tid,{core})'
            s.ex('server','sampler','python3','-c',code,str(identity['process']['uid']),json.dumps(assignments))
            identity = s.snapshot_server(engine['process_name'])
            assert {t['affinity'][0] for t in identity['process']['tasks'] if len(t['affinity'])==1} == set(range(offset,offset+cpu))
    topology = s.live_topology(server, client, identity)
    for filename, value in [('topology.json', topology), ('identity.json',identity), ('server-pod.json', server), ('client-pod.json', client)]:
        s.write(out/filename, value)
    s.write(out/'startup.log', s.kub('logs', 'server', '-c', 'engine'))
    host = server['status']['podIP']
    s.postload_checks('vex' if name == 'candidate' else name, host, out, 'engine-before.json', io_threads=cpu)
    return out, host, engine, topology


def probes(host, action=''):
    code = """import socket,sys
parts=['DEBUG','PROBES']+([sys.argv[2]] if sys.argv[2] else [])
s=socket.create_connection((sys.argv[1],6379),10);f=s.makefile('rb')
s.sendall(('*'+str(len(parts))+'\\r\\n'+''.join('$'+str(len(x))+'\\r\\n'+x+'\\r\\n' for x in parts)).encode())
line=f.readline()
assert not line.startswith(b'-'),line
if line.startswith(b'$'):
 n=int(line[1:]);print(f.read(n).decode());assert f.read(2)==b'\\r\\n'
else:print(line.decode())
"""
    return s.ex('client', 'memtier', 'python3', '-c', code, host, action)


def run_diagnostics(o, engines):
    rows = []
    for round_no in range(1, o.rounds+1):
        for label, cpu, pin, client_cpu, rates, offset in diagnostics(round_no):
            completed = o.output/'cells'/f'r{round_no}-{label}'/'complete.json'
            if completed.exists():
                assert getattr(o, 'resume', False)
                rows.append(json.loads(completed.read_text()))
                continue
            ensure_client(o, client_cpu)
            out, host, engine, topology = start(o, engines, f'r{round_no}-{label}', cpu, pin=pin, offset=offset)
            preload = scaling.remote_job(['/results/openloop', '-host', host+':6379', '-preload', '100000'],
                                         '/results/'+o.output.name+'/'+out.name+'/preload', 180)
            assert preload.returncode == 0, preload.stderr
            s.write(out/'preload.stdout', preload.stdout)
            scaling.phase(o, out, host, engine, topology, 16000, 5, 'warmup')
            phases = [scaling.phase(o, out, host, engine, topology, rate, o.seconds, str(rate)) for rate in rates]
            s.postload_checks('vex', host, out, 'engine-after.json', io_threads=cpu)
            row = dict(round=round_no, condition=label, cpus=cpu, pinned=pin, cpu_offset=offset, client_cpus=client_cpu, phases=phases)
            s.write(out/'complete.json', row);rows.append(row)
    # Enable probes only for separately labelled diagnosis, after baseline trials.
    ensure_client(o, 48)
    for cpu in [4, 8]:
        out, host, engine, topology = start(o, engines, f'instrumented-{cpu}', cpu)
        preload = scaling.remote_job(['/results/openloop', '-host', host+':6379', '-preload', '100000'], '/results/'+o.output.name+'/'+out.name+'/preload', 180)
        assert preload.returncode == 0, preload.stderr
        probes(host, 'RESET');probes(host, 'ON')
        try:
            assert 'probes_enabled=true' in probes(host)
            scaling.phase(o, out, host, engine, topology, 800000, 20, 'instrumented-load')
            s.write(out/'probes.txt', probes(host))
        finally:
            probes(host, 'OFF')
        s.postload_checks('vex', host, out, 'engine-after.json', io_threads=cpu)
        s.write(out/'classification.json', {'instrumented': True, 'excluded_from_performance_claims': True})
    return rows


def run_workloads(o, engines):
    ensure_client(o, 48)
    assert s.memtier_hash() == s.MEMTIER_SHA256
    rows = []
    for round_no in range(1, o.rounds+1):
        for index, profile in enumerate(['baseline', 'mixed', 'hot']):
            for name in engine_order(o.engines, round_no, index):
                label = f'r{round_no}-{profile}-{name}'
                completed=o.output/'cells'/label/'complete.json'
                if completed.exists():
                    assert getattr(o,'resume',False)
                    rows.append(json.loads(completed.read_text()))
                    continue
                out, host, engine, topology = start(o, engines, label, 4, name=name)
                remote = '/results/'+o.output.name+'/'+label
                w = argparse.Namespace(memtier='memtier_benchmark', host=host, port=6379, threads=32, clients=32,
                      prefix='vex:expanded:', keys=o.keys, output=pathlib.Path(remote), profile=profile)
                s.ex('client', 'memtier', 'mkdir', '-p', remote)
                phases = []
                for phase, seconds, preload in [('preload', 0, True), ('warmup', 10, False), ('measure', o.seconds, False)]:
                    command = workload_command(w, phase, seconds, 0, preload)
                    before = s.snapshot_server(engine['process_name']);cb = client_snapshot()
                    result = scaling.remote_job(command, remote+'/'+phase+'-job', 1200 if preload else seconds+120)
                    s.write(out/(phase+'.command.json'), command);s.write(out/(phase+'.stderr'), result.stderr)
                    assert result.returncode == 0, result.stderr
                    raw = json.loads(s.ex('client', 'memtier', 'cat', remote+'/'+phase+'.json'))
                    s.write(out/(phase+'.raw.json'), raw)
                    row = validate_result(raw, result.stderr, seconds, 0, False)
                    final, after = s.assert_final_topology(topology, engine);ca = client_snapshot()
                    s.assert_memory(before, after);s.assert_memory(cb, ca);assert cb['boot']==ca['boot']
                    for key, data in [('server-before',before),('server-after',after),('client-before',cb),('client-after',ca),('topology-after',final)]:
                        s.write(out/(phase+'.'+key+'.json'),data)
                    cfg = raw['configuration']
                    expected = dict(server=host, port=6379, threads=1 if preload else 32, clients=1 if preload else 32,
                                    pipeline=128 if preload else 1, ratio='1:0' if preload else '1:4', key_minimum=1,
                                    key_maximum=o.keys, key_prefix=w.prefix, rate_limit=0,
                                    key_pattern='S:S' if preload else 'G:G' if profile=='hot' else 'R:R', expiry_range='0:0')
                    assert all(cfg.get(k)==v for k,v in expected.items()), cfg
                    assert cfg['data_size']==256 if profile=='baseline' else cfg['data_size_list']==SIZES and cfg['random_data']=='true'
                    if preload:assert raw['ALL STATS']['Totals']['Count']==o.keys
                    client = row['client_cpu']
                    row.update(phase=phase, client_headroom_ok=max(t['cores_used'] for t in client['Per Thread'].values())<.9 and client['cpu_cores_used']<.8*o.client_cpus)
                    delta = s.aws.sat.cpu_delta(cb['cgroup']['cpu'], ca['cgroup']['cpu'])
                    row['client_throttled_seconds'] = delta['throttled_usec']/1e6
                    if not preload:
                        row['client_headroom_ok'] &= row['client_throttled_seconds'] < .01*seconds
                    s.write(out/(phase+'.summary.json'),row)
                    if phase=='measure':phases.append(row)
                s.postload_checks('vex' if name=='candidate' else name, host, out, 'engine-after.json', io_threads=4)
                row=dict(round=round_no, engine=name, profile=profile, cpus=4, keys=o.keys, phases=phases,
                         arrival_model='uncapped closed-loop; service latency, not scheduled latency')
                s.write(out/'complete.json',row);rows.append(row)
                print('COMPLETE',label,'ops',round(phases[0]['ops_per_sec']),'p99',phases[0]['p99_ms'],'client_valid',phases[0]['client_headroom_ok'],flush=True)
    return rows


def run_budgets(o, engines):
    """Share each arrival trace across all budgets; only confirmations establish capacity."""
    ensure_client(o,48)
    o.driver_args=['-keys',str(o.keys)]
    o.latency_budgets=[1,2,5];o.primary_budget_ms=5
    results=[]
    for name in o.engines:
        label='r1-budget-'+name
        completed=o.output/'cells'/label/'complete.json'
        if completed.exists():
            assert o.resume
            results.append(json.loads(completed.read_text()));continue
        out,host,engine,topology=start(o,engines,label,4,name=name,pin=False)
        preload=scaling.remote_job(['/results/openloop','-host',host+':6379','-preload',str(o.keys)],
                                   '/results/'+o.output.name+'/'+label+'/preload',600)
        assert preload.returncode==0,preload.stderr
        s.write(out/'preload.stdout',preload.stdout)
        scaling.phase(o,out,host,engine,topology,16000,5,'warmup')
        search=[];confirmations=[]
        def measure(rate,seconds,kind):
            return scaling.phase(o,out,host,engine,topology,rate,seconds,kind)
        rate=160000
        for stage in range(7):
            row=measure(rate,o.seconds,f'ramp-{stage}');search.append(row)
            if not row['client_valid'] or not row['latency_budgets']['5']['pass']:break
            if rate>=1200000:break
            rate=min(1200000,rate*3//2//1000*1000)
        # Find a passing floor if even the initial rate missed the strictest budget.
        if not any(r['latency_budgets']['1']['pass'] for r in search):
            for rate in [80000,40000,20000]:
                row=measure(rate,o.seconds,f'lower-{rate}');search.append(row)
                if row['latency_budgets']['1']['pass']:break
        for budget in [5,2,1]:
            key=str(budget)
            for step in range(2):
                passing=[r['rate'] for r in search if r['latency_budgets'][key]['pass']]
                if not passing:break
                low=max(passing)
                upper=[r['rate'] for r in search if r['rate']>low and not r['latency_budgets'][key]['pass']]
                if not upper:break
                mid=(low+min(upper))//2000*1000
                if mid<=low:break
                search.append(measure(mid,o.seconds,f'refine-{budget}-{step}'))
        budgets={}
        for budget in [5,2,1]:
            key=str(budget)
            candidates=[r['rate'] for r in search if r['latency_budgets'][key]['pass']]
            if not candidates:
                budgets[key]={'confirmed_rate':None,'classification':'no valid passing search trial'};continue
            rate=max(1000,int(max(candidates)*.97)//1000*1000)
            accepted=None
            for reduction in range(3):
                trials=[measure(rate,60,f'confirm-{budget}-{reduction}-{i}') for i in range(3)]
                confirmations.extend(trials)
                if all(r['latency_budgets'][key]['pass'] for r in trials):
                    accepted=rate;break
                lower=[r['rate'] for r in search if r['rate']<rate and r['latency_budgets'][key]['pass']]
                rate=max(lower) if lower else max(1000,rate*8//10//1000*1000)
            failures=[r['rate'] for r in search+confirmations if r['client_valid'] and not r['latency_budgets'][key]['pass'] and (accepted is None or r['rate']>accepted)]
            budgets[key]=dict(confirmed_rate=accepted,nearest_tested_failure=min(failures) if failures else None,
                              classification='confirmed lower bound; not an exact maximum' if accepted else 'confirmation failed')
        s.postload_checks('vex' if name=='candidate' else name,host,out,'engine-after.json',io_threads=4)
        result=dict(engine=name,cpus=4,keys=o.keys,vex_pinned=False,budgets=budgets,search=search,confirmations=confirmations)
        s.write(completed,result);results.append(result)
        print('COMPLETE BUDGETS',name,budgets,flush=True)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--namespace',required=True);p.add_argument('--binary',type=pathlib.Path,required=True)
    p.add_argument('--suite',choices=['diagnostics','workloads','budgets','hypotheses','hypotheses-mixed'],default='diagnostics')
    p.add_argument('--seconds',type=int,default=90);p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--keys',type=int,default=1000000)
    p.add_argument('--first-round',type=int,default=1,help='First repetition number for a documented fresh-host continuation')
    p.add_argument('--resume',action='store_true',help='Continue interrupted trials on the same surviving nodes')
    o=p.parse_args();o.output=o.output.resolve()
    assert o.namespace.startswith('vex-scaling-') and min(o.seconds,o.rounds,o.keys,o.first_round)>0
    assert o.suite in ('hypotheses','hypotheses-mixed') or o.first_round==1
    assert o.suite!='budgets' or o.rounds==3
    assert json.loads(s.run(['aws','sts','get-caller-identity','--profile','default','--output','json']).stdout)['Account']=='208168340597'
    if o.resume:
        protocol=json.loads((o.output/'protocol.json').read_text())
        assert protocol.get('first_round',1)==o.first_round
        for key in ['namespace','suite','seconds','rounds','keys']:
            assert protocol[key]==getattr(o,key),(key,protocol[key],getattr(o,key))
        assert hashlib.sha256(o.binary.read_bytes()).hexdigest()==protocol['binary_sha256']
        s.BASE,s.NS=o.output,o.namespace
        s.K=['kubectl','--context',s.CTX,'-n',o.namespace]
        engines=json.loads((o.output/'engines.json').read_text())
        o.engines=protocol['engines']
        nodes=json.loads(s.kub('get','nodes','-l','bench.vex.dev/run='+o.namespace,'-o','json'))
        original=json.loads((o.output/'nodes-active.json').read_text())
        identity=lambda items: {(n['metadata']['uid'],n['status']['nodeInfo']['bootID']) for n in items}
        assert identity(nodes['items'])==identity(original['items']), 'Cannot resume on changed hosts or boots'
        assert not list((o.output/'cells').glob('instrumented-*')), 'Instrumented recovery needs separate handling'
        archive=o.output/'interruptions'/str(time.time_ns());archive.mkdir(parents=True)
        for cell in (o.output/'cells').glob('r*'):
            if not (cell/'complete.json').exists():cell.rename(archive/cell.name)
        for name in ['cleanup-request.log','interruption.json']:
            if (o.output/name).exists():(o.output/name).rename(archive/name)
        s.write(archive/'resume-source.py',pathlib.Path(__file__).read_text())
    else:
        engines=prepare(o)
        for kind,name in [('namespace',o.namespace),('nodepool',o.namespace+'-server'),('nodepool',o.namespace+'-client')]:
            assert not s.kub('get',kind,name,'--ignore-not-found','-o','name').strip()
    try:
        if not o.resume:
            s.kub('create','-f',str(o.output/'resources.json'));s.kub('create','-f',str(o.output/'holder.manifest.json'))
        ensure_client(o,48)
        from dragonfly_hypotheses import run_hypotheses
        runner={'hypotheses':run_hypotheses,'hypotheses-mixed':run_hypotheses,'diagnostics':run_diagnostics,'workloads':run_workloads,'budgets':run_budgets}[o.suite]
        rows=runner(o,engines)
        s.write(o.output/'summary.json',rows)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        s.write(o.output/'cleanup-request.log',result.stdout+result.stderr)
        print('Cleanup request exit:',result.returncode,flush=True)

if __name__=='__main__':main()
