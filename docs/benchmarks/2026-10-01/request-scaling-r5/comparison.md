# Vex request scaling — 1 October 2026

5/15 workload/CPU cells complete. Results include only validated three-run confirmations.

| CPUs | Workload | Confirmed ops/s | Connections | Median p99 ms | CPU µs/op | CPU cores used | Scaling vs 1 CPU |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | get | 136,023 | 32 | 0.295 | 7.35 | 1.00 | 1.00× |
| 1 | hash | 122,305 | 64 | 0.623 | 8.17 | 1.00 | 1.00× |
| 1 | zset | 115,649 | 32 | 0.359 | 8.64 | 1.00 | 1.00× |
| 2 | get | 262,874 | 64 | 0.303 | 7.60 | 2.00 | 1.93× |
| 2 | hash | 230,466 | 32 | 0.199 | 8.71 | 2.00 | 1.88× |

Same dedicated c6gn.8xlarge server host, CPU quotas and worker counts of 1/2/4/8/16; unpinned. Separate c7g.16xlarge load generator with 48-CPU quota. Same availability zone, 8 GiB server memory, 256 partitions, adaptive ownership disabled.

Each cell sweeps 32/64/128/256/512/1024 connections for 15 seconds each, then confirms the fastest passing setting with three 60-second runs (falling back if necessary). Pipeline 1, uncapped closed-loop load; p99 is service latency. Every confirmation must pass the 5 ms budget and client-headroom gates. Reported rates are confirmed lower bounds, not proven maxima.

GET: one million 256-byte values, uniform reads. Hash: 4,096 hashes × 16 fields × 256 bytes, 80% HSET / 20% HGET. Sorted set: 64 sets × 4,096 members, 80% ZINCRBY / 20% ZRANK. No TTLs or persistence. These workload differences prevent interpreting cross-workload rates as equivalent work.

Every cell verifies process/host continuity, memory health and ENA allowance counters. Hash/sorted-set data are checked exactly; GET uses the existing sampled-value validation. Client limits: each thread below 0.9 CPU, aggregate below 38.4 CPUs, throttling below 1% of trial duration.

CPU figures cover engine cgroup CPU time; host networking CPU is not fully attributed to that cgroup. CPU quotas are not dedicated physical-core pinning. This run varies connections and CPU allocation, not key cardinality; it cannot independently identify hot-key or internal lock contention.

Binary SHA-256: `943ae2d799b346b5ee8a9d5832b1e8c238aac9c389b15da2b196ec35cc6b9124`.

Raw evidence: `/Users/pratyushsingh/vex-co/vex-performance/bench/loadtest/runs/2026-10-01-request-scaling-aws-r5`. Cleanup state is recorded in the run directory; a deletion request alone does not prove EC2 termination.
