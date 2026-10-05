# AWS optimization decision — 2026-09-24

The matched **two-worker/two-CPU endpoint is complete**. The nominal worker
scaling ratio is 72.72%, but this is **not physical-core efficiency or proof
of a Vex coordination bottleneck**. Both endpoints used the same eight-core
host; networking can run outside the Vex cgroup. Whole-node system and softirq
CPU per operation are similar at both endpoints, despite higher system CPU
charged to Vex at six workers. The completed matched profiles show distributed
network-path work rather than one demonstrated removable engine cost. **Stop
this investigation without an engine patch or another benchmark sweep**; the
evidence does not justify a repeatable 10% pipeline-1 gain from a specific
change. Preserve the results and clean up the task's AWS resources.

## Host-accounting limit

`bench/loadtest/runs/2026-09-24-aws-request-path/host-cpu-context.json` reports
whole-node `/proc/stat` counter deltas divided by completed operations:

| Host counter (µs/op) | Two-worker runs | Six-worker runs |
| --- | ---: | ---: |
| Busy | 11.454 / 11.348 | 9.906 / 9.996 |
| System | 2.200 / 2.271 | 2.242 / 2.272 |
| Softirq | 5.717 / 5.547 | 5.708 / 5.770 |
| Nice | 2.563 / 2.516 | 0.993 / 0.971 |

The coordinator verified `USER_HZ=100` on the runtime. The wider orchestration
brackets include DaemonSets and
OS work, so none of these rows is an exact allocation of Vex request cost.
The nice-CPU difference is consistent with roughly fixed background work
amortized over fewer operations at two workers; its owner has not been
attributed. It must not be represented as extra per-request engine work.

The two-worker cgroup can benefit from networking performed on the other six
host cores. Moving from two to six workers therefore does not triple the
total physical CPU available to the request path. The cgroup system-CPU
increase may substantially reflect where IRQ/softirq work executes or is
charged. Similar whole-node system/softirq cost is evidence against treating
the entire cgroup increase as newly added network work. Neither accounting
source alone establishes a causal bottleneck or a hardware efficiency result.

## Evidence that determines the choice

The accepted six-worker controls measured 734,143.63 and 713,396.27 ops/s:
median 723,769.95, range/median 2.87%. Client CPU headroom was about 60%, and
Vex used about 5.8 CPU cores. The p99 values, 0.255 and 0.431 ms, varied much
more than throughput; the throughput stability gate does not certify stable
tail latency. Raw evidence is in
`bench/loadtest/runs/2026-09-24-aws-request-path/controls-summary.json`.

The separate CPU-clock capture covers all six worker TIDs, 13–18, with
11,325 samples and zero recorded loss. Kernel leaves account for 88.17% and
user leaves 11.83%. The 5.97% hash-map leaf is KV lookup: the HNSW name is an
identical-code-folding alias, and the official callchains lead through
`ConcurrentKV` GET/SET. It is not evidence of vector work in this workload.

As a rough fixed-work worker-CPU budget, a 10% throughput increase requires removing
9.09% of total cost. That would require eliminating about 77% of all sampled
user work if kernel work were unchanged. Completely eliminating the sampled
5.97% lookup leaf would provide only about 6.35% under those assumptions.
These are conditional prioritization estimates, not physical upper bounds or
predictions: profile time shares are not exact service-demand measurements,
and interrupt/softirq cost can move into or out of worker accounting.

The 60.25% `io_send` and 58.86% `net_rx_action` inclusive shares overlap.
Official stacks show TCP, device/virtual-interface processing and softirqs
nested underneath submission. They do not assign 60% to removable io_uring
bookkeeping. The networking leaves include `nft_do_chain` 3.35%,
`fib_table_lookup` 2.85%, and `__nf_conntrack_find_get` 2.73%; these establish
real network-path cost, not permission to bypass cluster networking or proof
that one topology change removes their sum.

Use `profile/perf-self.txt`, `profile/perf-inclusive.txt`, and
`profile/perf-header.txt` for official attribution. The coordinator corrected
context markers in `profile/analysis-verified.json`: zero unresolved kernel
callchain addresses remain, and user addresses are excluded from kernel
mapping. The old unresolved inclusive row is invalid. The 85 kernel leaf
samples reported as unknown in the overlay module (0.75%) remain uncertain;
nearest-symbol disagreements are preserved. Frame-pointer chains can omit
optimized user frames. Process-scoped sampling also misses host work executed
outside Vex; node CPU/softirq brackets remain context.

## Request path checked against the code

- `src/server/event_loop.zig`: `addFdRecvMode` already suppresses duplicate
  readiness polling. `submitRecv`/`submitSend` queue SQEs; the event loop uses
  the existing combined submit/wait path.
- `src/server/worker.zig`: `processOneCommand` uses the fast RESP parser;
  `executeHotFast` performs GET lookup under the stripe read lock and copies
  the stable value into the response. SET calls `ConcurrentKV.setInternal`.
  `submitUringWriteFromScratch` and `rearmRecv` already avoid eager submits.
  Scratch ownership protects in-flight sends and partial-write ordering;
  whole responses of at least 4 KiB already transfer buffer ownership.
- `src/engine/kv/concurrent_kv.zig`: SET takes the stripe write lock and
  reuses value storage through `replaceValue`. The GET lock protects map
  rehash and value lifetime. Removing it requires a correctness design, and
  this capture does not establish lock contention as the limiting cost.
- `src/server/tcp.zig`: accepted TCP sockets already enable TCP_NODELAY.

