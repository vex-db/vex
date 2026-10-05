"""Matched CPU/pipeline and independent-key sorted-set experiments."""
import argparse
import hashlib
import json
import pathlib
import random

import scaling_diagnostics_aws as d
from realistic_aws import client_snapshot, engine_order
from realistic_workloads import command, validate_result

s = d.s
CASES = [('getset', 4, 1), ('getset', 16, 1), ('getset', 4, 16), ('getset', 16, 16),
         ('zset', 4, 16), ('zset', 16, 16), ('zset-hot', 16, 16)]
BOARDS, MEMBERS = 64, 4096


def configure(o, protocol):
    protocol.update(classification='Dragonfly crossover hypotheses; equal-resource three-engine comparison',
                    arrival_model='uncapped closed-loop; service latency only', slo=None,
                    cases=([('zset-mixed',16,16)] if o.suite=='hypotheses-mixed' else CASES), server_memory='8Gi', vex_pinning=False,
                    workload='GET/SET: 1m x 256B, 80/20; ZINCRBY: 64 sorted sets x 4096 existing members, uniform replay; hot control targets set 0',
                    hypotheses=['CPU/pipeline interaction: matched 4/16 CPU x pipeline 1/16',
                                'Independent-key command parallelism: sorted sets at 4/16 CPU, pipeline 16',
                                'Single-hot-key control: identical preloaded dataset, requests target one set'],
                    interpretation='All repetitions retained; client-limited cells inconclusive; single-process engines, not Redis Cluster; 5ms p99 shown separately from uncapped throughput')
    if o.suite=='hypotheses-mixed':
        protocol.update(workload='64 sorted sets x 4096 existing members; 80% ZINCRBY / 20% ZRANK seeded replay; 16 CPU, pipeline 16',
                        hypotheses=['Leaderboard score updates with rank readers; deferred sort work must be included'],cpus=[16])
    for path in o.output.glob('server-*.json'):
        data=json.loads(path.read_text());primary=data['spec']['containers'][0]
        for kind in ('requests','limits'):primary['resources'][kind]['memory']='8Gi'
        primary['command']=['--maxmemory=6gb' if x.startswith('--maxmemory=') else x for x in primary['command']]
        path.write_text(json.dumps(data,indent=2)+'\n')
    s.write(o.output/'source'/pathlib.Path(__file__).name,pathlib.Path(__file__).read_text())


def trace(hot=False, mixed=False):
    rng=random.Random(20260927)
    lines=[]
    for i in range(32768):
        board=rng.randrange(BOARDS);member=rng.randrange(MEMBERS)
        operation=f'"ZRANK" "lb:{board}" "m:{member}"' if mixed and i%5==0 else f'"ZINCRBY" "lb:{0 if hot else board}" "1" "m:{member}"'
        lines.append(f'{i+1}.000000 [0 127.0.0.1:12345] {operation}\n')
    return ''.join(lines)


# Runs outside measurement; verifies every reply, fixed cardinality, and exact
# score conservation across all warmup/measured increments (no silent errors).
ZSET_CHECK = r'''import socket,sys,json
host,mode=sys.argv[1:3];sock=socket.create_connection((host,6379),30);sock.settimeout(120);f=sock.makefile('rb')
def reply():
 line=f.readline();assert line,line
 kind=line[:1];assert kind!=b'-',line
 if kind==b'*':return [reply() for _ in range(int(line[1:]))]
 if kind==b'$':
  n=int(line[1:]);assert n>=0,line
  data=f.read(n);assert f.read(2)==b'\r\n';return data.decode()
 return line[1:].decode().strip()
def ask(parts):
 parts=[str(x).encode() for x in parts]
 sock.sendall(b'*'+str(len(parts)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in parts));return reply()
if mode=='preload':
 for b in range(64):
  for start in range(0,4096,128):
   args=['ZADD','lb:'+str(b)]
   for m in range(start,start+128):args+=['0','m:'+str(m)]
   assert int(ask(args))==128
 # Compatibility preflight including actual update and restoration.
 assert float(ask(['ZINCRBY','lb:0','1','m:0']))==1
 assert float(ask(['ZINCRBY','lb:0','-1','m:0']))==0
cards=[];score_sum=0
for b in range(64):
 card=int(ask(['ZCARD','lb:'+str(b)]));assert card==4096,card;cards.append(card)
 values=ask(['ZRANGE','lb:'+str(b),'0','-1','WITHSCORES']);assert len(values)==8192
 assert set(values[::2])=={'m:'+str(m) for m in range(4096)}
 scores=[float(x) for x in values[1::2]];assert all(x>=0 and x.is_integer() for x in scores)
 pairs=list(zip(values[::2],scores));assert pairs==sorted(pairs,key=lambda x:(x[1],x[0])), 'Incorrect sorted order'
 for rank in (0,2048,4095):assert int(ask(['ZRANK','lb:'+str(b),values[rank*2]]))==rank
 score_sum+=sum(int(x) for x in scores)
expected=0 if mode=='preload' else int(sys.argv[3]);assert score_sum==expected,(score_sum,expected)
print(json.dumps({'cardinalities':cards,'score_sum':score_sum,'expected':expected}))
'''


