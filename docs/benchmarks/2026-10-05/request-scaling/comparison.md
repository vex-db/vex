# Current Vex request benchmarks — updated 5 October 2026

**15/15 CPU/workload combinations have confirmed measurements.** These are
October 1 (r4/r5) and October 5 (r6) measurements of the same frozen binary.
The r6 run fills gaps and repeats comparison points on one new host pair.
For repeated cells, the table selects the newest confirmed cohort, regardless of
which throughput is higher. All cohort measurements remain in the JSON.

## Confirmed throughput

Operations per second, median of three 60-second confirmations; pipeline 1.
Each confirmation passed the 5 ms p99 and client/network gates. Rates are
confirmed lower bounds, not proven maxima.

| CPU quota | GET ops/s | Hash ops/s | Sorted-set ops/s | Host cohort |
|---:|---:|---:|---:|---|
| 1 | 136,023 | 122,305 | 115,649 | r5 |
| 2 | 258,949 | 230,273 | 217,530 | r6 |
| 4 | 494,517 | 432,031 | 406,620 | r6 |
| 8 | 856,465 | 740,773 | 706,987 | r6 |
| 16 | 1,523,294 | 1,290,356 | 1,192,853 | r4 |

**Each cohort used different physical hosts.** The matrix is a coverage
inventory, not a single same-host scaling curve. Missing cells are not
interpolated; preliminary search trials are not confirmations.

## Calculated scaling on the same hosts

| Workload | CPU quota change | Cohort | Throughput ratio | Throughput gain | Quota scaling efficiency |
|---|---|---|---:|---:|---:|
| GET | 1 → 2 | r5 | 1.933× | +93.3% | 96.6% |
| Hash | 1 → 2 | r5 | 1.884× | +88.4% | 94.2% |
| GET | 8 → 16 | r4 | 1.774× | +77.4% | 88.7% |
| GET | 2 → 4 | r6 | 1.910× | +91.0% | 95.5% |
| GET | 4 → 8 | r6 | 1.732× | +73.2% | 86.6% |
| Hash | 2 → 4 | r6 | 1.876× | +87.6% | 93.8% |
| Hash | 4 → 8 | r6 | 1.715× | +71.5% | 85.7% |
| Sorted set | 2 → 4 | r6 | 1.869× | +86.9% | 93.5% |
| Sorted set | 4 → 8 | r6 | 1.739× | +73.9% | 86.9% |

Throughput ratio = higher-allocation ops/s ÷ lower-allocation ops/s.
Quota scaling efficiency = throughput ratio ÷ CPU-quota ratio × 100%.
This measures how closely throughput follows allocation growth, not physical-core
utilization or cloud cost. Connections are selected separately for each allocation;
these are not fixed-concurrency comparisons. Only the within-cohort pairs above
support scaling calculations; combining cohorts does not establish a 1 → 16 curve.

## Latency and calculated throughput per CPU

| CPU quota | Workload | Connections | Median p99 ms | Ops/s per allocated CPU | Observed ops/s range | Cohort |
|---:|---|---:|---:|---:|---|---|
| 1 | GET | 32 | 0.295 | 136,023 | 134,927–137,379 | r5 |
| 1 | Hash | 64 | 0.623 | 122,305 | 122,243–123,105 | r5 |
| 1 | Sorted set | 32 | 0.359 | 115,649 | 114,496–116,035 | r5 |
| 2 | GET | 32 | 0.151 | 129,475 | 258,263–262,036 | r6 |
| 2 | Hash | 32 | 0.199 | 115,136 | 229,872–232,546 | r6 |
| 2 | Sorted set | 32 | 0.199 | 108,765 | 215,979–218,665 | r6 |
| 4 | GET | 128 | 0.351 | 123,629 | 493,817–495,243 | r6 |
| 4 | Hash | 64 | 0.207 | 108,008 | 426,538–439,318 | r6 |
| 4 | Sorted set | 128 | 0.447 | 101,655 | 406,617–413,449 | r6 |
| 8 | GET | 256 | 0.415 | 107,058 | 854,038–856,637 | r6 |
| 8 | Hash | 256 | 0.471 | 92,597 | 738,725–741,829 | r6 |
| 8 | Sorted set | 256 | 0.511 | 88,373 | 705,546–713,560 | r6 |
| 16 | GET | 512 | 0.847 | 95,206 | 1,521,412–1,532,139 | r4 |
| 16 | Hash | 512 | 0.807 | 80,647 | 1,288,813–1,292,056 | r4 |
| 16 | Sorted set | 512 | 0.711 | 74,553 | 1,190,099–1,204,048 | r4 |

Ops/s per allocated CPU = measured ops/s ÷ CPU quota. This is allocation
efficiency, not throughput per consumed CPU second. Original cgroup CPU figures
remain in the source summaries and CSV; see the [accounting audit](../README.md)
before interpreting them. Delayed collection brackets prevent trustworthy host-cost
attribution for the 16-CPU sorted-set trials; no replacement CPU-cost estimate is made.

## Coverage and remaining comparisons

All 15 planned CPU/workload cells now have confirmed measurements.

- SET-only workload and current-build matched Redis/Dragonfly comparisons.

Additional comparisons require real runs on matched hosts. A complete inventory
across cohorts does not establish a same-host curve across all CPU counts.
The earlier 32-connection optimization percentages compared
builds at the same CPU allocation, not scaling between allocations.

## Protocol and provenance

- Server c6gn.8xlarge; separate c7g.16xlarge client with a 48-CPU quota.
- Server memory 8 GiB; workers equal CPU quota, unpinned; 256 partitions;
  adaptive ownership disabled; no TTL or persistence.
- GET: one million 256-byte values, uniform reads.
- Hash: 4,096 hashes × 16 fields × 256 bytes; 80% HSET / 20% HGET.
- Sorted set: 64 sets × 4,096 members; 80% ZINCRBY / 20% ZRANK.
- Uncapped closed-loop load; pipeline 1; service latency. Search 32–1,024
  connections, then confirm the fastest passing setting with three 60-second trials.

Measured binary SHA-256: `943ae2d799b346b5ee8a9d5832b1e8c238aac9c389b15da2b196ec35cc6b9124`.
This is the measured request-path build, not the later October 5 packaging build.

Sources: [r5](../../2026-10-01/request-scaling-r5/comparison.md),
[r4](../../2026-10-01/request-scaling/comparison.md), and
[r6](../request-scaling-r6/comparison.md). Raw evidence is retained locally.
r4 cleanup was verified; r5 resources were verified absent and cleaned up on October 5.
r6 resumed on the original hosts after credential refresh and completed all nine cells.
[Cleanup verification](../request-scaling-r6/cleanup-verification.json) confirms both
instances terminated and all run-owned Kubernetes resources were removed.

The [September three-engine comparison](../../../benchmarks.md#september-29-three-engine-comparison)
uses an older adaptive build and pipeline 16. Its competitor numbers cannot be
combined with this pipeline-1 table to claim a current Redis/Dragonfly advantage.

Reproduce from the committed source summaries:

```sh
python3 bench/loadtest/scripts/summarize_current_request_scaling.py
```

[Machine-readable results](summary.json) · [CSV](results.csv)
