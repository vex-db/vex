#!/usr/bin/env python3
"""One fresh, bounded AWS control. Infrastructure is provisioned separately.

Server accounting uses its own monotonic orchestration bracket. Client runtime
and latency use the repaired client's clock; clocks are never mixed across nodes.
"""
import argparse
import json
import pathlib
import statistics
import subprocess
import sys

import profile_local_docker as local
import profile_saturation as sat

ROOT = pathlib.Path(__file__).resolve().parents[3]
BASE = ROOT / 'bench/loadtest/runs/2026-09-24-aws-request-path'
K = ['kubectl', '--context', 'scrum-fresh', '-n', 'vex-request-path-20260924']
SERVER_HASH = '4f3df6e8916d9c127a6a9a7940701e1faa3e11182c5809310f7bd2c42fc047d6'
CLIENT_HASH = 'f894894dcfd4cf3f38636b7ec679762d2c6ac5ea0363f98e68703844cf9a3207'
local.REQUIRE_MONOTONIC_CLOCK = True
SERVER_WORKERS=6
SERVER_QUOTA=6
SERVER_MANIFEST='server.manifest.json'

# Executed in each workload container, so /sys/fs/cgroup is the workload's.
SNAPSHOT = r'''
import os,time,json,pathlib,hashlib
P=pathlib.Path

def fields(path):
 return {a:int(b) for a,b in (line.split() for line in P(path).read_text().splitlines())}
def process(pid):
 p=P('/proc')/str(pid);s=(p/'stat').read_text().rsplit(')',1)[1].split()
 status={a:b.strip() for a,b in (line.split(':',1) for line in (p/'status').read_text().splitlines() if ':' in line)}
 tasks=[]
 for t in (p/'task').iterdir():
  try:
   st=(t/'stat').read_text().rsplit(')',1)[1].split()
   tasks.append({'tid':int(t.name),'cpu_seconds':(int(st[11])+int(st[12]))/os.sysconf('SC_CLK_TCK'),'affinity':sorted(os.sched_getaffinity(int(t.name)))})
  except (OSError,ProcessLookupError):pass
 return {'pid':int(pid),'start':s[19],'cpu_seconds':(int(s[11])+int(s[12]))/os.sysconf('SC_CLK_TCK'),'rss_bytes':int(status.get('VmRSS','0 kB').split()[0])*1024,'hwm_bytes':int(status.get('VmHWM','0 kB').split()[0])*1024,'tasks':tasks}
def snapshot(name):
 result={'monotonic':time.monotonic(),'realtime':time.time(),'boot':P('/proc/sys/kernel/random/boot_id').read_text().strip(),'cpu':fields('/sys/fs/cgroup/cpu.stat'),'memory_events':fields('/sys/fs/cgroup/memory.events'),'memory_current':int(P('/sys/fs/cgroup/memory.current').read_text()),'swap_current':int(P('/sys/fs/cgroup/memory.swap.current').read_text()),'cpu_max':P('/sys/fs/cgroup/cpu.max').read_text().strip(),'cpuset':P('/sys/fs/cgroup/cpuset.cpus.effective').read_text().strip(),'node':{n:P('/proc/'+n).read_text() for n in ['stat','softirqs','net/dev','net/snmp','vmstat','meminfo','pressure/cpu','pressure/memory']}}
 matches=[]
 for p in P('/proc').glob('[0-9]*'):
  try:
   if (p/'comm').read_text().strip()==name:matches.append(p)
  except OSError:pass
 if matches:
  assert len(matches)==1
  result['process']=process(matches[0].name)
  result['exe_sha256']=hashlib.sha256((matches[0]/'exe').read_bytes()).hexdigest()
 return result
'''


def run(args, timeout=120, check=True):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f'{args[:8]} exited {r.returncode}: {r.stderr[-2000:]}')
    return r


def ex(pod, container, *args, timeout=120):
    return run(K + ['exec', pod, '-c', container, '--', *args], timeout).stdout


def save(out, name, value):
    (out / name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2)+'\n')