def args(w, phase, seconds, pipeline, sorted_set=False):
    if not sorted_set:
        result=command(w,phase,seconds,0,phase=='preload')
        if phase!='preload':result[result.index('--pipeline')+1]=str(pipeline)
        return result
    return ['memtier_benchmark','-s',w.host,'-p','6379','-t','32','-c','32','--pipeline',str(pipeline),
            '--test-time',str(seconds),'--distinct-client-seed','--hide-histogram','--print-percentiles','50,95,99,99.9',
            '--monitor-input',str(w.output/'trace.txt'),'--command','__monitor_line@__','--monitor-pattern','R',
            '--json-out-file',str(w.output/(phase+'.json'))]



def capture_timeout(out, remote, engine, error):
    s.write(out/'timeout.json',dict(error=str(error),classification='No valid measured throughput; benchmark did not finish within its bound'))
    for label,command in [('pods',['get','pods','server','client','-o','json']),('server-log',['logs','server','-c','engine','--tail=100'])]:
        result=s.run(s.K+command,timeout=30,check=False)
        s.write(out/('timeout-'+label+'.txt'),result.stdout+result.stderr)
    code="import pathlib,sys,json;p=pathlib.Path(sys.argv[1]);print(json.dumps({str(x):x.read_text()[-16000:] for x in p.glob('*') if x.is_file()}))"
    result=s.run(s.K+['exec','client','-c','memtier','--','python3','-c',code,remote],timeout=30,check=False)
    s.write(out/'timeout-client.txt',result.stdout+result.stderr)
    try:s.write(out/'timeout-server.json',s.snapshot_server(engine['process_name']))
    except Exception as exc:s.write(out/'timeout-snapshot-error.txt',str(exc))


