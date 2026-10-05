#!/usr/bin/env python3
"""Summarize completed scaling cells without treating missing cells as results."""
import argparse
import json
import pathlib
import os

p=argparse.ArgumentParser();p.add_argument('run',type=pathlib.Path);p.add_argument('report',type=pathlib.Path);p.add_argument('--continuation',type=pathlib.Path);p.add_argument('--supplement',type=pathlib.Path);o=p.parse_args()
rows=[json.loads(f.read_text()) for f in sorted(o.run.glob('cells/*/complete.json'))]
by={(r['cpus'],r['engine']):r for r in rows}
overlap=[]
for run in [x for x in (o.continuation,o.supplement) if x]:
 for f in sorted(run.glob('cells/*/complete.json')):
  r=json.loads(f.read_text());key=(r['cpus'],r['engine'])
  if key in by:overlap.append((by[key],r))
  by[key]=r
rows=list(by.values())
engines=['candidate','redis','dragonfly'];names={'candidate':'Vex rc.3','redis':'Redis 8.10.1','dragonfly':'Dragonfly 2.0.0'}
rows.sort(key=lambda r:(r['cpus'],engines.index(r['engine'])))
lines=['# CPU scaling with scheduled arrivals — '+('26–27 September 2026' if o.continuation else '26 September 2026'),'',f'{len(rows)} of 15 engine/CPU cells completed. Results below include only completed, validated cells; missing cells are not zero capacity.','','The SLO is 99% of offered requests completed within 1 ms, with zero errors/loss after drain and at least 99% completed inside the arrival window. Latency includes waiting from the intended arrival time, not just socket round trips.','','## Confirmed offered operations per second','','Each number passed three 30-second confirmations. A dash means no confirmed rate is available.','','| CPU quota / workers | Vex rc.3 | Redis | Dragonfly |','|---|---:|---:|---:|']
for cpu in (1,2,4,8,16):
 values=[by.get((cpu,e),{}).get('confirmed_rate') for e in engines]
 lines.append(f'| {cpu} | '+' | '.join(f'{v:,}' if v is not None else '—' for v in values)+' |')
if o.continuation:
 lines+=['','The original hosts were unavailable after credentials expired. Continuation results use a fresh pair of c7g.8xlarge instances in the same availability zone. The table uses continuation results wherever available; earlier 1/2/4-CPU results remain from the original pair. Repeated 8-CPU points provide overlap controls for the first continuation. A separate Vex 16-CPU run, if linked below, uses a third host pair after the Dragonfly startup failure caused cleanup. Host differences remain a confounder for cross-run scaling ratios.','','## Repeated points across host pairs','','Host changes, measurement variation, and the coarse search bracket can all affect these differences; they are not isolated causal effects.','','| CPUs | Engine | Original confirmed ops/s | Continuation confirmed ops/s |','|---:|---|---:|---:|']
 for before,after in overlap:
  lines.append(f"| {after['cpus']} | {names[after['engine']]} | {before['confirmed_rate'] or '—'} | {after['confirmed_rate'] or '—'} |")
failures=[json.loads(f.read_text()) for run in [x for x in (o.run,o.continuation,o.supplement) if x] for f in run.glob('cells/*/failure.json')]
if failures:
 lines+=['','## Configurations that could not start','']
 for r in failures:
  lines.append(f"- {names[r['engine']]}, {r['cpus']} CPUs: {r['reason']}. This is a configuration/startup failure, not zero throughput or a measured performance ceiling.")
lines+=['','## Search boundaries and confidence','','A bracket is workload-specific. Client-limited trials are inconclusive about the database limit. A passing rate at the tested ceiling establishes only a lower bound. An unstable boundary means the longer upper-rate check passed despite an earlier short failure. The lowest rejected long-run rate includes failed confirmations; a single observed rejection is not proof that every run at that rate fails. Coarse fallback steps can leave wide uncertainty intervals.','','| CPUs | Engine | Confirmed offered rate | Upper tested rate | Lowest rejected long-run rate, valid client | Classification | Confirmation p99 range (ms) |','|---:|---|---:|---:|---:|---|---|']
for cpu in (1,2,4,8,16):
 for e in engines:
  r=by.get((cpu,e))
  if not r:continue
  vals=[x['p99_ms'] for x in r['confirmations']]
  display=f'{min(vals):.3f}–{max(vals):.3f}' if vals and all(isinstance(x,(int,float)) for x in vals) else str(vals)
  long_trials=r.get('confirmation_history',r['confirmations'])+([r['boundary_check']] if r.get('boundary_check') else [])
  rejected=[x['rate'] for x in long_trials if x['client_valid'] and not x['pass']]
  lines.append(f"| {cpu} | {names[e]} | {r['confirmed_rate'] or '—'} | {r['first_failing_or_client_limited_rate'] or '—'} | {min(rejected) if rejected else '—'} | {r['classification']} | {display} |")
