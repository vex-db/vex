# Vex memory density and reclamation — 2026-09-25

The release candidate fixes idle expiry reclamation, reports current process RSS correctly, and returns substantially more memory after explicit `FLUSHDB`. It does **not** itself meet the target of loaded memory between Redis and Dragonfly. The separate [32-byte entry prototype has now met the memory target at sampled flat-stage points](entry32-prototype-results-2026-09-25.md); its throughput remains unverified and it is not promoted.

## Measured memory behavior

These are native ARM64 **local Docker memory measurements**, not throughput results. Baseline engines use the pinned binaries from the earlier AWS comparison; the release candidate uses the new binary identified below. All use 6 CPU and 4 GiB container limits, identical 16-byte keys, no persistence, and no swap/OOM events. A persistent client sends batches of 1,000 commands. Each case starts a fresh process and preserves its PID, executable hash, raw INFO, independent `/proc` RSS, cgroup state, and value checks. Each phase below is one observation; allocator history can change residual RSS.

| Phase | Original Vex | Release candidate | Redis | Dragonfly |
|---|---:|---:|---:|---:|
| 800k keys × 256 B | 321.0 MiB | 320.7 MiB | 301.1 MiB | 249.6 MiB |
| 1M keys × 256 B | 459.6 MiB | 459.2 MiB | 371.7 MiB | 292.6 MiB |
| Delete all after growth to 1.6M; wait 5 s | 634.6 MiB | 633.7 MiB | 175.9 MiB | 126.7 MiB |
| Refill 200k, then FLUSHDB; wait 5 s | 471.9 MiB | **55.2 MiB** | 18.1 MiB | 31.7 MiB |
| 100k values shrink 4 KiB → 32 B | 415.4 MiB | 415.7 MiB | 145.2 MiB | 45.4 MiB |
| FLUSHDB after that shrink; wait 5 s | 415.4 MiB | **45.2 MiB** | 18.3 MiB | 36.9 MiB |

The growth case samples 200k, 400k, 600k, 750k, 800k, 825k, 850k, 900k, 1M, 1.2M, 1.5M and 1.6M. The full curve is in the [compact results](../bench/loadtest/results/memory-lifecycle-2026-09-25.json). Loaded memory remains essentially unchanged by the release fixes.

For 100k keys with 256-byte values and a 2-second TTL, original Vex still counted **100k keys after 30 seconds** without key access. The candidate counted **zero at both 5 and 30 seconds**, with each expiration counted once. Candidate RSS remained 44.3 MiB at both points: reclaiming objects for reuse does not ensure their pages return to the OS. Expiry can begin during preload; comparisons use observations after preload, not an assumption that all engines held the full set simultaneously.

## Where memory stays

The native allocation diagnostic separates three causes:

1. **Expired objects:** the reactor previously returned nil for expired reads without physically reclaiming entries during idle time. Bounded maintenance now scans actual table buckets, removes still-expired entries under the stripe lock, invalidates WATCH state, and releases owned allocations.
2. **Reserved table capacity:** after deleting all 1.6M entries, the original table still had 162.01 MiB of requested live backing allocation. Ordinary DEL still retains this capacity. Expiry maintenance can detach empty stripe maps.
3. **Allocator residency:** in the standalone diagnostic, FLUSHDB brought tracked live allocation to zero while RSS stayed at 56.93 MiB; an explicit trim reduced it to 1.93 MiB. That standalone process is not the multithreaded server, whose residual memory is higher and history-dependent.

The candidate calls `malloc_trim(0)` once after an explicit full flush releases all stores, and only with Linux glibc's C allocator. It does not trim on GET, SET, DEL or each expiry pass. The operation can return whole free pages across allocator arenas, but cannot promise a particular RSS floor. [Linux malloc_trim documentation](https://man7.org/linux/man-pages/man3/malloc_trim.3.html).

Shrinking heap values below half their capacity already releases their old buffer to the allocator; smaller changes deliberately reuse capacity. Both choices can leave high RSS. Automatic trimming and sparse-table shrinking on ordinary deletion remain unresolved.

## Implementation and validation

Maintenance uses one joined thread, bounded physical-bucket scans, try-locks, and a sticky TTL-present flag to skip scanning for workloads that have never accepted TTLs. The worker refreshes its cached clock after poll returns. WATCH validation is serialized with expiry maintenance; transaction control commands now reach their existing connection handlers after the hot dispatch declines.

`INFO used_memory_rss` now reports current residency with an availability field; the lifetime peak is separate. In every final-candidate memory phase, reported current RSS agrees with independent `/proc` sampling within 1 MiB. The macOS reporting branch was compile-checked, not run on macOS.

Validation passed: 269 ReleaseSafe unit tests, ReleaseFast build, idle expiry/WATCH/EXEC and refresh checks, 8 concurrent clients × 1,000 varying-size updates, GET/MGET/DEL/INCR/TTL and binary-value checks, idle wakeup, fragmented requests, an ordered burst larger than 64 KiB, and reconnects. Final candidate binary SHA-256: `4766521395da4fc139ddadcf84d19fe7763d1399535d5d11e79ddf18284d00eb`.

The first candidate's memory runs and failed WATCH regression are retained separately. The failure exposed a pre-existing transaction-dispatch bypass, corrected before the final candidate. Its earlier post-FLUSH RSS was 55.8–115.6 MiB; do not treat the final candidate's 45.2–55.2 MiB observations as a guaranteed floor.

The candidate's two synchronous FLUSHDB calls took 70.7 ms after the grow/delete/refill sequence and 26.3 ms after the overwrite sequence, measured inside the same client process. Fresh baseline timing controls took 31.9 ms and 6.9 ms respectively. Returning pages adds work to this explicit bulk operation. These are local single-call observations, not latency percentiles.

## Throughput and acceptance status

The earlier AWS result—about 684k ops/s versus 507k Redis and 506k Dragonfly—belongs to the original binary. **Throughput preservation for this candidate is not established yet.** The six-cell matched AWS confirmation is prepared, but uploading the tested binary to the private benchmark registry awaits explicit user approval after automatic approval review blocked the upload. No new AWS resources have been created for this confirmation.

The predeclared confirmation gates require at least 98% of control median throughput, no more than 5% throughput spread per variant, at most 110% of control median p99, and at most 102% of control median peak RSS, with the existing client-headroom, clock, identity, correctness and memory-pressure checks. Failed or unstable runs are not rerun for a favorable result.

Raw artifacts: `bench/loadtest/runs/2026-09-25-memory-lifecycle/`; prepared AWS confirmation: `bench/loadtest/runs/2026-09-25-memory-release-perf/`.
