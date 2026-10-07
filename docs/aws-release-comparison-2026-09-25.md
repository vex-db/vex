# AWS release comparison — 25 September 2026

Completed 24 validated measurements comparing Vex 0.8.1-rc.2 (control), Vex 0.8.1-rc.3 (candidate), Redis 8.10.1, and Dragonfly 2.0.0. **Overall result: measurement inconclusive** because three groups exceeded the predeclared 5% throughput spread limit.

| Value | Engine | Median ops/s | Median run p99 (ms) | Median sampled peak RSS (MiB) | Throughput spread |
|---|---|---:|---:|---:|---:|
| 256 B | Vex rc.2 | 753,766 | 0.255 | 460.1 | 8.23% |
| 256 B | Vex rc.3 | 719,702 | 0.263 | 356.3 | 7.64% |
| 256 B | Redis | 541,818 | 0.471 | 374.4 | 0.17% |
| 256 B | Dragonfly | 529,954 | 0.439 | 295.1 | 4.01% |
| 32 B | Vex rc.2 | 761,802 | 0.247 | 200.2 | 2.23% |
| 32 B | Vex rc.3 | 742,677 | 0.319 | 142.1 | 3.23% |
| 32 B | Redis | 554,723 | 0.439 | 105.9 | 1.32% |
| 32 B | Dragonfly | 553,156 | 0.383 | 110.3 | 7.84% |

## Interpretation

- 256 B: rc.3 median throughput was 4.52% lower than rc.2; median sampled peak RSS was 22.56% lower. Median p99 changed by +3.14%.
- 32 B: rc.3 median throughput was 2.51% lower than rc.2; median sampled peak RSS was 29.03% lower. Median p99 changed by +29.15%.

The 256-byte control/candidate and 32-byte Dragonfly groups exceeded 5% spread. Medians are descriptive; this run does not establish a repeatable overall performance ranking. Both candidate/control median throughput ratios fall below the predefined 98% acceptance threshold, and the 32-byte candidate p99 exceeds the 110% latency threshold; the suite classifies variability first, so the result remains inconclusive rather than accepted.

## Method and interruption

One million keys; 80% GET / 20% SET; pipeline 1; 128 connections; 32 client threads; 10-second warmup and 60-second measurement; three rounds per engine and value size. Dedicated ARM64 c7g.2xlarge server and c7g.8xlarge client in ap-south-1a; engine budget 6 CPU / 4 GiB, client budget 24 CPU / 4 GiB. Images and executable hashes were pinned and validated. Each completed cell passed the existing data, clock, identity, resource, and client-headroom checks. RSS is sampled process memory, not disk usage or a lifetime high-water mark.

The original runner stopped after 21 completed cells. Those results were reused unchanged. The interrupted third-round 32-byte Redis cell had no saved validated measurement; its partial files remain intact, and its replacement is explicitly named `value32-round3-redis-resumed`. The final Dragonfly and control cells were then completed. This interruption and replacement limit strict uninterrupted-run comparability. No completed result was discarded or rerun.

## Evidence

- [Full results and validation records](../bench/loadtest/runs/2026-09-25-packed-growth-aws/comparison.json)
- [Resumption record](../bench/loadtest/runs/2026-09-25-packed-growth-aws/resume-record.json)
- [Raw artifacts](../bench/loadtest/runs/2026-09-25-packed-growth-aws/)

## Cleanup

Both dedicated EC2 instances are verified terminated; [termination evidence](../bench/loadtest/runs/2026-09-25-packed-growth-aws/cleanup-ec2.json). The temporary namespace and two NodePools were deleted.
