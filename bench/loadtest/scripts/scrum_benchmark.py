#!/usr/bin/env python3
"""Matched scrum-only benchmark. All workload mutations use one namespace.
Run bootstrap, then run; cleanup deletes only this run's resources.
Raw memtier JSON/logs and per-second process RSS/CPU samples stay in --output.
"""
import argparse, json, subprocess, time, pathlib, sys
from scrum_results import metrics

CTX='scrum-fresh'
NS='vex-bench-20260921'
LOADGEN='loadgen'
IMAGES={
 'vex':'ghcr.io/pratyush-sngh/vex@sha256:92c13d2f90d594a0c6b15b47884916ed4dab8da96e436ece4e6f94254d222e4b',
 'redis':'redis@sha256:5edb5f1591cd35076057573171f40e0439ec7fbbb38e04d56c0efae810d99d47',
 'dragonfly':'docker.dragonflydb.io/dragonflydb/dragonfly@sha256:ba22a11c729cc90e50fbaf527d800defbd2048d4f3a9ef8dfe05c0806cd1e078',
 'memtier':'redislabs/memtier_benchmark@sha256:6d18c5358a6c4af7ec40cba4fd8a03e7fb77a9f99a718db3950de116d8df622c',
 'python':'python:3.12-slim',
}
SAMPLER=r'''
import json,time,os,pathlib
with open('/results/process.jsonl','a',buffering=1) as f:
 while True:
  records=[]
  for p in pathlib.Path('/proc').glob('[0-9]*'):
   try:
    name=(p/'comm').read_text().strip()
    if not (name in ('redis-server','dragonfly') or name.startswith('vex') or name.startswith('memtier')): continue
    st={a:b.strip() for a,b in (line.split(':',1) for line in (p/'status').read_text().splitlines() if ':' in line)}
    stat=(p/'stat').read_text().rsplit(')',1)[1].split()
    records.append({'pid':int(p.name),'name':name,'rss_bytes':int(st.get('VmRSS','0 kB').split()[0])*1024,'hwm_bytes':int(st.get('VmHWM','0 kB').split()[0])*1024,'cpu_seconds':(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK')})
   except (OSError,ValueError,IndexError): pass
  f.write(json.dumps({'time':time.time(),'processes':records})+'\n'); time.sleep(1)
'''

def kub(*args,input=None,timeout=120):
 p=subprocess.run(['kubectl','--context',CTX,'-n',NS,*args],input=input,text=True,capture_output=True,timeout=timeout)
 if p.returncode: raise RuntimeError(f'kubectl {args[:4]}: {p.stderr[-2500:]}')
 return p.stdout

def save(name,data):
 (OUT/name).write_text(data if isinstance(data,str) else json.dumps(data,indent=2))

def apply(obj):
 save(obj['metadata']['name']+'.manifest.json',obj)
 return kub('apply','-f','-',input=json.dumps(obj))

def pod(name,role,size,containers,monitor=False):
 spec={'restartPolicy':'Never','terminationGracePeriodSeconds':5,'activeDeadlineSeconds':10000,'automountServiceAccountToken':False,
  'nodeSelector':{'karpenter.sh/nodepool':NS,'node.kubernetes.io/instance-type':size,'bench.vex.dev/role':role},
  'tolerations':[{'key':'vex-benchmark','value':'20260921','operator':'Equal','effect':'NoSchedule'}],
  'containers':containers}
 if monitor:
  spec.update(shareProcessNamespace=True,volumes=[{'name':'results','emptyDir':{}}])
  spec['containers'].append({'name':'sampler','image':IMAGES['python'],'command':['python3','-u','-c',SAMPLER],
    'volumeMounts':[{'name':'results','mountPath':'/results'}],
    'resources':{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'cpu':'200m','memory':'128Mi'}}})
 return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'labels':{'app':'vex-benchmark','role':role},'annotations':{'karpenter.sh/do-not-disrupt':'true'}},'spec':spec}

