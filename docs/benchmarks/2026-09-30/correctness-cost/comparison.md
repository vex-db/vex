# Cost impact of the key-lifecycle correctness fix

Completed 28/28 cells as of 2026-09-30T12:14:40.166198+00:00. Only completed before/after pairs appear below.

Baseline: integer fixes already applied. Candidate: key-type, lifecycle and locking fixes. Both are Linux arm64 ReleaseFast binaries, unpinned, with adaptive ownership disabled. CPU allocations are quotas on the same c6gn.8xlarge host, with a separate c7g.16xlarge client.

## Confirmed throughput below 5 ms p99

Medians of three 60-second repetitions; every repetition must pass the latency and client gates. These are confirmed lower bounds from a connection sweep, not proven global maxima.

| CPUs | Workload | Before ops/s | After ops/s | Change | Before / after p99 ms | Relative cost/op |
|---:|---|---:|---:|---:|---:|---:|
| 4 | get | 504,150 | 489,417 | -2.9% | 0.35 / 0.20 | 1.03× |
| 4 | set | 495,096 | 459,756 | -7.1% | 0.19 / 0.38 | 1.08× |
| 4 | incr | 543,283 | 515,256 | -5.2% | 0.31 / 0.17 | 1.05× |
| 4 | hash | 507,849 | 60,816 | -88.0% | 0.20 / 4.89 | 8.35× |
| 4 | list | 303,078 | 44,489 | -85.3% | 2.69 / 3.79 | 6.81× |
| 4 | set-member | 513,141 | 62,997 | -87.7% | 0.36 / 4.38 | 8.15× |
| 4 | zset | 447,226 | 400,089 | -10.5% | 0.21 / 0.24 | 1.12× |
| 16 | get | 1,555,156 | 1,503,727 | -3.3% | 0.81 / 1.14 | 1.03× |
| 16 | set | 1,519,369 | 1,428,193 | -6.0% | 1.12 / 1.12 | 1.06× |
| 16 | incr | 1,676,670 | 1,597,045 | -4.7% | 0.43 / 0.95 | 1.05× |
| 16 | hash | 1,491,541 | 29,173 | -98.0% | 1.23 / 0.49 | 51.13× |
| 16 | list | 256,282 | 28,490 | -88.9% | 3.10 / 0.42 | 9.00× |
| 16 | set-member | 1,005,413 | 28,192 | -97.2% | 4.05 / 0.61 | 35.66× |
| 16 | zset | 1,277,679 | 1,129,602 | -11.6% | 0.77 / 0.71 | 1.13× |

Relative cost/op assumes the same hourly server price and sustained operation at these confirmed rates. It excludes client cost and is not an actual invoice or a prediction of the number of instances needed.

## Fixed-concurrency control

Medians of three 30-second repetitions at 128 connections and pipeline 1. Both builds receive identical client settings; achieved request rates can differ. CPU includes total engine cgroup CPU, divided by completed operations. Loaded RSS is measured immediately after preload, before the connection sweep. Post-trial RSS is retained in JSON but can reflect different connection/allocator histories.

| CPUs | Workload | Before / after ops/s | Before / after CPU µs/op | Before / after loaded RSS MiB | Before / after p99 ms |
|---:|---|---:|---:|---:|---:|
| 4 | get | 509,114 / 482,458 | 7.84 / 8.25 | 727.0 / 726.9 | 0.34 / 0.35 |
| 4 | set | 482,004 / 459,023 | 8.25 / 8.65 | 727.0 / 726.9 | 0.35 / 0.37 |
| 4 | incr | 545,490 / 511,347 | 7.29 / 7.77 | 3.8 / 3.8 | 0.31 / 0.34 |
| 4 | hash | 498,262 / 97,083 | 7.97 / 16.65 | 53.4 / 51.9 | 0.35 / 14.02 |
| 4 | list | 304,828 / 84,547 | 11.18 / 18.49 | 88.1 / 85.9 | 0.97 / 16.77 |
| 4 | set-member | 518,653 / 101,686 | 7.60 / 15.86 | 14.0 / 13.1 | 0.35 / 14.14 |
| 4 | zset | 450,797 / 391,548 | 8.77 / 10.06 | 67.0 / 66.9 | 0.40 / 0.46 |
| 16 | get | 1,022,020 / 1,004,384 | 12.17 / 12.42 | 728.7 / 728.6 | 0.22 / 0.22 |
| 16 | set | 1,024,647 / 1,000,153 | 12.16 / 12.70 | 728.7 / 728.6 | 0.22 / 0.21 |
| 16 | incr | 1,036,552 / 1,027,183 | 11.48 / 11.83 | 5.5 / 5.5 | 0.22 / 0.21 |
| 16 | hash | 1,039,459 / 57,918 | 12.25 / 27.34 | 55.1 / 53.5 | 0.24 / 20.86 |
| 16 | list | 275,587 / 53,271 | 14.96 / 29.14 | 89.1 / 103.7 | 7.01 / 22.66 |
| 16 | set-member | 773,732 / 59,824 | 12.25 / 26.39 | 15.7 / 14.8 | 0.66 / 20.09 |
| 16 | zset | 948,336 / 888,475 | 13.53 / 14.92 | 70.4 / 70.8 | 0.24 / 0.24 |

Workloads: GET and SET each use one million 256-byte values; INCR uses 4,096 independent counters. Hashes and lists use 4,096 keys with 16 elements each and 80% updates / 20% reads. Set membership is read-only. Sorted sets use 64 sets × 4,096 members with 80% ZINCRBY / 20% ZRANK.

No TTLs or persistence are enabled; these measurements do not quantify expiry metadata or expired-key reclamation. The baseline has known correctness bugs and is a measurement control, not a safe rollback recommendation.
