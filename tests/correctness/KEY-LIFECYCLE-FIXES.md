# Shared key lifecycle repair — 30 September 2026

The 22 failing groups from the test-first baseline are repaired. The unit suite
now contains **318 tests**, including command implementations, former reactor
extensions, SCAN, deterministic collection expiry, independent partition
progress, and partition-count alignment. This is a local development patch, not a release or complete Redis
compatibility claim.

## Implementation

- One type lookup and WRONGTYPE validation path covers strings, hashes, lists,
  sets and sorted sets. Rejected operations retain the original data and cannot
  create hidden entries in another store.
- DEL/UNLINK, EXISTS, TYPE, DBSIZE, KEYS and SCAN share the logical keyspace.
  SET/MSET replace prior collections; NX/XX checks see all types. FLUSHDB respects
  the selected database; FLUSHALL clears every store and retains ConcurrentKV's
  memory-trimming behavior.
- Empty collections are reclaimed after response serialization. In particular,
  a list pop's borrowed value remains valid until it has been written.
- EXPIRE/PEXPIRE, TTL/PTTL and PERSIST apply to collections as well as strings.
  Collection deadlines are owned by the existing command partitions and checked
  lazily on access. Updates preserve deadlines; deletion and replacement remove
  them. No active collection-expiry sweeper or persistence-format change is
  included in this patch.
- Added LTRIM and ZREVRANK, which the contracts exposed as missing. LTRIM builds
  its retained range before replacing storage; ZREVRANK reverses the sorted rank.
- Reactor collection and multi-key shortcuts now use the shared handler. Basic
  GET/SET/INCR retain guarded shortcuts. Existing reactor-only extensions remain
  available through the handler. WATCH bookkeeping follows successful writes;
  read-only fallback commands no longer invalidate watched keys.
- Fixed the test helper's direct-storage probe to use `db:0:k`, matching the
  handler namespace. The command-level WRONGTYPE assertions were unchanged.

## Synchronization and performance

Type validation, mutation and response serialization share command-partition
locks. Ordinary sorted-set commands retain per-key partition concurrency.
The initial lifecycle repair routed generic collections through the command
mutex and all partitions because list/set maps were unsafe to reshape
independently. That implementation is the candidate measured in the AWS cost
study below. The subsequent partition repair removes that broad lock from
single-key collection commands; multi-key and global operations retain it.

The ReleaseFast AWS comparison established the initial regression. Published
website numbers have not been updated to either development build.

### Performance follow-up (ReleaseFast)

The [paired cost comparison](../../docs/benchmarks/2026-09-30/correctness-cost/comparison.md)
has all 28 completed cases: before/after builds at 4 and 16 CPU quotas on the
same isolated hosts. The baseline already includes the integer fixes. Both
binaries are frozen Linux arm64 ReleaseFast builds; this is separate from the
ReleaseSafe correctness binary documented below.

- Confirmed GET/SET/INCR throughput below 5 ms p99 falls about 3–7% across the two
  CPU allocations. At identical client settings, CPU time per operation rises
  about 2–7%.
- Hash updates/reads, list updates/reads and read-only set membership regress
  severely: confirmed throughput falls 85–88% at 4 CPUs and 89–98% at 16 CPUs.
  In that measured build, these commands took the global command mutex and all partition locks.
  The measurements are consistent with serialization, but do not isolate the
  cost of locking from removal of the older fast paths.
- Sorted sets lose 10.5% confirmed throughput at 4 CPUs and 11.6% at 16 CPUs.
  The final interrupted control was repeated on the same verified live process;
  writes from the interrupted trial were included in exact data validation.
- Loaded RSS is mostly similar; the 16-CPU list case rises from 89.1 to 103.7 MiB.
  These non-expiring, pipeline-1
  workloads do not measure TTL/persistence costs or conditional SET variants.

