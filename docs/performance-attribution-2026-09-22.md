# Saturation and attribution run — 2026-09-22

Status: matrix and diagnostics complete; independently reviewed. The bounded runner is
`bench/loadtest/scripts/profile_saturation.py`; it does not create the
namespace or NodePool. The coordinating run supplies context `scrum-fresh`,
namespace `vex-saturation-20260922`, and output
`bench/loadtest/runs/2026-09-22-saturation`.

The matrix uses one fresh six-worker server process for all eight cells; its
pod UID and node are checked before and after every cell. The stable client
pod UID and node are checked the same way. The compact binary SHA256 is
`200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e`, with one
million fixed 16-byte keys, 256-byte values, 32 memtier threads, and an exact
1:4 SET:GET ratio. The server is c6a.2xlarge (6 CPU, 10 GiB) and the client
is loadgen-large c6a.8xlarge (24 CPU). Warmup is 10 seconds and the timed
sample is 30 seconds.

The eight validated rows completed with one server pod/process and one
preload. The modern io_uring backend was reported by all six workers, the
binary hash matched, and the server and client pod identities/nodes stayed
fixed. Every row had zero connection errors and zero misses; all 32 sampled
values were valid before and after each cell.
No error, miss, throttle, or client-headroom constraint flags were set.

| Direction | Pipeline | Connections | Threads × clients |
|---|---:|---:|---:|
| forward/reverse | 1 | 32 | 32 × 1 |
| forward/reverse | 1 | 128 | 32 × 4 |
| forward/reverse | 1 | 512 | 32 × 16 |
| forward/reverse | 8 | 128 | 32 × 4 |

Measured matrix results (one forward and one reverse sample per row; p99 is
the median of those two reported p99 values, with the observed range shown):

| Pipeline | Connections | Ops/s median (range) | p99 median ms (range) | Server CPU µs/op | Peak server RSS MiB |
|---:|---:|---:|---:|---:|---:|
| 1 | 32 | 180,708 (178,634–182,781) | 0.347 (0.271–0.423) | 24.79 | 468.8 |
| 1 | 128 | 321,560 (320,266–322,855) | 0.751 (0.623–0.879) | 18.29 | 468.8 |
| 1 | 512 | 315,201 (314,550–315,852) | 4.431 (2.399–6.463) | 17.91 | 468.7 |
| 8 | 128 | 1,889,770 (1,879,935–1,899,605) | 0.819 (0.799–0.839) | 3.12 | 466.9 |

The CPU column is server cgroup CPU time per completed operation; the p1/c128
median is 1.04 user plus 17.26 system µs/op. No row crossed the predeclared
1% throttle threshold.

Each raw cell records operation count, throughput, p99, validated memtier
configuration and duration, zero-error/miss checks, key count, sampled value
checks, binary hash, backend startup lines, server/client pod UIDs and nodes,
RSS samples, process and cgroup CPU deltas, CPU time per operation, and
before/after `/proc/stat`, `/proc/softirqs`, `/proc/net/dev`, and
`/proc/net/snmp` snapshots for both server and client scopes. Server and
client cgroup `cpu.stat` snapshots retain user/system usage and throttling
counters.

The proc/stat, softirq, netdev, and SNMP captures use a wider before/after
window than the 30-second timing interval. proc/stat and softirq are nodewide;
the netdev and SNMP files are benchmark-container captures. They are
attribution context, not interval-normalized timing results.

Throttle is classified after the cell, never used to abort it. A cell is
`constrained` when either `throttled_usec / (wall_usec × declared_cpu_quota)`
or `throttled_usec / wall_usec` reaches the predeclared 1% threshold. The
classification and both ratios remain in the row so a constrained result can
be excluded from a later comparison without hiding it.

Run the local fixture check with:

```sh
python3 bench/loadtest/scripts/profile_saturation.py --self-test
```

The parent diagnostic can reuse the configured runner without restarting the
server: `configure(namespace, output, context)` followed by
`run_mt(stem, connections, pipeline, seconds=20)` returns the validated raw
sample and timing bounds.

