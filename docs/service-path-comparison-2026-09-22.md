# Service path comparison — 2026-09-22

Completed four valid samples. Direct Pod access showed a **2.4% throughput
difference**, below the predeclared 5% gate. This supports a small observed
path difference in this run, not a substantial routing gain or an engine
speedup. No production source change was made.

| Destination | Median ops/s | Median run p99 | Server CPU/op | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Service ClusterIP | 329,446 | 0.703 ms | 17.90 µs | 459.94 MiB |
| Direct Pod IP | 337,409 | 0.643 ms | 17.51 µs | 459.93 MiB |

The ABBA throughput sequence was 326,923 / 329,906 / 344,912 / 331,970 ops/s.
The matched direct-Pod gains were 0.91% and 3.90%, with lower CPU/op and p99
in both pairs. Median paired gain is 2.406%; the ratio of path medians is
2.417%. Both remain below the gate. Two samples per path cannot establish a
small effect precisely or rule one out. Median run p99 is not a percentile
computed over combined requests.

All raw durations were 60.000–60.001 seconds, with zero checked errors or
misses, one million keys before and after, unchanged process/pod/endpoint
identity, and no significant throttling or client headroom constraint. Maximum
average client-thread utilization was 44.9%. Both paths recorded approximately
one received and one transmitted packet per operation in pod `eth0` counters;
these are not physical NIC packet counts. CPU/op includes small control-call
overhead, and network snapshots cover a slightly wider interval than timing.
RSS reflects a shared process and allocator history, not independent memory
experiments.

Portable results: [`service-path-2026-09-22.json`](../bench/loadtest/results/service-path-2026-09-22.json).
Raw JSON, counter snapshots, definitions, runner copies and identities are in
`bench/loadtest/runs/2026-09-22-service-path`.

This is a bounded four-cell ABBA measurement of the same compact Vex server
through two Kubernetes destinations:

| Path | Destination |
| --- | --- |
| A | Service `benchmark-server` ClusterIP |
| B | The fresh server Pod IP |

The order is A1, B1, B2, A2. Every cell uses pipeline 1, 32 threads, four
clients, 128 connections, one million fixed 16-byte keys, 256-byte values,
an 80/20 GET/SET workload, 10 seconds of warmup, and 60 seconds of timing.
The server has a six-vCPU quota and 10 GiB on a dedicated `c6a.2xlarge`; the
load generator has a 24-vCPU quota on a separate `c6a.8xlarge`. Pods are
Burstable, so the quota does not establish six exclusive physical cores. The
accepted server binary SHA256 is
`200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e`.

The runner starts one fresh server and preloads once. Before timing it records
the Service and EndpointSlice definitions, resolves Service DNS to the
ClusterIP, and requires exactly one ready and serving endpoint whose address
and target Pod UID match the server. It checks the same Pod UID, node, live
process PID/start tick/executable, key count, 32 sampled value lengths, and client Pod
identity around every cell. Raw memtier JSON must report the intended server
and port for that cell. CPU, RSS, cgroup throttling, kernel counters, binary
identity, and client headroom are saved with the raw result.

The comparison produces a valid measurement when errors, misses, endpoint or
process changes, key-count changes, client constraints, and significant
throttling are absent. A path signal requires both paired comparisons to agree,
at least a 5% median throughput gap, lower server CPU per operation for the
winner in both pairs, and no p99 regression above 10%. A smaller or mixed
result remains valid evidence with no signal. This measurement does not claim
an engine improvement or justify changing the default path; it only tests
whether this deployment path is sensitive at the selected workload.

Execution used namespace `vex-service-path-20260922`. The profiler sidecar
remained idle except for process identity checks; no sampled profiling ran
during these measurements. Cleanup verified: namespace, NodePool, nodes and NodeClaims are gone; both temporary EC2 instances are terminated.