def bootstrap():
 driver={'name':'loadgen','image':IMAGES['memtier'],'command':['sh','-c','sleep 10000'],'resources':{'requests':{'cpu':'12','memory':'2Gi'},'limits':{'cpu':'12','memory':'4Gi'}}}
 cli={'name':'cli','image':IMAGES['redis'],'command':['sh','-c','sleep 10000'],'resources':{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'cpu':'200m','memory':'128Mi'}}}
 apply(pod('loadgen','client','c6a.4xlarge',[driver,cli],True))
 # Holder keeps the same physical node across engines in each size tier.
 holder('c6a.xlarge')
 apply({'apiVersion':'v1','kind':'Service','metadata':{'name':'benchmark-server'},'spec':{'selector':{'app':'vex-benchmark','endpoint':'server'},'ports':[{'port':6379,'targetPort':6379}]}})
 save('images.json',IMAGES)

def holder(size):
 apply(pod('hold-'+size.replace('.','-'),'server',size,[{'name':'hold','image':IMAGES['redis'],'command':['sh','-c','sleep 10000'],'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'50m','memory':'64Mi'}}}]))

def execpod(podname,container,*args,timeout=120):
 return kub('exec',podname,'-c',container,'--',*args,timeout=timeout)

def cli(*args):
 return execpod(LOADGEN,'cli','timeout','15','redis-cli','-h','benchmark-server','-p','6379','--raw',*args).strip()

def mt(stem,ratio,seconds,threads=None,connections=None,preload=False):
 if threads is None: threads=32 if LOADGEN=="loadgen-large" else 16
 if connections is None: connections=128//threads
 args=['memtier_benchmark','-s','benchmark-server','-p','6379','-t',str(threads),'-c',str(connections),
  '--pipeline','128' if preload else '1','--ratio',ratio,'--key-minimum','1','--key-maximum','1000000','--data-size','256',
  '--hide-histogram','--print-percentiles','50,95,99','--json-out-file',f'/tmp/{stem}.json']
 args+=['--requests','1000000','--key-pattern','S:S'] if preload else ['--test-time',str(seconds),'--key-pattern','R:R','--distinct-client-seed']
 t0=time.time()
 proc=subprocess.run(['kubectl','--context',CTX,'-n',NS,'exec',LOADGEN,'-c','loadgen','--',*args],capture_output=True,text=True,timeout=max(seconds+90,600 if preload else 120))
 t1=time.time();save(stem+'.log',proc.stdout);save(stem+'.stderr.log',proc.stderr)
 if proc.returncode: raise RuntimeError(f'{stem}: memtier failed: {proc.stderr[-1500:]}')
 raw=execpod(LOADGEN,'loadgen','cat',f'/tmp/{stem}.json');save(stem+'.json',raw)
 data=json.loads(raw)
 metrics(data)
 return t0,t1,data

def start(engine,size,cpus):
 args={'vex':['--reactor','--workers',str(cpus),'--no-persistence','--port','6379'],
  'redis':['redis-server','--port','6379','--save','','--appendonly','no','--io-threads',str(cpus)],
  'dragonfly':[f'--proactor_threads={cpus}','--port=6379','--maxmemory=4gb','--snapshot_cron=']}[engine]
 c={'name':'server','image':IMAGES[engine],'args':args,'ports':[{'containerPort':6379}],
   'resources':{'requests':{'cpu':str(cpus),'memory':'4Gi'},'limits':{'cpu':str(cpus),'memory':'4Gi'}},
   'readinessProbe':{'tcpSocket':{'port':6379},'periodSeconds':2,'failureThreshold':90}}
 if engine=='vex':
  c['command']=['sh','-ec','while [ ! -x /tmp/vex-benchmark ]; do sleep 1; done; exec /tmp/vex-benchmark \"$@\"','vex-benchmark']
 obj=pod('benchmark-server','server',size,[c],True);obj['metadata']['labels']['endpoint']='server'
 apply(obj)
 if engine=='vex':
  kub('wait','--for=jsonpath={.status.phase}=Running','pod/benchmark-server','--timeout=300s',timeout=320)
  kub('cp',str(OUT/'vex-linux-amd64'),'benchmark-server:/tmp/vex-upload','-c','server',timeout=120)
  execpod('benchmark-server','server','sh','-ec','chmod 755 /tmp/vex-upload; mv /tmp/vex-upload /tmp/vex-benchmark')
 kub('wait','--for=condition=Ready','pod/benchmark-server','--timeout=300s',timeout=320)
 save(f'{size}-{engine}-pod.json',json.loads(kub('get','pod','benchmark-server','-o','json')))
 for _ in range(20):
  try:
   if cli('PING')=='PONG':break
  except RuntimeError:pass
  time.sleep(1)
 else:raise RuntimeError('server service is not ready')
 save(f'{size}-{engine}-info.txt',cli('INFO'))

def run():
 kub('wait','--for=condition=Ready','pod/'+LOADGEN,'--timeout=300s',timeout=320)
 save('memtier-version.txt',execpod(LOADGEN,'loadgen','memtier_benchmark','--version'))
 save('nodes-start.json',json.loads(kub('get','nodes','-l','karpenter.sh/nodepool='+NS,'-o','json')))
 rows=json.loads((OUT/'cells.json').read_text()) if (OUT/'cells.json').exists() else []
 def completed(size,engine):
  return sum(r['machine']==size and r['engine']==engine for r in rows)==9 and (OUT/f'{size}-{engine}-memory.jsonl').exists()
 for index,(size,cpus) in enumerate([('c6a.xlarge',3),('c6a.2xlarge',6),('c6a.4xlarge',12)]):
  if all(completed(size,e) for e in ['vex','redis','dragonfly']): continue
  holder(size)
  engines=['vex','redis','dragonfly'];engines=engines[index:]+engines[:index]
  for engine in engines:
   if completed(size,engine): continue
   kub('delete','pod','benchmark-server','--ignore-not-found','--wait=true','--timeout=60s')
   rows=[r for r in rows if not (r['machine']==size and r['engine']==engine)]
   save('cells.json',rows)
   print(f'START {size} {engine} cpus={cpus}',flush=True)
   start(engine,size,cpus);stem=f'{size}-{engine}'
   mt(stem+'-preload','1:0',0,1,1,True)
   count=int(cli('DBSIZE'));assert count==1000000,(stem,count)
   time.sleep(3)
   save(stem+'-loaded-memory.txt',cli('INFO','memory'))
   save(stem+'-loaded-at.json',{'time':time.time(),'keys':count})
   for workload,ratio in [('GET','0:1'),('SET','1:0'),('MIXED','1:4')]:
    mt(stem+'-'+workload+'-warmup',ratio,10)
    for repeat in range(3):
     cell=f'{stem}-{workload}-{repeat+1}'
     t0,t1,data=mt(cell,ratio,20)
     row={'machine':size,'client_pod':LOADGEN,'server_cpu_quota':cpus,'engine':engine,'workload':workload,'repeat':repeat+1,'pipeline':1,'connections':128,'value_bytes':256,'keys':count,'start':t0,'end':t1,'file':cell+'.json'}
     rows.append(row);save('cells.json',rows)
     m=metrics(data)
     print(f'DONE {cell}: {m["ops"]:,.0f} ops/s, p99={m["p99_ms"]:.3f}ms, client={m["client_cpu_cores"]:.1f} cores',flush=True)
   save(stem+'-memory.jsonl',execpod('benchmark-server','sampler','cat','/results/process.jsonl'))
   save(stem+'-final-info.txt',cli('INFO'))
   save(stem+'-server.log',kub('logs','benchmark-server','-c','server'))
   kub('delete','pod','benchmark-server','--wait=true','--timeout=60s')
  kub('delete','pod','hold-'+size.replace('.','-'),'--wait=true','--timeout=60s')
 save('client-memory.jsonl',execpod(LOADGEN,'sampler','cat','/results/process.jsonl'))
 save('nodes-end.json',json.loads(kub('get','nodes','-l','karpenter.sh/nodepool='+NS,'-o','json')))
 print('COMPLETE all raw results saved',flush=True)

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('mode',choices=['bootstrap','run','cleanup']);a.add_argument('--client-pod',default='loadgen');a.add_argument('--output',default='bench/loadtest/runs/2026-09-21-scrum');opt=a.parse_args()
 LOADGEN=opt.client_pod
 OUT=pathlib.Path(opt.output);OUT.mkdir(parents=True,exist_ok=True)
 if opt.mode=='bootstrap':bootstrap()
 elif opt.mode=='run':run()
 else:
  kub('delete','namespace',NS,'--wait=false')
  kub('delete','nodepool',NS,'--wait=false')