failed=[(r,x) for r in rows for x in r.get('confirmation_history',r['confirmations']) if not x['pass']]
if failed:
 lines+=['','## Failed confirmation trials retained','','These rates were not accepted merely because an earlier short probe passed.','','| CPUs | Engine | Offered ops/s | Scheduled p99 ms | Client valid |','|---:|---|---:|---|---|']
 for r,x in failed:
  lines.append(f"| {r['cpus']} | {names[r['engine']]} | {x['rate']:,} | {x['p99_ms']} | {x['client_valid']} |")
lines+=['','## Uncapped closed-loop cross-check','','Same 1,024 connections, pipeline 1, and GET/SET mix using the existing patched memtier. These short throughput measurements do not satisfy the scheduled-arrival SLO by themselves.','','| CPUs | Engine | Achieved ops/s | Service p99 ms | Client headroom check |','|---:|---|---:|---:|---|']
for r in rows:
 u=r.get('uncapped_crosscheck')
 if u:lines.append(f"| {r['cpus']} | {names[r['engine']]} | {u['ops_per_sec']:,.0f} | {u['p99_ms']:.3f} | {u['client_headroom_ok']} |")
lines+=['','## Scope and controls','','- CPU-quota scaling on a c7g.8xlarge Graviton3 server with a separate c7g.8xlarge load generator (each machine: 32 vCPUs, 64 GiB RAM); this is not an EC2 price comparison. Equal 4 GiB engine memory; no persistence; matched worker/I/O-thread counts; fixed dataset and fixed hosts within each run. CPU quota is not exclusive physical-core pinning.','- Uniform 80% GET / 20% SET, 100,000 preloaded 256-byte values, no expiration, 1,024 connections, pipeline 1. These results do not generalize automatically to the TTL and hot-key profiles from the earlier realistic pilot.','- Independent deterministic arrival schedules, bounded connection queues, explicit drops, monotonic scheduled latency, and no retries. Client gates require dispatch p99 <=0.25 ms, CPU below 80% of its 24-CPU quota, and throttling below 1% of the arrival window. Total captured CPU/throttling divided by window length gives conservative upper bounds even though snapshots include startup/export overhead.','- Start at 32k or an explicitly recorded prior-rate search hint, double or probe the prior upper boundary, descend if necessary, refine twice, then confirm the passing rate three times and the upper boundary once. Failed confirmations trigger up to two step-downs, each requiring a fresh set of three passing confirmations. The resolution is a bracket, not an exact universal maximum. Keep all failed and inconclusive phases.','- All commands pre-encoded and connections opened before arrival timing. Histograms have 10 μs buckets and a >=100 ms overflow bucket; SLO violation counts use exact durations. Snapshots also include process startup/export overhead, so CPU averages are not phase-perfect.','- Raw per-phase histograms, offered/completed/lost counts, CPU throttling, configuration, process/node identities, source snapshots, image digests, and executable hashes are retained.','','See [methodology](../bench/loadtest/SCALING.md).']
lines+=['',f"[Raw artifacts, protocol, and source snapshots]({os.path.relpath(o.run.resolve(),o.report.parent.resolve())}/)."]
if o.continuation:
 lines+=['',f"[Continuation artifacts and source snapshots]({os.path.relpath(o.continuation.resolve(),o.report.parent.resolve())}/)."]
if o.supplement:
 lines+=['',f"[Final Vex 16-CPU artifacts and source snapshots]({os.path.relpath(o.supplement.resolve(),o.report.parent.resolve())}/)."]
o.report.parent.mkdir(parents=True,exist_ok=True);o.report.write_text('\n'.join(lines)+'\n')
print(o.report)