def snapshot(pod, container, process_name):
    return json.loads(ex(pod, container, 'python3', '-c', SNAPSHOT + '\nprint(json.dumps(snapshot('+repr(process_name)+')))'))


def workload(host, seconds, preload=False):
    args = ['memtier_benchmark', '-s', host, '-p', '6379', '-t', '1' if preload else '32', '-c', '1' if preload else '4', '--pipeline', '128' if preload else '1', '--ratio', '1:0' if preload else '1:4', '--key-prefix', 'vex:key::', '--key-minimum', '1000000', '--key-maximum', '1999999', '--data-size', '256', '--hide-histogram', '--print-percentiles', '50,95,99', '--json-out-file', '/results/load.json']
    return args + (['--requests', '1000000', '--key-pattern', 'S:S'] if preload else ['--test-time', str(seconds), '--key-pattern', 'R:R', '--distinct-client-seed'])


def validate(data, stderr, host, seconds, preload=False):
    local.assert_no_benchmark_errors(stderr)
    clock = local.parse_clock_metadata(stderr)
    if preload:
        cfg=data['configuration']
        expected={'server':host,'port':6379,'threads':1,'clients':1,'pipeline':128,'ratio':'1:0','data_size':256,'key_minimum':1000000,'key_maximum':1999999,'key_prefix':'vex:key::'}
        assert all(cfg.get(k)==v for k,v in expected.items()),cfg
        assert data['ALL STATS']['Totals']['Count']==1000000
        assert str(data['ALL STATS']['Runtime'].get('Interrupted','false')).lower()=='false'
        assert all(not v.get('Connection Errors',0) for v in data['ALL STATS'].values() if isinstance(v,dict))
        return {'clock':clock}
    local.validate_latency(data)
    result = sat.validate_raw(data, 1, 128, seconds, destination_host=host)
    assert result['connection_errors']==0 and result['misses_per_sec']==0,result
    return result | {'clock':clock,'ops_per_sec':result['ops']}


def values(host):
    local.SERVER=host
    local.dex=lambda unused,*args,**kwargs: ex('client','sampler',*args,**kwargs)
    result=local.probe_values()
    assert result['dbsize']==1000000,result
    return result


def memory_guard(before, after):
    for item in [before, after]:
        assert item['swap_current']==0,item['swap_current']
        assert not any(item['memory_events'].get(k,0) for k in ['high','max','oom','oom_kill']),item['memory_events']
    for key in ['pswpin','pswpout']:
        def value(s):return dict(line.split() for line in s['node']['vmstat'].splitlines())[key]
        assert value(before)==value(after),f'node paging changed: {key}'


def cpu_metrics(before, after, operations, quota, duration):
    delta=sat.cpu_delta(before['cpu'],after['cpu'])
    bracket=after['monotonic']-before['monotonic']
    assert bracket>0 and before['boot']==after['boot']
    throttle=delta['throttled_usec']/1e6/duration
    assert throttle<=.01,f'CPU throttling {throttle:.2%}'
    return {'cpu_seconds':delta['usage_usec']/1e6,'cpu_us_per_op':delta['usage_usec']/operations,'bracket_seconds':bracket,'cpu_cores_during_load':delta['usage_usec']/1e6/duration,'headroom_fraction':1-delta['usage_usec']/1e6/duration/quota,'throttled_fraction':throttle,'delta':delta}