The independent review identifies 128 connections as the lowest tested
pipeline-1 plateau candidate, while 512 connections has materially worse p99
in both directions. Pipeline 8 is retained as a diagnostic for batching effects; the optimization
target remains unpipelined traffic. It does not establish an engine improvement claim. The
runner's advisory selector was corrected after this matrix ran to choose the
previous level (`p1-c128`) when a plateau is detected; the recorded timing
rows are unaffected.

## CPU profile and timing probes

The separate 20-second workload and process-scoped cpu-clock profile were
recovered after a kubectl transport interruption. The complete remote memtier
JSON passes duration, configuration, error and miss validation. The finalized
perf data is readable and reports zero lost samples. The recorder's exit code
is unknown because the client connection closed; the post-capture task
snapshot was collected later and is not an exact CPU-rate denominator.
Instrumented results are excluded from the eight benchmark samples.

The 11,835 samples cover all six active worker threads, with 1,970–1,974
samples each on their assigned CPUs 0–5. Kernel leaves account for 11,160
samples (94.30%); userspace leaves account for 675 (5.70%). Kernel function
names are unavailable: `/proc/kallsyms` contains only zero addresses. About
91.8% of samples have `IoUring.enter` in their ancestry, which includes kernel
work through the call and does not identify io_uring bookkeeping, TCP, or
sleeping as the cause.

Resolved userspace self-costs are small: key equality approximately 1.10%,
map lookup 0.88%, and memory copying 0.68%. The broad, inlined Worker.run
symbol accounts for 1.46%. These fractions do not support a large userspace
optimization opportunity in this measured workload.

The timing probes record 6,056,995 command dispatches, matching the diagnostic
workload's 6,056,994 operations plus one control command. There are 520,181
ring-entry ticks: approximately 11.64 commands per entry. Vex already batches
work from multiple connections at pipeline 1. Weighted instrumented GET lock
acquisition averages about 107 ns and lookup about 688 ns; no lock-dominance
claim is supported. Probe spans are nested wall-clock measurements with
instrumentation overhead, so they must not be added together as CPU costs.
The wait-enter maximum includes idle time during control calls. CQE averages
are event counts despite their column labels; absent SET subprobes are
uninstrumented paths, not zero-cost work.

## Packet and node counters

Pod eth0 and TCP counters show approximately one received and one transmitted
packet per operation at pipeline 1, versus one-eighth each at pipeline 8.
No new drops or retransmissions appear in the captured intervals. These are
pod/veth counters, not physical NIC wire measurements; their wider windows
include small control traffic.

Whole-node CPU counters attribute about 72% of node kernel-accounted CPU to
softirq time in both pipeline-1/128-connection intervals. This reinforces the
exchange-cost hypothesis, but does not assign that percentage to Vex or name
a TCP/IRQ function. Softirq invocation counts are not vector-specific CPU time.

## Decision and next bounded experiment

No production source patch is justified by this capture. The supported
conclusion is that per-exchange system work dominates this workload, while
specific kernel functions remain unattributed. The small userspace costs and
existing ring batching do not support another speculative lock or generic
submission-batching change.

The next handoff compares the literal Service ClusterIP with the current
server Pod IP at pipeline 1 and 128 connections. Verify that Service DNS
resolves to the ClusterIP and that exactly one ready Service endpoint matches
the unchanged server UID and Pod IP. Both paths keep TCP, io_uring, CNI,
hardware, binary, process, preload and workload unchanged.

Use ABBA order, 10-second warmups and 60-second samples. Require both matched
comparisons to favor the same path, at least 5% median throughput difference,
consistent CPU/op movement, and no p99 regression over 10%; invalidate
interpretation if endpoints change, errors/misses appear, or significant
throttling/client constraints occur. Stop after four samples. A positive
result establishes Service-path sensitivity, not an engine speedup or a named
kernel root cause. This next experiment is planned, not executed.

Portable measurements: [saturation-2026-09-22.json](../bench/loadtest/results/saturation-2026-09-22.json).
Raw captures remain in the ignored `bench/loadtest/runs/2026-09-22-saturation/`
directory, including the recovered perf data and transport-failure logs.

Cleanup verified: the saturation namespace and NodePool were deleted, both temporary EC2 instances terminated, and no associated nodes or NodeClaims remain.
