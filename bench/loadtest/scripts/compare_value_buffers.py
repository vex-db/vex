#!/usr/bin/env python3
"""Controlled follow-up against the published build; reuses the recorded harness."""
import json,pathlib,shutil,sys,time,statistics,hashlib
BASE=pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0,str(BASE/'vex/bench/loadtest/scripts'))
import value_size_sweep as v
import scrum_benchmark as b
from scrum_results import memory
v.NS='vex-optimize-20260922';v.OUT=pathlib.Path(__file__).resolve().parents[1]/'runs/value-size-followup';v.configure()
old=BASE/'vex/bench/loadtest/runs/2026-09-21-optimized/vex-linux-amd64'
compact=BASE/'vex-compact-values/bench/loadtest/runs/compact-values/v4/candidate-linux-amd64'
assert hashlib.sha256(compact.read_bytes()).hexdigest()=='200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e'
variants={'published':old,'compact':compact}
b.save('followup-settings.json',{'purpose':'Compare published memory-fix build with existing compact/reusable-value candidate','variants':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in variants.items()},'keys':1000000,'key_bytes':16,'connections':128,'pipeline':1,'warmup_seconds':10,'measured_seconds':20,'repeats':3,'server':'c6a.2xlarge','server_cpu_quota':6,'server_memory_gib':10,'client':'c6a.8xlarge','client_cpu_quota':24,'scenarios':{'32':['MIXED'],'256':['MIXED'],'4096':['GET','SET','MIXED']}})
b.kub('wait','--for=condition=Ready','pod/'+b.LOADGEN,'--timeout=300s',timeout=320)
b.save('client-pod.json',json.loads(b.kub('get','pod',b.LOADGEN,'-o','json')))
rows=json.loads((v.OUT/'comparison.json').read_text()) if (v.OUT/'comparison.json').exists() else []
first=v.OUT/'4096-published-pod.json'
server_node=json.loads(first.read_text())['spec']['nodeName'] if first.exists() else None
for size in [4096,32,256]:
 order=['published','compact'] if size!=32 else ['compact','published']
 for variant in order:
  expected_groups=3 if size==4096 else 1
  if sum(r['variant']==variant and r['value_bytes']==size for r in rows)==expected_groups:continue
  print('START',size,variant,flush=True)
  b.kub('delete','pod','benchmark-server','--ignore-not-found','--wait=true','--timeout=60s')
  shutil.copyfile(variants[variant],v.OUT/'vex-linux-amd64')
  b.start('vex','c6a.2xlarge',6)
  stem=f'{size}-{variant}';pod=json.loads(b.kub('get','pod','benchmark-server','-o','json'))
  if server_node is None:server_node=pod['spec']['nodeName']
  assert pod['spec']['nodeName']==server_node
  b.save(stem+'-pod.json',pod)
  v.mt(stem+'-preload',size,'1:0',preload=True)
  assert int(b.cli('DBSIZE'))==1000000
  b.save(stem+'-wire.json',v.verify_values(size));loaded=v.state(stem)
  groups=[]
  for workload,ratio in (v.WORKLOADS if size==4096 else [('MIXED','1:4')]):
   v.mt(stem+'-'+workload+'-warmup',size,ratio,10)
   runs=[]
   for i in range(1,4):
    r=v.mt(stem+'-'+workload+'-'+str(i),size,ratio);runs.append(r)
    print('DONE',stem,workload,i,round(r['ops']),r['p99_ms'],flush=True)
   groups.append({'variant':variant,'value_bytes':size,'workload':workload,'loaded_rss_mib':loaded,'runs':runs})
  mp=v.OUT/(stem+'-memory.jsonl');mp.write_text(b.execpod('benchmark-server','sampler','cat','/results/process.jsonl'))
  b.save(stem+'-cpu.stat',b.execpod('benchmark-server','server','cat','/sys/fs/cgroup/cpu.stat'))
  b.save(stem+'-server.log',b.kub('logs','benchmark-server','-c','server'))
  b.save(stem+'-final-wire.json',v.verify_values(size));assert int(b.cli('DBSIZE'))==1000000
  for g in groups:
   for r in g['runs']:r.update(memory(mp,r['start'],r['end']))
   for key in ['ops','p99_ms','server_cpu_cores','client_cpu_cores']:g[key]=statistics.median(r[key] for r in g['runs'])
   g['peak_rss_mib']=max(r['peak_rss_mib'] for r in g['runs'])
  rows+=groups;b.save('comparison.json',rows)
  print('SAVED',stem,'loaded',round(loaded,1),'peak',round(max(g['peak_rss_mib'] for g in groups),1),flush=True)
b.save('client-final-cpu.stat',b.execpod(b.LOADGEN,'loadgen','cat','/sys/fs/cgroup/cpu.stat'))
print('COMPLETE 30 matched measurements',flush=True)