The partition repair below retains the type/lifecycle correctness contract while
narrowing synchronization. The buggy baseline is not a rollback recommendation. Capacity-derived
cost ratios assume an unchanged hourly server price and the tested connection
grid; they are not an invoice or an instance-count forecast.

Raw evidence and frozen sources: `bench/loadtest/runs/2026-09-30-correctness-cost/`.
Authentication interruptions and the recovery procedure are recorded alongside
the raw evidence. Both EC2 instances are independently verified terminated and
all run-owned Kubernetes resources are gone; see the run's
`cleanup-verification-latest.json`.

### Collection partition repair

- Lists and sets now own one resizable map per command partition, using the
  same key hash and configured partition count as the lock coordinator. Map
  growth on one partition cannot invalidate another partition's map entries.
- Single-key hash/list/set/sorted-set commands retain one partition lock across
  type validation, lazy expiry, mutation, response serialization and empty-key
  reclamation. HashStore also retains its existing internal stripe locks.
- Transactions, multi-key set algebra, enumeration and other global operations
  keep coordinated locking. No correctness checks or error assertions were
  removed. The existing shared handler performs collection commands.
- All 318 ReleaseSafe unit tests pass. The new progress test holds an unrelated
  partition locked while exercising each collection family, including expiry
  of a different key type. Startup alignment is checked at 1/16/256/1024/4096
  partitions, with map growth and invalid-count coverage.
- The network stress test passes at 1/16/256/1024 partitions with eight clients
  and 9,216 collection keys per run, covering concurrent map growth, borrowed
  replies, expiry/retyping, empty-key recreation and global enumeration. It
  includes mixed-case commands, keys longer than 600 bytes and databases 0/1.
- Redis differential, integer/lifecycle regressions, 200 cross-type races and
  Porcupine register/sorted-set checks pass again. The two upstream increment
  suites retain 13 unsupported cases each, with zero behavioral failures or
  exceptions. Disposable test containers are removed.

Evidence: `results/collection-locks-units-final-20260930.log`,
`results/collection-locks-smoke-final-20260930/`, and
`results/collection-partition-final-20260930/`.
The new stress test is `collection_partition_regressions.py --target <url>`.
The final Linux arm64 ReleaseFast candidate is
`.cache/collection-locks-final/bin/vex`, SHA-256
`31dd579e6b25826b6552516f21130b2bd039f7c8b19cb90730139fb1e78c4509`.
This repair has not yet been remeasured on AWS.

### Local before/after verification of the partition repair

The baseline here is the **correctness-fixed, globally locked** ReleaseFast
binary from the AWS cost study, not the earlier pre-lifecycle implementation.
Both binaries and the runner are frozen in
`bench/loadtest/runs/2026-09-30-collection-locking-final-local/`.
Baseline SHA-256:
`a238b319c81e531fefa695c503c8db9402e6c7d04b1eee68ff6b86b00c87cef8`.

Local Docker arm64/epoll, 4 or 8 CPU quota/workers, 32 connections, 8 client
threads, pipeline 1, distinct client seeds, 256 partitions, adaptive ownership
off, no persistence or TTLs. Each cell has a 3-second warmup and three 10-second
confirmations; the table reports medians. Before/after order alternates across
workloads. Hash/list/set traces match the AWS cost study; GET uses 100,000
256-byte values, and sorted sets use 64 sets × 4,096 members with 80% ZINCRBY /
20% ZRANK. CPU cost is server cgroup CPU time per completed operation.

