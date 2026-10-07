#!/usr/bin/env python3
"""Validate raw memtier output and summarize repeated three-engine measurements."""
import json, pathlib, statistics, sys, collections, csv

def metrics(data):
 stats=data['ALL STATS'];t=stats['Totals']
 assert t['Count']>0 and t['Ops/sec']>0,'Empty benchmark'
 for section in stats.values():
  if isinstance(section,dict):
   assert section.get('Connection Errors',0)==0,'Connection errors'
 assert t.get('Misses/sec',0)==0,'GET misses: keyspace was not fully loaded'
 assert stats['Runtime'].get('Interrupted') not in (True,'true'),'Interrupted run'
 p=t['Percentile Latencies']
 return {'ops':t['Ops/sec'],'p50_ms':p['p50.00'],'p95_ms':p['p95.00'],'p99_ms':p['p99.00'],
  'client_cpu_cores':stats['CPU']['cpu_cores_used'],'client_max_thread_avg_pct':max(t['cores_used'] * 100 for t in stats['CPU']['Per Thread'].values()),
  'rx_kib_s':t['KB/sec RX'],'tx_kib_s':t['KB/sec TX']}

def memory(path,start,end):
 samples=[]
 for line in path.read_text().splitlines():
  v=json.loads(line)
  if start<=v['time']<=end:
   procs=[p for p in v['processes'] if (p['name'] in ('redis-server','dragonfly') or p['name'].startswith('vex'))]
   if procs: samples.append((v['time'],sum(p['rss_bytes'] for p in procs),sum(p['cpu_seconds'] for p in procs)))
 assert len(samples)>=2,f'Insufficient RSS samples: {path}'
 return {'peak_rss_mib':max(x[1] for x in samples)/1048576,'mean_rss_mib':statistics.mean(x[1] for x in samples)/1048576,
  'server_cpu_cores':(samples[-1][2]-samples[0][2])/(samples[-1][0]-samples[0][0])}

def summarize(root):
 cells=json.loads((root/'cells.json').read_text());raw=[]
 for c in cells:
  data=json.loads((root/c['file']).read_text())
  assert data['configuration']['pipeline']==1
  assert data['configuration']['threads']*data['configuration']['clients']==c['connections']
  row=dict(c,**metrics(data));prefix=f'{c["machine"]}-{c["engine"]}'
  mp=root/(prefix+'-memory.jsonl')
  if not mp.exists():continue # An engine still running has not exported its sampler yet.
  row.update(memory(mp,c['start'],c['end']))
  loaded=json.loads((root/(prefix+'-loaded-at.json')).read_text())
  idle=memory(mp,loaded['time']-3,loaded['time'])
  row['loaded_rss_mib']=idle['mean_rss_mib'];raw.append(row)
 groups=collections.defaultdict(list)
 for r in raw:groups[(r['machine'],r['engine'],r['workload'])].append(r)
 out=[]
 for (machine,engine,workload),rows in groups.items():
  m={'machine':machine,'engine':engine,'workload':workload,'repeats':len(rows),'cpu_quota':rows[0]['server_cpu_quota'],'client_machine':'c6a.8xlarge' if rows[0].get('client_pod')=='loadgen-large' else 'c6a.4xlarge','client_cpu_quota':24 if rows[0].get('client_pod')=='loadgen-large' else 12}
  for k in ('ops','p50_ms','p95_ms','p99_ms','client_cpu_cores','server_cpu_cores','loaded_rss_mib','peak_rss_mib','mean_rss_mib','client_max_thread_avg_pct'):
   m[k]=statistics.median(r[k] for r in rows)
  m['ops_min']=min(r['ops'] for r in rows);m['ops_max']=max(r['ops'] for r in rows)
  m['sampled_peak_rss_mib']=max(r['peak_rss_mib'] for r in rows)
  out.append(m)
 (root/'summary.json').write_text(json.dumps(out,indent=2))
 if out:
  with (root/'summary.csv').open('w') as f:
   w=csv.DictWriter(f,fieldnames=list(out[0]));w.writeheader();w.writerows(out)
 for r in out: print(f'{r["machine"]:13} {r["engine"]:10} {r["workload"]:6} {r["ops"]:10,.0f} ops/s p99={r["p99_ms"]:.3f}ms RSS={r["peak_rss_mib"]:.1f}MiB CPU={r["server_cpu_cores"]:.1f} n={r["repeats"]}')
 return out

if __name__=='__main__':
 root=pathlib.Path(sys.argv[1])
 if root.name=='parser-smoke.json':
  d=json.loads(root.read_text());assert metrics(d)['ops']>0
  d['ALL STATS']['Totals']['Misses/sec']=1
  try:metrics(d)
  except AssertionError:pass
  else:raise AssertionError('Miss validation did not reject bad data')
  print('Parser smoke check passed, including bad-data rejection')
 else:summarize(root)