Do not propose these existing behaviors as new optimizations. Do not revive
the rejected pointer-entry or recv-poll-first candidates, enable SQ-head-cache
from its old-clock pilot, copy another server's architecture, introduce
worker-owned KV shards, or rewrite sends/multishot receive from this evidence.

## Completed scaling discriminator

**Original hypothesis:** throughput at six workers is materially below
proportional scaling from two workers on the same remote TCP path. The
endpoint answers that nominal worker-count question, while the host counters
show why it cannot establish proportional physical-resource scaling or assign
the ratio to storage ownership, networking contention, or I/O submission.

The coordinator ran two fresh two-worker/two-CPU controls on the same server
machine class and same-AZ separate client, retaining the 24-CPU client and
existing binary/image digests. Both retained the 1M 16-byte keys, 256-byte values,
80:20 GET:SET, pipeline 1, 128 connections, 10-second warmup and 60-second
sample, with experimental switches and DEBUG PROBES off. Both used fresh
server processes and preloads and passed measurement validation. Normal
DaemonSets remained unchanged. This was a two-endpoint comparison.

`bench/loadtest/runs/2026-09-24-aws-request-path/scaling-summary.json` contains
the validated values:

| Measure | Two workers / two CPUs | Six workers / six CPUs |
| --- | ---: | ---: |
| Control 1 throughput (ops/s) | 326,762.52 | 734,143.63 |
| Control 2 throughput (ops/s) | 336,804.43 | 713,396.27 |
| Median throughput (ops/s) | 331,783.475 | 723,769.95 |
| Range / median | 3.03% | 2.87% |
| Server total CPU (µs/op) | 5.949 | 7.985 |
| Server user CPU (µs/op) | 0.899 | 0.940 |
| Server system CPU (µs/op) | 5.050 | 7.044 |

The median throughput multiple is 2.18145, and the nominal worker-count ratio
`E = Q6 / (3 * Q2) = 72.7151%`. Cross-combinations of the observed controls
range from 70.60% to 74.89%; all are below 80%. This observed range is not a
statistical confidence interval. The CPU figures are medians of per-run
counter deltas divided by completed operations; server-local CPU brackets
include a small launch/drain margin, as in the six-worker controls.

Server CPU grows by 2.035 µs/op: 1.994 µs/op system and 0.041 µs/op user.
System CPU therefore accounts for about 98% of the **Vex-cgroup** increase.
This arithmetic is correct but does not establish growth in total system work;
the host counters above materially change its interpretation. The endpoints
also differ in achieved packet rate and connections per worker. Do not label
the 2.035 µs/op cgroup difference as removable overhead or attribute it to
stripe locks, shared kernel contention, or any specific engine path.

## Completed matched profile and final decision

The additional profile asked which kernel functions account for the different
amount of work captured inside Vex at two and six workers. A larger
process-scoped leaf cost cannot by itself prove more total host work;
host-counter context must accompany the comparison. Cost shifting remains a
live explanation.

The coordinator collected one separate 20-second workload at the validated
two-worker/two-CPU endpoint with the unchanged 24-CPU client, dataset, command
mix, pipeline 1, and 128 connections, matching the six-worker capture's verified
CPU-clock event, 99 Hz, frame-pointer callchains, MONOTONIC clock and explicit
enable/disable acknowledgements. The two-worker capture has 3,954 samples,
zero recorded loss, and exactly 1,977 samples on each worker TID (13 and 14).
Kernel/user shares are 85.63%/14.37%. All official leaf identities, modes,
addresses and timestamps were checked against the raw capture; there are zero
unresolved kernel callchain addresses. The 24 unknown kernel leaf samples in
the overlay module retain their attribution uncertainty. Profile throughput
remains outside the uninstrumented baseline.

`profile-two-worker/analysis-verified.json` and
`profile-leaf-comparison.json` contain the evidence. The comparison uses
disjoint kernel leaves normalized by each instrumented workload's own
completed operations. Summed sampled CPU is approximately 6.116 µs/op at two
workers and 7.885 µs/op at six. The largest positive leaf differences are:

| Kernel leaf | Six minus two sampled CPU (µs/op) |
| --- | ---: |
| `__wake_up_sync_key` | 0.139 |
| `skb_release_data` | 0.111 |
| `nft_do_chain` | 0.086 |
| `fib_table_lookup` | 0.084 |
| `__slab_free` | 0.083 |

The growth in process-scoped samples is distributed across network work.
`__wake_up_sync_key` alone does not establish Vex lock contention. These
differences are neither removable-cost estimates nor evidence that total
network CPU rose: whole-node system plus softirq remains about 7.9 µs/op at
both endpoints. Sampling, wider counter brackets, background work and
off-process execution limit precision and causality.

**Decision:** no concrete engine change currently has cost evidence strong
enough for the requested material improvement. Do not dispatch a smaller model
to manufacture a patch from these profiles. Do not change worker ownership,
map layout, io_uring strategy, or cluster networking on this evidence. A future
topology experiment would need one specific removable path, a bounded task
topology change and an explicit falsifiable prediction; this result does not
select that experiment. No additional sweep is part of this handoff.

The remaining smaller-model scope is artifact validation/reporting only if
needed. Reuse existing summaries and preserve raw counters; no `src/` changes,
dependencies, builds or engine tests. The coordinator retains cloud cleanup
ownership. The nominal scaling result is neither an optimization win nor a
causal engine deficit.

Any future engine candidate still needs matched paired controls with at least
10% repeatable P1 throughput improvement, no more than 5% p99 regression,
no more than 2% RSS increase, and lower CPU/op. Moving work outside the server
cgroup is not sufficient; include separate and summed task CPU/op and host
counter context. Keep the acceptance gate unchanged.