| CPUs | Workload | Ops/s before → after | Throughput ratio | p99 ms before → after | CPU µs/op before → after |
|---:|---|---:|---:|---:|---:|
| 4 | GET | 430.9k → 439.1k | 1.02× | 0.167 → 0.167 | 9.00 → 8.83 (−1.9%) |
| 4 | Hash | 106.1k → 407.7k | 3.84× | 4.127 → 0.191 | 12.16 → 9.15 (−24.8%) |
| 4 | List | 100.4k → 398.1k | 3.97× | 4.191 → 0.167 | 12.08 → 9.83 (−18.6%) |
| 4 | Set membership | 108.4k → 434.3k | 4.01× | 4.079 → 0.167 | 12.03 → 8.84 (−26.6%) |
| 4 | Sorted set | 391.5k → 396.6k | 1.01× | 0.183 → 0.175 | 9.73 → 9.74 (+0.1%) |
| 8 | GET | 385.2k → 394.3k | 1.02× | 0.231 → 0.223 | 11.88 → 11.83 (−0.4%) |
| 8 | Hash | 106.5k → 360.5k | 3.38× | 5.151 → 0.239 | 13.78 → 12.55 (−8.9%) |
| 8 | List | 102.1k → 365.6k | 3.58× | 5.823 → 0.231 | 14.00 → 13.01 (−7.1%) |
| 8 | Set membership | 110.9k → 378.9k | 3.42× | 5.087 → 0.231 | 13.58 → 12.13 (−10.7%) |
| 8 | Sorted set | 364.9k → 366.5k | 1.00× | 0.231 → 0.231 | 13.16 → 13.03 (−1.0%) |

All 20 cells passed exact dataset validation; all 80 warmup/measured samples
passed client CPU/throttling gates. No server restarts or OOM kills occurred.
The run completed successfully and removed its disposable containers/network;
see `summary.json`, per-cell raw outputs and `cleanup.json`.

This demonstrates recovery from the collection-lock regression at identical
client settings. It does **not** establish peak capacity, AWS cost savings or
vertical scaling: fixed 32-connection load and a shared local Docker VM are not
a saturation sweep. The lower eight-worker rates therefore are not evidence
that the maximum eight-core capacity is below four cores. GET and sorted-set
differences are small and are not presented as performance wins. Restoring the
original AWS capacity requires a separate matched-host measurement.

### Direct comparison against the original pre-lifecycle build

The earlier local table measures recovery from the slow intermediate build. This
additional A/B uses the **original September 29 baseline**, before the key-type/
lifecycle changes; the earlier integer fixes were already present. Baseline SHA-256:
`2299b3ea28e9642640ff364dbf30dac3523700f46e5e5def96790b7cad258618`.
The repaired binary remains `31dd579e6b25826b6552516f21130b2bd039f7c8b19cb90730139fb1e78c4509`.

Same local protocol as above: 32 connections, pipeline 1, three 10-second
repetitions, unpinned workers, 3 GiB server memory limit and no TTL/persistence.
These are newly paired local measurements, not ratios between AWS and local runs.

| CPUs | Workload | Original → repaired ops/s | Change | Original → repaired p99 ms | CPU µs/op original → repaired (change) |
|---:|---|---:|---:|---:|---:|
| 4 | GET | 441.6k → 444.3k | +0.6% | 0.167 → 0.151 | 8.77 → 8.84 (+0.8%) |
| 4 | Hash | 448.1k → 434.4k | -3.1% | 0.175 → 0.159 | 8.34 → 8.90 (+6.7%) |
| 4 | List | 281.8k → 413.9k | +46.9% | 1.215 → 0.151 | 9.92 → 9.51 (-4.2%) |
| 4 | Set membership | 409.8k → 444.7k | +8.5% | 0.191 → 0.151 | 8.78 → 8.73 (-0.7%) |
| 4 | Sorted set | 413.3k → 403.7k | -2.3% | 0.175 → 0.167 | 9.38 → 9.68 (+3.1%) |
| 8 | GET | 396.7k → 394.8k | -0.5% | 0.215 → 0.223 | 11.67 → 11.78 (+0.9%) |
| 8 | Hash | 383.3k → 372.1k | -2.9% | 0.223 → 0.231 | 11.79 → 12.46 (+5.7%) |
| 8 | List | OOM → 350.7k | n/a | n/a → 0.247 | n/a → 13.22 |
| 8 | Set membership | 324.7k → 379.9k | +17.0% | 0.887 → 0.231 | 12.22 → 12.15 (-0.6%) |
| 8 | Sorted set | 367.2k → 365.3k | -0.5% | 0.231 → 0.231 | 12.92 → 13.15 (+1.8%) |