def timed(out, host, label, seconds):
    # Sample Vex from its sidecar; cgroup counters still come from vex itself.
    sampler = SNAPSHOT + r'''
stop=P('/results/sample-stop');stop.unlink(missing_ok=True)
with open('/results/server-samples.jsonl','w',buffering=1) as f:
 while not stop.exists():
  matches=[]
  for p in P('/proc').glob('[0-9]*'):
   try:
    if (p/'comm').read_text().strip()=='vex':matches.append(p)
   except OSError:pass
  assert len(matches)==1
  f.write(json.dumps({'monotonic':time.monotonic(),'process':process(matches[0].name)})+'\n');time.sleep(.5)
'''
    # Client counters are taken on that node immediately around the subprocess.
    wrapper = SNAPSHOT + '\nimport subprocess\nargs='+repr(workload(host,seconds))+r'''
before=snapshot('memtier_benchmark');observed=[]
with open('/results/load.stdout','w') as stdout,open('/results/load.stderr','w') as stderr:
 child=subprocess.Popen(args,stdout=stdout,stderr=stderr)
 while child.poll() is None:
  try:observed.append({'monotonic':time.monotonic(),'process':process(child.pid)})
  except (OSError,ProcessLookupError):pass
  time.sleep(.25)
after=snapshot('memtier_benchmark')
P('/results/client-accounting.json').write_text(json.dumps({'before':before,'after':after,'samples':observed,'returncode':child.returncode}))
assert child.returncode==0,child.returncode
'''
    with (out/(label+'-sampler.stderr')).open('w') as serr:
        sample=subprocess.Popen(K+['exec','server','-c','sampler','--','python3','-c',sampler],stdout=subprocess.DEVNULL,stderr=serr)
        try:
            before=snapshot('server','vex','vex')
            ex('client','memtier','python3','-c',wrapper,timeout=seconds+120)
            after=snapshot('server','vex','vex')
        finally:
            ex('server','vex','touch','/results/sample-stop')
            assert sample.wait(timeout=15)==0,'server sampler failed'
    stdout=ex('client','memtier','cat','/results/load.stdout');stderr=ex('client','memtier','cat','/results/load.stderr')
    data=json.loads(ex('client','memtier','cat','/results/load.json'))
    client=json.loads(ex('client','memtier','cat','/results/client-accounting.json'))
    sample_text=ex('server','sampler','cat','/results/server-samples.jsonl')
    for name,value in [('stdout',stdout),('stderr',stderr),('json',data),('server-before.json',before),('server-after.json',after),('client-accounting.json',client),('server-samples.jsonl',sample_text)]:save(out,label+'.'+name,value)
    row=validate(data,stderr,host,seconds)
    for key in ['pid','start']:assert before['process'][key]==after['process'][key]
    assert before['exe_sha256']==after['exe_sha256']==SERVER_HASH
    assert before['cpu_max']==after['cpu_max']==f'{SERVER_QUOTA*100000} 100000'
    assert client['before']['cpu_max']==client['after']['cpu_max']=='2400000 100000'
    memory_guard(before,after);memory_guard(client['before'],client['after'])
    duration=row['runtime_ms']/1000;count=row['operation_count']
    servercpu=cpu_metrics(before,after,count,SERVER_QUOTA,duration)
    clientcpu=cpu_metrics(client['before'],client['after'],count,24,duration)
    assert clientcpu['headroom_fraction']>=.20,clientcpu
    samples=[json.loads(line) for line in sample_text.splitlines()]
    samples=[r for r in samples if before['monotonic']<=r['monotonic']<=after['monotonic']]
    assert len(samples)>=seconds, len(samples)
    thread_ticks={}
    for r in client['samples']:
        for t in r['process']['tasks']:
            thread_ticks.setdefault(t['tid'],[]).append((r['monotonic'],t['cpu_seconds']))
    utilizations=[]
    for ticks in thread_ticks.values():
        if len(ticks)>1 and ticks[-1][0]-ticks[0][0]>=seconds*.8:
            utilizations.append((ticks[-1][1]-ticks[0][1])/(ticks[-1][0]-ticks[0][0]))
    assert len(utilizations)>=32,len(utilizations)
    assert max(utilizations)<.9,f'client hot thread {max(utilizations):.2%}'
    row.update(server_cpu=servercpu,client_cpu=clientcpu,client_max_thread_cpu_fraction=max(utilizations),rss_peak_bytes=max(r['process']['rss_bytes'] for r in samples),rss_median_bytes=statistics.median(r['process']['rss_bytes'] for r in samples),lifetime_hwm_bytes=after['process']['hwm_bytes'],rss_scope='server-local orchestration bracket; includes launch/drain, excludes preload/warmup',values_after=values(host),measurement_valid=True)
    save(out,label+'-summary.json',row)
    print(label,round(row['ops_per_sec']),'ops/s p99',row['p99_ms'],'RSS MiB',round(row['rss_peak_bytes']/1048576,2),flush=True)
    return row


