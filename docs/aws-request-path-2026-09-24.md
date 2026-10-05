# Vex AWS baseline and CPU profiles — 2026-09-24

The AWS investigation is complete. Vex measured **724k ops/s with six workers**
and **332k ops/s with two workers**, using the same server binary and machine.
Both endpoints passed the predeclared 5% throughput repeatability check.
These are baseline measurements; **no engine speedup or new default is claimed**.

## Workload and results

Each cell used a fresh Vex process and preload: 1M **16-byte keys**, **256-byte
values**, 80:20 GET:SET, pipeline 1, 128 connections (32 client threads × 4),
10-second warmup and 60-second measured load. The client connected directly to
the server Pod IP from a separate dedicated node in the same availability zone.

| Workers / CPU quota | Fresh run | Throughput (ops/s) | p99 (ms) | Peak Vex RSS (MiB) |
| --- | --- | ---: | ---: | ---: |
| 6 / 6 | 1 | 734,143.63 | 0.255 | 460.30 |
| 6 / 6 | 2 | 713,396.27 | 0.431 | 460.07 |
| 2 / 2 | 1 | 326,762.52 | 0.495 | 458.48 |
| 2 / 2 | 2 | 336,804.43 | 0.487 | 458.74 |

Median throughput: **723,769.95** and **331,783.48 ops/s**, respectively.
Range/median: **2.87%** and **3.03%**. Six-worker p99 varied substantially;
throughput repeatability does not establish stable tail latency.

All four cells passed workload/configuration, exact database count and sampled
value lengths, strict repaired-client clock metadata, latency bounds, zero
errors/misses, process/binary identity, CPU throttling, client headroom,
memory events and swap checks. Six-worker controls used about 5.8 Vex CPU
cores, with roughly 60% client CPU headroom. RSS and server CPU counters use
server-local launch/drain brackets; preload and warmup are excluded. Brackets
are slightly wider than the client's measured interval.

## What scaling does—and does not—show

Six workers delivered **2.18×** the throughput of two. The nominal worker ratio
`Q6 / (3 × Q2)` is **72.72%**, below the selected 80% goal. Cross-combinations
of the observed controls range from **70.60% to 74.89%**; this is not a
statistical confidence interval.

This is **not physical-core efficiency or proof of Vex lock contention**.
Both endpoints ran on the same eight-vCPU server node. Network IRQ/softirq
work can run outside the Vex process and CPU quota, including on other cores.
Two workers therefore do not represent an isolated two-core machine.

| Median accounted CPU cost (µs/op) | 2 workers | 6 workers |
| --- | ---: | ---: |
| Vex cgroup total | 5.949 | 7.985 |
| Vex cgroup user | 0.899 | 0.940 |
| Vex cgroup system | 5.050 | 7.044 |

The Vex cgroup's increase is almost entirely system CPU. However, whole-node
**system plus softirq cost stayed near 7.9 µs/op at both endpoints**. Whole-node
busy CPU was approximately 11.4 µs/op at two workers and 10.0 at six, including
system/DaemonSet activity and nice CPU time. These counters span wider brackets
and cannot be attributed exclusively to Vex. They strongly limit any claim
that increased Vex-accounted CPU means increased total networking work:
where softirq work is charged can change. `SC_CLK_TCK=100` was verified on the
actual server before converting the counters.

## CPU attribution and decision

Two separate, instrumented 20-second profiles were excluded from baseline
throughput. Both used CPU-clock sampling at 99 Hz, frame-pointer callchains,
verified recorder enable/disable acknowledgements, an explicit monotonic
clock, the same-boot kernel symbols, and disabled Vex timing probes.

| Profile | Samples | Kernel mode | User mode | Worker coverage | Recorded loss |
| --- | ---: | ---: | ---: | --- | ---: |
| 6 workers | 11,325 | 88.17% | 11.83% | All 6 | 0 |
| 2 workers | 3,954 | 85.63% | 14.37% | Both | 0 |

Networking dominates sampled worker CPU. At six workers, `io_send` accounts
for 60.25% inclusive and `net_rx_action` for 58.86% inclusive; their stacks
overlap and **must not be summed or called removable io_uring overhead**.
The largest user leaf, KV hashmap lookup, accounts for 5.97% of all samples.
Its HNSW-looking symbol is an identical-code-folding alias, not vector work.

The matched profiles show many small networking leaf differences, with no
single demonstrated removable engine cost sufficient for the requested
repeatable 10% gain. Profile comparisons use each instrumented workload's own
operation count, and remain subject to the host-accounting caveat above.

**Decision:** retain the baseline and attribution; do not ship another hashmap,
allocator, lock-removal or I/O rewrite from this evidence. The next useful
investigation must isolate a specific network/request-path cost and account
for CPU outside Vex before choosing a bounded engine change. See
[the detailed strategy handoff](aws-optimization-handoff-2026-09-24.md).

Frame-pointer chains may omit optimized user frames. Kernel leaf names remained
unknown for 85 six-worker samples (0.75%) and 24 two-worker samples (0.61%);
those uncertainties are preserved. Raw sample identities, modes, addresses
and timestamps were checked against official perf output. Context markers
exclude user addresses from kernel-chain mapping. There were no unresolved
kernel callchain addresses after that correction.

## Environment, reproducibility and cleanup

- Region/AZ: ap-south-1 / ap-south-1a, scrum EKS cluster.
- Server: dedicated on-demand c7g.2xlarge, 8 vCPU, Vex memory limit 4 GiB.
- Client: dedicated on-demand c7g.8xlarge, 32 vCPU, quota 24, memory limit 4 GiB.
- Actual worker affinity: CPUs 0–5 or 0–1. Server effective cpuset: 0–7.
  Quotas did not reserve exclusive cores. Normal cluster DaemonSets remained.
- Native ARM64 Vex SHA256:
  `4f3df6e8916d9c127a6a9a7940701e1faa3e11182c5809310f7bd2c42fc047d6`.
- Repaired memtier SHA256:
  `f894894dcfd4cf3f38636b7ec679762d2c6ac5ea0363f98e68703844cf9a3207`.
- All io_uring backends active; SQ-head-cache off; DEBUG PROBES verified off.
- Machine-readable results: `bench/loadtest/results/aws-request-path-2026-09-24.json`.
- Raw manifests, exact node/pod identities, workload output, counters, profiles
  and analysis: `bench/loadtest/runs/2026-09-24-aws-request-path/` (ignored artifacts).
- Runner: `bench/loadtest/scripts/profile_aws_request_path.py`. It requires
  the separately saved infrastructure/manifests and refuses existing results.

The initial 12-CPU client warmup failed the 20% headroom requirement; no
60-second sample ran. Its output is retained separately. Client capacity was
increased before the accepted controls. DWARF sampling was rejected before a
profile workload started; supported frame-pointer sampling and perf's
NUL-terminated acknowledgements were verified first. Rendering fixes reused
the same capture. Setup failures were not counted as performance trials.

**Cleanup verified:** task namespace, NodePools and NodeClaims are absent.
All three task-created EC2 instances—including the superseded client—are
terminated: `i-0529a42f975a46a05`, `i-048228d06bfe4c8fe`,
`i-052576e4b8ee5a093`. The shared EC2NodeClass was unchanged. Two private
benchmark ECR image tags are retained for reproducibility; no compute remains.

These results support no Redis/Dragonfly comparison, no local-Docker or x86
comparison, and no general performance headline beyond the measured workload.