The original eight-worker list process was OOM-killed during the third measured
repetition (Docker OOM event, exit 137). Its first two repetitions are retained
but excluded from the comparison: it has neither a valid three-run median nor
final dataset verification. This observation does not establish the cause of
memory growth. The failed process used automatic removal, so its container logs
were unavailable afterward; Docker lifecycle events are preserved. The initial
runner stopped, and the remaining cells were completed in separate batches with
identical settings. The repaired list process completed all repetitions and
data verification at the same memory limit.

All 19 completed cells passed exact data verification and client-headroom gates.
All three batches have cleanup receipts. This run does not test SET/INCR, peak
capacity, 16 CPUs, TTL costs or persistence. Small percentage differences from
short local repetitions should not be treated as proven wins or regressions.

Raw evidence under `bench/loadtest/runs/`:
- `2026-09-30-original-vs-repaired-local/`
- `2026-09-30-original-vs-repaired-local-rest/`
- `2026-09-30-original-vs-repaired-local-list/`

The first directory contains `combined-results.json` and
`original-list-oom-events.jsonl`; successful cells each contain `complete.json`.

### Request-path optimization — 1 October 2026

This follow-up removes redundant work without changing the command-partition
locking contract:

- GET hits use the live string entry already protected by the stripe read lock,
  avoiding the earlier existence lookup. Misses and expired entries release the
  stripe lock before the shared type/expiry check; the command lock remains held.
- Collection type validation checks expiry and the expected store first. A miss
  still checks all other types. This relies on the existing single-logical-type
  invariant and the partition lock preventing concurrent retyping.
- Only collection writes prune empty keys, and only in the affected store.
  Cleanup remains deferred until borrowed reply data has been serialized.

Final ReleaseFast SHA-256:
`943ae2d799b346b5ee8a9d5832b1e8c238aac9c389b15da2b196ec35cc6b9124`.
The baseline is the prior partition repair (`31dd579e…`), not the original
pre-lifecycle build. Both binaries and changed sources are frozen in
`bench/loadtest/runs/2026-10-01-request-path-final-local/`.

Identical local protocol: 4/8 CPU quota/workers, 32 connections, pipeline 1,
256 partitions, adaptive ownership disabled, three 10-second measured
repetitions after warmup, no TTL/persistence. The table reports medians.

| CPUs | Workload | Prior repair → optimized ops/s | Change | CPU µs/op before → after (change) | p99 ms before → after |
|---:|---|---:|---:|---:|---:|
| 4 | get | 365.3k → 367.4k | +0.6% | 10.75 → 10.72 (-0.3%) | 0.175 → 0.175 |
| 4 | hash | 332.3k → 347.3k | +4.5% | 11.56 → 11.16 (-3.4%) | 0.215 → 0.199 |
| 4 | zset | 321.9k → 325.2k | +1.0% | 12.17 → 12.05 (-1.0%) | 0.191 → 0.215 |
| 8 | get | 381.7k → 389.3k | +2.0% | 12.07 → 11.80 (-2.2%) | 0.239 → 0.223 |
| 8 | hash | 369.5k → 366.4k | -0.8% | 12.49 → 12.51 (+0.1%) | 0.231 → 0.231 |
| 8 | zset | 368.2k → 366.8k | -0.4% | 13.01 → 12.88 (-1.0%) | 0.231 → 0.231 |

The result is modest and mixed. Several individual trial ranges overlap; the
four-CPU absolute rates also shifted below earlier local batches. These short
shared-host measurements do not establish a broad throughput win, statistical
significance, peak capacity or better scaling. Do not combine their percentages
with the earlier original-vs-repaired run to estimate original-vs-optimized
performance. Larger claims require isolated-host profiling and load sweeps.

