# Adaptive Vex vs Redis vs Dragonfly — completed AWS comparison

All 36 measured trials passed data, client CPU and network checks. Vex leads
mixed hot/cold and both uniform workloads at these fixed settings. Redis leads
the single-hot-key workload. This compares engine configurations, not the
isolated causal effect of adaptive ownership.

The first three workloads ran on September 28. The original hosts expired after
a credential interruption; the 1,024-set workload was rerun in full for all
three engines on a fresh matched pair of the same instance types on September
29. Binaries and load settings are identical. Comparisons within each row are
matched; cross-workload comparisons also involve different data distributions
and, for 1,024 sets, a different host pair.

## Throughput

Medians of three 60-second trials, in millions of operations per second.

| Workload | Redis | Vex adaptive | Dragonfly |
|---|---:|---:|---:|
| hot | 0.882 | 0.756 | 0.570 |
| mixed | 0.718 | 1.569 | 0.689 |
| uniform64 | 0.667 | 2.919 | 1.769 |
| uniform1024 | 0.787 | 3.660 | 1.912 |

## CPU cost

Server CPU microseconds per operation; lower is better.

| Workload | Redis | Vex adaptive | Dragonfly |
|---|---:|---:|---:|
| hot | 2.270 | 2.373 | 3.576 |
| mixed | 2.558 | 2.647 | 4.302 |
| uniform64 | 2.676 | 2.445 | 4.271 |
| uniform1024 | 2.418 | 1.976 | 4.124 |

## Latency and variation

Pipeline round-trip p99 in milliseconds. For mixed traffic, this is the hot
connection group. Ranges are throughput min–max across the three repetitions,
not confidence intervals.

| Workload | Engine | Pipeline p99 ms | Throughput range, M ops/s |
|---|---|---:|---:|
| hot | redis | 0.69 | 0.863–0.918 |
| hot | Vex adaptive | 1.23 | 0.751–0.766 |
| hot | dragonfly | 2.05 | 0.567–0.572 |
| mixed | redis | 0.93 | 0.716–0.720 |
| mixed | Vex adaptive | 0.93 | 1.567–1.573 |
| mixed | dragonfly | 1.97 | 0.684–0.693 |
| uniform64 | redis | 0.99 | 0.661–0.673 |
| uniform64 | Vex adaptive | 0.29 | 2.854–2.927 |
| uniform64 | dragonfly | 0.64 | 1.745–1.811 |
| uniform1024 | redis | 0.81 | 0.783–0.914 |
| uniform1024 | Vex adaptive | 0.26 | 3.610–3.678 |
| uniform1024 | dragonfly | 0.41 | 1.887–1.947 |

Redis's 1,024-set trial range is 0.783–0.914M ops/s; the median is 0.787M.
Three consecutive repetitions on one host pair per workload do not establish
host-to-host or long-duration stability.

## Mixed traffic and ownership behavior

| Engine | Hot M ops/s | Cold M ops/s | Cold pipeline p99 ms |
|---|---:|---:|---:|
| redis | 0.539 | 0.180 | 0.92 |
| Vex adaptive | 0.712 | 0.860 | 0.87 |
| dragonfly | 0.529 | 0.160 | 2.19 |

Mixed connection shares do not imply fixed operation shares: faster cold
connections complete more work. Independently computed medians of hot and cold
rates need not sum exactly to the median total rate.

Vex activated ownership once in each hot and mixed cell, without release during
those cells. Both uniform cells recorded zero activations. Uniform throughput
therefore describes Vex with adaptive monitoring enabled; it is not a gain
caused by ownership routing. Ownership remains disabled by default.

## Protocol

Within each workload, all engines run sequentially on the same isolated c6gn.8xlarge server in
ap-south-1b, with an 8-CPU cgroup quota and 8 GiB container limit. Vex uses eight
unpinned workers, 256 sorted-set partitions and adaptive ownership enabled.
Redis uses eight I/O threads; Dragonfly uses eight proactor threads. Persistence
is disabled. The server host has 32 vCPUs; the engine allocation is eight CPUs,
not an exclusive eight-core cpuset. A separate c7g.16xlarge host runs the client
with a 48-CPU container quota and GOMAXPROCS=16.

Redis and Dragonfly use the same immutable image/binary versions as the earlier
comparison: Redis 8.10.1 and Dragonfly 2.0.0-40553fd842c2850b65787ed4da908d95aeab8834.
Their running binary hashes are checked. Vex's frozen Linux binary SHA-256 is
`a0d74431a5a761903864ff276f698287d78afe54b6b1ca2cc82ed4bfc733472c`.

The driver uses 32 connections, pipeline 16, approximately 80% ZINCRBY and 20%
ZRANK. Hot-only traffic targets one key. Mixed traffic uses 24 hot connections
and eight cold connections spread over the other 63 keys. Uniform workloads
use 64 sets × 4,096 members or 1,024 sets × 256 members. Every configuration
therefore preloads 262,144 members. Key/member selection follows the same
deterministic modular sequences as the local ownership test.

Each engine/workload cell starts a fresh server and dataset, runs a ten-second
warmup and three consecutive 60-second measurements. Engine order rotates
between workloads. These are matched fixed-concurrency results, not a saturation
sweep or independently optimized peak capacity for each engine.

Reported latency is the **full 16-command pipeline round-trip p99**. It is not
per-command service p99 and must not be combined with the older memtier charts.
CPU cost is the server cgroup CPU-time delta around each measurement divided by
completed operations; snapshot windows include small orchestration overhead.

Client OS-thread CPU usage is sampled every 200 ms. Valid trials require each
sampled thread's trial-average use below 0.9 cores, aggregate use below 12.8
cores, and container throttling below 1% of measured duration. These diagnostics
do not rule out every possible load-generator bottleneck. Server/client ENA
allowance-exceeded counters must not increase; pod, process and node identities
must remain stable. No swaps, OOM events or restarts are accepted.

Verification checks every set's cardinality, complete member list, sorted order,
sampled ranks and aggregate score conservation across warmup plus measurements.
Every driver response is checked for expected type and valid rank/score bounds.

## Interruption, completion and cleanup

The interrupted 1,024-set cell from September 28 is excluded. Its raw directory
is retained for audit. The original EC2 instance IDs were no longer present on
September 29, and leftover Kubernetes resources were deleted. All three
1,024-set configurations were rerun from fresh datasets on the new host pair.

The final runner's cleanup request succeeded. Both completion-run EC2 instances
(`i-0b6de8a95ede34bbc`, `i-0fdf8e9d5048c0f56`) are verified terminated. Both run
labels have zero remaining namespaces, nodes, nodepools or nodeclaims.
[EC2 termination evidence](../bench/loadtest/runs/2026-09-29-adaptive-three-aws-final/cleanup-ec2-final.json)
is retained with both Kubernetes cleanup snapshots.

## Evidence

[Combined JSON](../bench/loadtest/results/adaptive-three-engine-aws-2026-09-29.json) ·
[Combined CSV](../bench/loadtest/results/adaptive-three-engine-aws-2026-09-29.csv) ·
[Original protocol](../bench/loadtest/runs/2026-09-28-adaptive-three-aws/protocol.json) ·
[1,024-set completion protocol](../bench/loadtest/runs/2026-09-29-adaptive-three-aws-final/protocol.json)

Each run directory retains client histograms, CPU snapshots, manifests, binary
identities, network counters, logs and verification records. The same frozen
Vex and client executables were used in both runs. The configuration-comparison
harness accounting/error tests and `git diff --check` pass.
