#!/usr/bin/env python3
"""Bounded on-host CPU profiling of the frozen Vex sorted-set candidate."""
import argparse
import concurrent.futures
import json
import pathlib
import time
import compare_sorted_set_scaling as b
s=b.s


def ssm(instance, script, out, timeout=240):
    response=json.loads(s.run(['aws','ssm','send-command','--region','ap-south-1',
        '--document-name','AWS-RunShellScript','--instance-ids',instance,'--parameters',json.dumps({'commands':[script]}),'--output','json']).stdout)
    b.save(out.with_suffix('.command.json'),response)
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        result=s.run(['aws','ssm','get-command-invocation','--region','ap-south-1','--command-id',response['Command']['CommandId'],'--instance-id',instance,'--output','json'],check=False)
        if result.returncode==0:
            data=json.loads(result.stdout)
            if data['Status'] not in ('Pending','InProgress','Delayed'):
                b.save(out.with_suffix('.json'),data)
                return data
        time.sleep(2)
    raise RuntimeError('SSM profiling timeout')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--namespace',required=True)
    o=p.parse_args();o.output=o.output.resolve();o.three_engines=True;o.cpus=[8,16];o.zone='ap-south-1b'
    assert o.namespace.startswith('vex-scaling-')
    assert json.loads(s.run(['aws','sts','get-caller-identity','--output','json']).stdout)['Account']=='208168340597'
    s.BASE,s.NS=o.output,o.namespace;s.K=['kubectl','--context',s.CTX,'-n',o.namespace]
    assert not s.kub('get','namespace',o.namespace,'--ignore-not-found','-o','name').strip()
    b.prepare(o)
    b.save(o.output/'profiler.py', pathlib.Path(__file__).read_text())
    b.save(o.output/'profile-protocol.json',dict(purpose='diagnostic; profiler-affected rates are not performance claims',cpus=[8,16],clients=128,pipeline=16,unprofiled_seconds=30,profiled_seconds=30,sampling='99 Hz software CPU clock with call stacks',binary='frozen prior candidate'))
    try:
        for name in ('resources.json','holder.manifest.json','client-48.json'):s.kub('create','-f',str(o.output/name))
        s.kub('wait','--for=condition=Ready','pod/client','--timeout=600s',timeout=610)
        assert s.memtier_hash()==s.MEMTIER_SHA256
        trace=o.output/'mixed.trace';b.save(trace,b.trace(mixed=True));s.kub('cp',str(trace),'client:/results/mixed.trace','-c','memtier')
        for cpu in o.cpus:
            out=o.output/f'c{cpu}-after';out.mkdir()
            host,engine,topology,instances=b.start(o,cpu,'after',out)
            if cpu==8:
                installed=ssm(instances['server'],'command -v perf || dnf install -y perf; perf --version',o.output/'perf-install',600)
                assert installed['Status']=='Success',installed
            pre=b.remote_job(['python3','-c',b.ZSET_CHECK,host,'preload'],'/results/preload-'+out.name,600)
            b.save(out/'preload.json',pre.stdout);assert pre.returncode==0,pre.stderr
            before=b.ena(out,'before',instances)
            rows=[b.phase(o,out,host,engine,topology,'mixed','warmup',10,4,16),b.phase(o,out,host,engine,topology,'mixed','unprofiled',30,4,16)]
            script='''set -eu
pid=$(pgrep -x vex)
test "$(printf '%s\\n' "$pid" | wc -l)" -eq 1
sleep 8
perf record -e cpu-clock -F 99 --call-graph fp -p "$pid" -o /tmp/vex-perf.data -- sleep 18 2>/tmp/vex-perf-record.log
cat /tmp/vex-perf-record.log
perf report -i /tmp/vex-perf.data --stdio --no-children --call-graph none --percent-limit 0.5 --sort comm,dso,symbol --stdio-color never | head -70
perf report -i /tmp/vex-perf.data --stdio --no-children --symbols __pthread_mutex_lock,__pthread_mutex_unlock,__pthread_mutex_timedlock --percent-limit 0.5 --sort comm,dso,symbol --stdio-color never | head -60
'''
            script = script.replace('/tmp/vex-perf.data', f'/tmp/vex-perf-{cpu}.data')
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                job=pool.submit(ssm,instances['server'],script,out/'perf-samples')
                rows.append(b.phase(o,out,host,engine,topology,'mixed','profiled',30,4,16))
                report=job.result();print('PROFILE',cpu,report['Status'],report['StandardErrorContent'][-500:],flush=True)
            after=b.ena(out,'after',instances)
            verified=b.remote_job(['python3','-c',b.ZSET_CHECK,host,'verify',str(sum(r['writes'] for r in rows))],'/results/verify-'+out.name,600)
            b.save(out/'verify.json',verified.stdout);assert verified.returncode==0,verified.stderr
            b.save(out/'profile-complete.json',dict(rows=rows,network_before=before,network_after=after))
            print('COMPLETE PROFILE',cpu,flush=True)
    finally:
        result=s.run(s.K+['delete','-f',str(o.output/'resources.json'),'--ignore-not-found','--wait=false'],check=False)
        b.save(o.output/'cleanup-request.log',result.stdout+result.stderr);print('CLEANUP',result.returncode,flush=True)


if __name__=='__main__':main()