Validation: all 318 ReleaseSafe unit tests passed. The final ReleaseFast build
passed Redis differential checks, integer/lifecycle regressions, concurrent
Porcupine histories and expanded GET checks (binary/empty/integer values,
mixed-case names, misses, expired strings and collections, long keys, databases
0/1 and EXEC). Redis/Valkey increment suites still have 13 unsupported cases
each, with zero behavioral failures or exceptions. All 12 benchmark cells
passed dataset validation and client gates; test/benchmark cleanup completed.

Evidence:
- `tests/correctness/results/request-path-final-units-20260930.log`
- `tests/correctness/results/request-path-final-smoke-20260930/`
- `bench/loadtest/runs/2026-10-01-request-path-final-local/`

The initial candidate and measurements remain in
`bench/loadtest/runs/2026-09-30-request-path-local/`; they are not the final build.

## Initial lifecycle repair validation

- `zig build test -Doptimize=ReleaseSafe --summary all`: 316/316 passed.
- `zig build check-docs`: passed.
- Focused integer and key lifecycle probes: passed on Redis 8.10.1 and the
  four-worker Vex reactor, including actual timed expiry and WATCH behavior.
- 200 bounded cross-type races on each server: passed. SET races HSET/RPUSH/SADD/
  ZADD; the result and deletion/recreation checks detect leftover collections.
  These are regression schedules, not an exhaustive linearizability proof.
- Hypothesis differential smoke: passed on Vex and the Redis positive control
  (10 examples, up to 30 steps each).
- Porcupine register and sorted-set checks: all three histories per model passed
  on Vex and Redis (720 operations per model per server).
- Offline harness checks: passed.

| Upstream increment suite, core profile | Passed | Behavioral failures | Unsupported cases |
|---|---:|---:|---:|
| Redis 8.10.1 against Vex | 19 | 0 | 13 |
| Valkey 8.1.3 against Vex | 18 | 0 | 13 |

The upstream suites still exit nonzero for unsupported commands, principally
INCRBYFLOAT. No assertions were removed or waived. The full Redis/Valkey suite,
longer randomized campaigns, ownership transitions, persistence/recovery and
OOM atomicity are not certified by these scoped checks.

## Final evidence

- Final network run: `results/key-lifecycle-fix-final-20260930/` (individual
  outputs, histories, source-file hashes and cleanup receipts).
- Final unit log: `results/key-lifecycle-fix-units-final-20260930.log`.
- Documentation check: `results/key-lifecycle-fix-docs-20260930.log`.
- Binary: `.cache/key-lifecycle-fix/bin/vex`, Linux arm64 ReleaseSafe.
- SHA-256: `6947c702c1c263a8566b1d4111fa8e3daf5d582675a7fbe3773208679b99d136`.
- All three disposable containers were removed successfully. The runner exits
  nonzero solely because the two upstream reports retain unsupported cases.

## Pre-commit validation — 5 October 2026

The current source was rebuilt with Zig 0.17.0-dev.314+eae06cf5c in the existing
Linux ARM64 Docker runtime. All 318 ReleaseSafe unit tests and `check-docs`
passed. The fresh ReleaseFast binary's SHA-256 is
`9b79428c6a7a5b29e3f5b3faae0bf7610127a3a8cba1be84d95be4b65492a980`.

The local smoke check passed integer, focused regression, key-lifecycle,
Hypothesis differential and Porcupine register/sorted-set checks. The Redis
and Valkey increment samples passed 19 and 18 assertions respectively, with
zero behavioral failures and exceptions. Each still reports 13 unsupported
cases; therefore the overall smoke command intentionally exits nonzero.
No assertions were disabled. Disposable smoke containers were cleaned up.

All seven offline benchmark test scripts and the correctness harness tests
passed from a clean index export without local run archives or a sibling
checkout. Local raw smoke evidence is in
`tests/correctness/results/commit-20261005-smoke/` and is not versioned.
This validates correctness and harness packaging, not additional performance
or a completed AWS scaling sweep.