def run_hypotheses(o, engines):
    d.ensure_client(o,48)
    assert s.memtier_hash()==s.MEMTIER_SHA256
    help_result=s.run(s.K+['exec','client','-c','memtier','--','memtier_benchmark','--help'],check=False)
    help_text=help_result.stdout+help_result.stderr
    assert help_result.returncode in (0,2),help_text
    assert '--monitor-input' in help_text and '--monitor-pattern' in help_text
    if not (o.output/'memtier-help.txt').exists():s.write(o.output/'memtier-help.txt',help_text)
    cases=[('zset-mixed',16,16)] if o.suite=='hypotheses-mixed' else CASES
    rows=[]
    for round_no in range(getattr(o,'first_round',1),getattr(o,'first_round',1)+o.rounds):
        # Rotate both workload and engine order to reduce order effects.
        shift=(round_no-1)%len(cases)
        for index,(profile,cpu,pipeline) in enumerate(cases[shift:]+cases[:shift]):
            for name in engine_order(o.engines,round_no,index):
                label=f'r{round_no}-{profile}-c{cpu}-p{pipeline}-{name}'
                complete=o.output/'cells'/label/'complete.json'
                if complete.exists():
                    assert o.resume;rows.append(json.loads(complete.read_text()));continue
                out,host,engine,topology=d.start(o,engines,label,cpu,name=name,pin=False)
                remote='/results/'+o.output.name+'/'+label
                w=argparse.Namespace(memtier='memtier_benchmark',host=host,port=6379,threads=32,clients=32,
                    prefix='vex:hyp:',keys=o.keys,output=pathlib.Path(remote),profile='baseline')
                s.ex('client','memtier','mkdir','-p',remote)
                sorted_set=profile.startswith('zset');total_increments=0
                if sorted_set:
                    contents=trace(profile=='zset-hot',profile=='zset-mixed');path=out/'trace.txt';s.write(path,contents)
                    s.kub('cp',str(path),'client:'+remote+'/trace.txt','-c','memtier')
                    assert s.ex('client','memtier','sha256sum',remote+'/trace.txt').split()[0]==hashlib.sha256(contents.encode()).hexdigest()
                    result=d.scaling.remote_job(['python3','-c',ZSET_CHECK,host,'preload'],remote+'/preload-job',600)
                    s.write(out/'preload.stdout',result.stdout);s.write(out/'preload.stderr',result.stderr)
                    assert result.returncode==0,result.stderr
                phases=[];failed=None
                schedule=([('preload',0)] if not sorted_set else [])+[('warmup',10),('measure',o.seconds)]
                for phase,seconds in schedule:
                    cmd=args(w,phase,seconds,pipeline,sorted_set)
                    before=s.snapshot_server(engine['process_name']);cb=client_snapshot()
                    s.write(out/(phase+'.command.json'),cmd)
                    try:
                        result=d.scaling.remote_job(cmd,remote+'/'+phase+'-job',1200 if phase=='preload' else seconds+120)
                    except RuntimeError as exc:
                        if o.suite!='hypotheses-mixed' or 'remote job did not finish/export within its bound' not in str(exc):raise
                        capture_timeout(out,remote+'/'+phase+'-job',engine,exc)
                        failed=dict(round=round_no,engine=name,profile=profile,cpus=cpu,pipeline=pipeline,status='timeout',phase=phase,error=str(exc))
                        s.write(out/'failed.json',failed)
                        # A detached timed-out load must not contaminate the next cell.
                        o.active_client_cpus=None;d.ensure_client(o,48)
                        print('FAILED HYPOTHESIS',label,phase,'timeout; fresh client created',flush=True)
                        break
                    s.write(out/(phase+'.stdout'),result.stdout);s.write(out/(phase+'.stderr'),result.stderr)
                    assert result.returncode==0,result.stderr
                    raw=json.loads(s.ex('client','memtier','cat',remote+'/'+phase+'.json'))
                    s.write(out/(phase+'.raw.json'),raw)
                    row=validate_result(raw,result.stderr,seconds,0,False)
                    final,after=s.assert_final_topology(topology,engine);ca=client_snapshot()
                    s.assert_memory(before,after);s.assert_memory(cb,ca);assert cb['boot']==ca['boot']
                    for key,data in [('server-before',before),('server-after',after),('client-before',cb),('client-after',ca),('topology-after',final)]:
                        s.write(out/(phase+'.'+key+'.json'),data)
                    cfg=raw['configuration'];preload=phase=='preload'
                    expected=dict(server=host,port=6379,threads=1 if preload else 32,clients=1 if preload else 32,pipeline=128 if preload else pipeline,rate_limit=0)
                    assert all(cfg.get(k)==v for k,v in expected.items()),cfg
                    count=raw['ALL STATS']['Totals']['Count']
                    if sorted_set:
                        buckets=[v for k,v in raw['ALL STATS'].items() if k!='Totals' and isinstance(v,dict) and 'Count' in v]
                        assert sum(v['Count'] for v in buckets)==count,raw['ALL STATS'].keys()
                        if profile=='zset-mixed':
                            increments=sum(v['Count'] for k,v in raw['ALL STATS'].items() if k.lower().rstrip('s')=='zincrby')
                            ranks=sum(v['Count'] for k,v in raw['ALL STATS'].items() if k.lower().rstrip('s')=='zrank')
                            assert increments+ranks==count and .19<ranks/count<.21,raw['ALL STATS'].keys()
                            total_increments+=increments
                        else:total_increments+=count
                    elif preload:assert count==o.keys
                    else:
                        assert cfg['ratio']=='1:4' and cfg['data_size']==256 and cfg['key_maximum']==o.keys
                    client=row['client_cpu'];delta=s.aws.sat.cpu_delta(cb['cgroup']['cpu'],ca['cgroup']['cpu'])
                    row.update(phase=phase,operations=count,client_throttled_seconds=delta['throttled_usec']/1e6,
                        client_headroom_ok=max(t['cores_used'] for t in client['Per Thread'].values())<.9 and client['cpu_cores_used']<.8*o.client_cpus)
                    if not preload:row['client_headroom_ok'] &= row['client_throttled_seconds']<.01*seconds
                    row['p99_under_5ms']=row['p99_ms']<=5
                    s.write(out/(phase+'.summary.json'),row)
                    if phase=='measure':phases.append(row)
                if failed:
                    rows.append(failed);continue
                if sorted_set:
                    result=d.scaling.remote_job(['python3','-c',ZSET_CHECK,host,'verify',str(total_increments)],remote+'/verify-job',600)
                    s.write(out/'verify.stdout',result.stdout);s.write(out/'verify.stderr',result.stderr)
                    assert result.returncode==0,result.stderr
                s.postload_checks('vex' if name=='candidate' else name,host,out,'engine-after.json',io_threads=cpu)
                assert s.memtier_hash()==s.MEMTIER_SHA256
                row=dict(round=round_no,engine=name,profile=profile,cpus=cpu,pipeline=pipeline,memory='8Gi',vex_pinning=False,phases=phases)
                s.write(complete,row);rows.append(row)
                print('COMPLETE HYPOTHESIS',label,'ops',round(phases[0]['ops_per_sec']),'p99',phases[0]['p99_ms'],'client_valid',phases[0]['client_headroom_ok'],flush=True)
    return rows
