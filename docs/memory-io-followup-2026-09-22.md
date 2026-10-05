# Memory density and transport follow-up

Status: completed. The benchmark clock repair and measurement tools are
retained. The global allocator switch and pointer-entry prototype were
rejected; the latter's exact production changes were removed. No new engine
optimization is promoted by this work.

The current work targets memory density and network/I/O costs. Worker-owned
shards are deferred: the available profile does not establish shared-store
locking as the limiting cost, and cross-worker routing would introduce new
coordination. A competing engine's architecture is not evidence that the same
change would improve Vex.

## Measurement repair

Four earlier local latency samples contain a signature consistent with a
negative wall-clock interval wrapping into an unsigned 32-bit microsecond
value. For example, 4,294,967.077 ms is exactly `(2^32 - 219)` microseconds.
The pinned memtier 2.5.1 source uses `gettimeofday` for request timestamps.
This identifies a plausible measurement mechanism; it does not establish the
underlying host/VM clock cause or explain the observed throughput drift.

The client-only patch in `bench/loadtest/patches/memtier-monotonic-clock.patch`
replaces all 25 application timestamp calls with `CLOCK_MONOTONIC` plus one
fixed epoch offset. The sampler uses the same Linux VM monotonic clock and
the reported per-invocation offset. Existing duration, latency, concurrency,
resource, identity and correctness guards remain required. Deterministic
tests check backwards wall-clock behavior, shared initialization across
translation units, timestamp normalization and fail-closed clock errors.

New comparisons must use the same immutable rebuilt client for every cell.
The client repair is not a Vex performance improvement. Earlier raw results
are retained unchanged.

## Storage accounting

A separate native diagnostic loads the real `ConcurrentKV`, overwrites every
value, validates content, and deinitializes the store. It counts live requested
bytes and actual map capacity, sums resident page coverage with `mincore`,
and records process RSS. Page coverage is not exclusive map RSS: neighboring
libc allocations can share a boundary page. Fresh processes use fixed
16-byte keys.

| Dataset | Map capacity | Map requested | Loaded RSS |
| --- | ---: | ---: | ---: |
| 1M × 32 B | 2,097,152 | 162.01 MiB | 194.81 MiB |
| 1M × 256 B | 2,097,152 | 162.01 MiB | 454.24 MiB |
| 100k × 4 KiB | 156,672 | 12.11 MiB | 409.14 MiB |

Both million-key runs have all 256 maps at capacity 8,192. Equal-size
overwrites make no further allocations; tracked live requested bytes return
to zero after deinitialization. Retained allocator RSS is distinct from a
live-allocation leak. These single-thread storage diagnostics are accounting
evidence, not server throughput measurements.

The pinned Zig startup selects `std.heap.c_allocator` for a libc-linked
release executable. Earlier SMP-allocation estimates therefore do not apply
to the current server. The measured 32-to-256-byte RSS difference is about
272 bytes per key, consistent with a 256-byte value plus allocator overhead.
Any storage candidate must preserve value-buffer reuse and be compared with
this actual allocation behavior.

Raw accounting: `bench/loadtest/runs/2026-09-22-memory-density`.

The bounded allocator comparison rejects a global SMP switch. With 1M ×
256-byte values, SMP initially used 436.09 MiB versus libc's 454.22 MiB.
After deleting all keys and reloading alternating 256/257-byte values, SMP
used 680.28 MiB versus 454.22 MiB. After `FLUSHDB`, tracked live allocations
were zero for both, but RSS remained 517.79 versus 291.73 MiB. A separate
20k-key 4096/4097-byte control showed the same direction: mixed reload used
161.94 MiB with SMP versus 83.39 MiB with libc. These results include each
allocator's reuse/retention history within a fresh process. They are not a
claim about steady-state throughput or universal allocator behavior.

Portable accounting: `bench/loadtest/results/allocator-accounting-2026-09-22.json`.

## Bounded storage candidate

A default-off `-Dindirect-reactor-entries=true` build experiment replaced each
reserved 64-byte hash-table value slot with an 8-byte pointer to one separately
allocated live Entry. The allocator, inline threshold, stripe count, hashes,
load factor and reusable value buffers remained identical. Before running the
experiment, the estimated saving at the measured million-key capacity was
about 35.7 MiB after libc allocation overhead. The build option has since
been removed with the rejected prototype.

The extra pointer lookup and allocation on new keys can cost speed. Higher
table occupancy can also erase the memory benefit. A predeclared fresh-process
off/on/on/off accounting matrix therefore covers 1M × 256 B, 200k × 256 B,
200k × 32 B, 1M × 32 B, and 100k × 4 KiB. The primary case must save at least
24 MiB; other loaded-RSS cases may not regress more than 2%. Every case checks
overwrite allocations, deletion, mixed-size reload, flush and exact live-byte
reconciliation. Correctness tests run with both build flags.

Default promotion additionally needs stable matched GET/SET/mixed throughput
and p99 evidence. The local transport stability failure does not satisfy that
condition; no new source behavior will become the default on accounting alone.

All 20 fresh-process accounting cells passed, including exact allocation
reconciliation, unchanged allocation counts during equal-size overwrites,
zero tracked live bytes after flush/deinit, and zero swap/memory-limit events.
The following values are medians of two processes per layout:

| Dataset | Direct loaded RSS | Indirect loaded RSS | Saving |
| --- | ---: | ---: | ---: |
| 1M × 256 B | 454.24 MiB | 418.91 MiB | 35.32 MiB (7.78%) |
| 200k × 256 B | 82.62 MiB | 81.97 MiB | 0.65 MiB |
| 200k × 32 B | 32.45 MiB | 30.21 MiB | 2.24 MiB |
| 1M × 32 B | 194.83 MiB | 159.33 MiB | 35.50 MiB (18.22%) |
| 100k × 4 KiB | 409.13 MiB | 408.42 MiB | 0.72 MiB |