def control(label):
    out=BASE/label;out.mkdir(exist_ok=False)
    save(out,'runner.py',pathlib.Path(__file__).read_text())
    run(K+['delete','pod','server','--wait=true','--timeout=45s'])
    run(K+['create','-f',str(BASE/SERVER_MANIFEST)])
    run(K+['wait','--for=condition=Ready','pod/server','--timeout=90s'])
    pods={p:json.loads(run(K+['get','pod',p,'-o','json']).stdout) for p in ['server','client']}
    save(out,'pods.json',pods)
    initial=json.loads((BASE/'nodes-active.json').read_text())['items']
    nodes={n['metadata']['labels']['bench.vex.dev/role']:n for n in initial}
    assert pods['server']['spec']['nodeName']==nodes['server']['metadata']['name']
    assert pods['client']['spec']['nodeName']==nodes['client']['metadata']['name']
    assert nodes['server']['metadata']['name']!=nodes['client']['metadata']['name']
    assert all(n['metadata']['labels']['topology.kubernetes.io/zone']=='ap-south-1a' for n in nodes.values())
    for p,c in [('server','vex'),('client','memtier')]:
        wanted=next(x['image'] for x in pods[p]['spec']['containers'] if x['name']==c).split('@')[1]
        status=next(x for x in pods[p]['status']['containerStatuses'] if x['name']==c)
        assert wanted in status['imageID'] and status['restartCount']==0,status
    host=pods['server']['status']['podIP']
    logs=run(K+['logs','server','-c','vex']).stdout;save(out,'startup.log',logs)
    assert logs.count('io_uring backend active')==SERVER_WORKERS and 'sq_head_cache=true' not in logs
    assert ex('server','vex','sha256sum','/usr/local/bin/vex').split()[0]==SERVER_HASH
    assert ex('client','memtier','sha256sum','/usr/local/bin/memtier_benchmark').split()[0]==CLIENT_HASH
    code="import socket; s=socket.create_connection(('127.0.0.1',6379));s.settimeout(5);s.sendall(b'*2\\r\\n$5\\r\\nDEBUG\\r\\n$6\\r\\nPROBES\\r\\n');f=s.makefile('rb');n=int(f.readline()[1:]);v=f.read(n).decode();assert 'probes_enabled=false' in v;print(v)"
    save(out,'probes.txt',ex('server','vex','python3','-c',code))
    preload=run(K+['exec','client','-c','memtier','--',*workload(host,0,True)],timeout=900)
    save(out,'preload.stdout',preload.stdout);save(out,'preload.stderr',preload.stderr)
    data=json.loads(ex('client','memtier','cat','/results/load.json'));save(out,'preload.json',data)
    validate(data,preload.stderr,host,0,True);save(out,'preloaded-values.json',values(host))
    timed(out,host,'warmup',10)
    return timed(out,host,label,60)


def self_test():
    local.self_test()
    compile(SNAPSHOT,'remote snapshot','exec')
    assert workload('1.2.3.4',60)[-5:]==['--test-time','60','--key-pattern','R:R','--distinct-client-seed']
    print('AWS wrapper syntax and shared validators passed')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--control',choices=['control-1','control-2','two-worker-1','two-worker-2']);ap.add_argument('--self-test',action='store_true');args=ap.parse_args()
    if args.self_test:self_test()
    elif args.control:
        if args.control.startswith('two-worker'):
            SERVER_WORKERS=SERVER_QUOTA=2
            SERVER_MANIFEST='server-two-worker.manifest.json'
        try:control(args.control)
        except Exception as exc:
            out=BASE/args.control
            if out.exists():save(out,'failure.json',{'reason':str(exc)})
            raise
    else:ap.error('choose --control or --self-test')
