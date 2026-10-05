# Pipeline-1 investigation: accounting follow-up

[Updated benchmark table and calculated scaling](request-scaling/comparison.md)
consolidates all 15 confirmed CPU/workload combinations, with no interpolated cells.

This page combines the completed October 5 measurements with the earlier
October 1 CPU-accounting investigation. Each host cohort remains separate;
the new run uses the same frozen Vex binary, with no engine changes.

## October 5 measurement status

The r6 run completed all nine cells with **27/27 passing 60-second
confirmations**. All 27 confirmation accounting brackets passed the delay audit,
and all completed cells shared the same server boot. New same-host scaling:

| Workload | 2 → 4 throughput gain | 4 → 8 throughput gain |
|---|---:|---:|
| GET | +91.0% | +73.2% |
| Hash | +87.6% | +71.5% |
| Sorted set | +86.9% | +73.9% |

The final 2-CPU sorted-set cell initially stopped after AWS/EKS authentication
expired. After refresh, the runner verified the original host and boot identities,
retained eight completed cells, and restarted only the interrupted cell with a
fresh dataset. Interrupted artifacts remain excluded under the raw run’s
`interrupted/` directory.

**Cleanup verified:** both benchmark instances are terminated, with no remaining
run-owned namespace, NodePools, nodes or NodeClaims.
[Verification record](request-scaling-r6/cleanup-verification.json).

[New results and protocol](request-scaling-r6/comparison.md) ·
[New CPU audit](request-accounting-r6/accounting.md)

## Findings from October 1

GET throughput rose from 858,742 ops/s at an 8-CPU quota and 256 connections
to 1,523,294 ops/s at a 16-CPU quota and 512 connections: 1.774×. Both points
are medians of three 60-second pipeline-1 confirmations below 5 ms p99.
This compares each allocation's selected concurrency, not fixed concurrency
or dedicated physical cores. It does not establish 4-to-8 scaling.

After excluding collection brackets longer than measured runtime +25%:

| GET allocation | Accepted accounting samples | Vex user µs/op | Vex system µs/op | Whole-host system + softirq ticks/million ops |
|---|---:|---:|---:|---:|
| 8 CPUs, 256 connections | 3/3 | 1.293 | 7.913 | 1163.369 |
| 16 CPUs, 512 connections | 2/3 | 1.286 | 9.047 | 1126.662 |

Vex-accounted system cost rises about 14%, while whole-host system/softirq
cost is about 3% lower. This is consistent with CPU charging moving between
Vex and other host contexts; it is not proof of the cause. Whole-host counters
include background activity, groups are small, and no confidence interval is
claimed. USER_HZ was not recorded, so host tick values are intentionally not
converted into microseconds or directly compared numerically with Vex costs.

All three 16-CPU sorted-set accounting brackets are too wide for this host-cost
comparison. Their original throughput confirmations remain separate evidence;
they cannot fill this attribution gap. The saved pipeline diagnostic still
shows batching efficiency, but is not the primary application-performance
benchmark and is not evidence of a code optimization.

The code already queues io_uring sends/receives and uses combined submit/wait.
The September 24 profile also found overlapping networking stacks and host
accounting effects. Repeating that broad conclusion is insufficient reason
to remove locks, rewrite ownership, or change defaults.

## Missing-cell measurement follow-up

On October 5, AWS/EKS access was refreshed. Neither previous r5 instance was
returned by EC2; there were no active instances under its NodePool tags, no
nodes, no NodeClaims and no pods. The stale namespace and two NodePools were
deleted and absence verified. The raw r5 directory records the cleanup evidence.

The requested missing-cell run is r6: all three workloads at 2, 4 and 8 CPUs
on one new host pair. It filled six gaps and repeated three existing cells to
provide same-host 2 → 4 → 8 comparisons. It uses the identical measured binary,
pipeline 1 and existing confirmation gates. Results are saved separately from
r4/r5; the collector prevents idle sleep, and delayed accounting brackets must
still be excluded. [r6 progress/results](request-scaling-r6/comparison.md).

## Subsequent targeted diagnostic

The missing-cell run supplies throughput evidence; the following profile remains
a separate investigation and is not part of that run.

The next diagnostic should hold pipeline depth at 1 and cross 8/16 CPUs
with 256/512 connections on the same hosts. Collect three 60-second
unprofiled controls per cell, plus separate 20-second CPU-clock profiles
excluded from throughput claims. Start with GET; use hash/sorted-set follow-ups
only where the GET result identifies a specific hypothesis to test.

Capture process and whole-host CPU (including recorded USER_HZ), cgroup
throttling, per-worker CPU, sample loss, futex/lock versus TCP/send/receive
stacks, and client/network headroom. Kernel inclusive shares overlap and
must not be added together. Keep the build, dataset, affinity policy and
network topology fixed. New ownership or I/O changes require an observed
cost and a same-setting A/B comparison that preserves correctness.

Interrupted local collection repeatedly consumed the previous run budgets.
Before another sweep, use bounded remote measurement/collection with retained
artifacts so a suspended local session cannot silently stretch accounting
brackets. Do not restart the full 15-cell matrix as a substitute for this
focused diagnostic.

## Reproduce locally

```sh
python3 bench/loadtest/scripts/test_request_accounting.py
python3 bench/loadtest/scripts/analyze_request_accounting.py \
  bench/loadtest/runs/2026-10-01-request-scaling-aws-r4 \
  docs/benchmarks/2026-10-05/request-accounting-r4
```

Raw runs remain local and ignored. [Accounting results](request-accounting-r4/accounting.md)
record which samples were excluded and preserve the underlying units.