The primary case saves at least 35.32 MiB in each pair; every other pair has
lower loaded RSS. The 200k-key cases actually reserve 288,768 slots, giving
69.26% occupancy. They do not establish savings at higher occupancy, where
the extra libc allocation per live Entry can erase the benefit.

The retained-memory result prevents an unconditional memory-win claim. After
the delete/mixed-reload/flush sequence, all tracked allocations are freed,
but the indirect layout retains more RSS: 360.61 versus 291.74 MiB for the
1M × 256 B case, 145.79 versus 55.22 MiB for 1M × 32 B, and 392.03 versus
269.85 MiB for 100k × 4 KiB. This is allocator retention, not a demonstrated
live-allocation leak. This tradeoff further supports rejecting the prototype.

Both native ReleaseSafe builds pass all 262 tests. Socket suites pass with
direct entries, indirect entries, and indirect entries on the fallback ring;
they cover concurrent varying-size values, GET/MGET/SCANGET/INCR/TTL, binary
payloads, fragmented requests, ordered bursts over 64 KiB and reconnects.
An additional COPY socket probe was excluded after the direct control
returned `ERR unknown command`; no COPY integration pass is claimed.
Preallocated ownership and its allocation failure are checked at unit level.

Portable results: `bench/loadtest/results/entry-density-2026-09-22.json`.
The diagnostic's insertion/overwrite timers include formatting, locks and
allocation instrumentation and must not be presented as server throughput.

A separate four-cell direct/indirect/indirect/direct mixed-P1 pilot uses the
same clock-repaired client and fixed two-worker setup. Both pairs must retain
at least 98% of control throughput, keep p99 within +5% and CPU/op within +2%,
and save at least 24 MiB of peak server RSS. More than 5% within-mode drift
stops the run. Passing this limited pilot cannot authorize default promotion:
GET-only/SET-only checks and the retained-memory tradeoff remain unresolved.

The pilot completed all four cells and failed its declared performance gates:

| Cell | Ops/s | p99 | Server CPU µs/op | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Direct 1 | 463,406 | 0.439 ms | 4.257 | 458.21 MiB |
| Indirect 1 | 416,794 | 0.495 ms | 4.721 | 422.95 MiB |
| Indirect 2 | 403,514 | 0.495 ms | 4.855 | 422.99 MiB |
| Direct 2 | 442,776 | 0.471 ms | 4.447 | 458.30 MiB |

Median throughput fell from 453,091 to 410,154 ops/s (−9.48%), while peak
RSS saved 35.28 MiB. Both pairs failed throughput, p99 and CPU/op limits.
All eight warmup/timed loads passed validity checks. Between-repeat variation
was 4.55% for control and 3.24% for candidate, within the 5% limit; visible
within-run drift nevertheless limits causal precision. This is sufficient
to reject the local viability screen, not a universal 9.48% code penalty.

The prototype and build option were removed by reversing only their isolated
patch. Earlier SQ-head work and larger-value regressions were preserved;
the restored unit-test file matches the retained pre-experiment image exactly.
No further server benchmarks were run. Reproduction evidence is retained as
`bench/loadtest/results/entry-indirection-rejected.patch` and
`bench/loadtest/results/entry-layouts-2026-09-22.json`, with both native images.

## Transport control

The predeclared same-binary comparison uses TCP and a Unix-domain socket with
one and two workers. Both modes enable identical listeners and mount the
same task-owned socket volume; only the selected client transport changes.
Each fresh process loads 1M × 256-byte values, then receives a 10-second
warmup and 60-second sample at 128 connections, pipeline 1, 80% GET/20% SET.

The order is TCP/UDS/UDS/TCP for one worker and UDS/TCP/TCP/UDS for two.
Reject invalid or constrained cells and stop for more than 5% within-mode
variation. Transport attribution requires both paired gains to exceed 5%
with consistent CPU/op direction. This control can identify avoidable local
transport costs; it cannot establish a remote-TCP engine improvement.

The run stopped after three timed cells at its repeatability guard:

| One-worker cell | Ops/s | p99 | Server CPU µs/op | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| TCP 1 | 222,392 | 0.887 ms | 4.397 | 458.44 MiB |
| UDS 1 | 560,719 | 0.423 ms | 1.764 | 458.29 MiB |
| UDS 2 | 479,165 | 0.479 ms | 2.063 | 458.25 MiB |

UDS throughput varied 15.685% between repeats, exceeding the declared 5%
limit. The remaining TCP control and all two-worker cells were not run.
Consequently there is no completed paired comparison or scaling result.
The large observed transport difference supports further investigation, but
is not a validated 2× performance claim. All three timed cells and their
warmups passed clock metadata, latency consistency and checked error/resource
guards. CPU/op uses cgroup snapshots around the workload, including a small
amount of setup/drain/polling time; it is not an exact request-only interval.

Portable evidence: `bench/loadtest/results/local-transports-2026-09-22.json`.
Raw data: `bench/loadtest/runs/2026-09-22-local-transports`.

All work is local Docker on native ARM64. CPU sets and quotas do not exclude
unrelated VM workloads or establish exclusive physical cores. No cloud
resources, website claims, or default engine behavior change in this report.

Cleanup verified: all temporary builders, benchmark/test containers, networks
and the socket volume were removed. Unrelated development containers were
preserved. Native control/candidate images and the repaired client are retained
with the raw logs, portable results and rejected patch for reproducibility.
