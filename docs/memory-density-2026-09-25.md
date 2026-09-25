# Memory density validation — 2026-09-25

The candidate uses 32-byte table entries, one allocation for each heap string
and its key, and intermediate table capacities at the existing 80% load limit.
The comparison control retains 64-byte entries. Both include idle expiry and
explicit FLUSHDB memory reclamation.

These are **local native allocation diagnostics**, not server throughput or
cross-engine RSS results. Each case uses a fresh Linux ARM64 process,
glibc `c_allocator`, Zig `0.17.0-dev.314+eae06cf5c`, and identical 16-byte keys.
Growth cases insert up to each listed count in one process; boundary cases
start fresh for each value size. RSS comes from `/proc/self/status`.

| Keys | Value bytes | Control RSS MiB | Candidate RSS MiB |
|---:|---:|---:|---:|
| 800,000 | 24 | 108.66 | 83.16 |
| 800,000 | 25 | 108.68 | 100.59 |
| 800,000 | 32 | 108.67 | 100.58 |
| 800,000 | 33 | 144.73 | 100.56 |
| 850,000 | 256 | 392.80 | 304.35 |
| 1,000,000 | 256 | 454.29 | 350.90 |
| 1,250,000 | 256 | 526.86 | 426.71 |
| 1,300,000 | 256 | 541.37 | 457.79 |
| 1,600,000 | 256 | 628.53 | 540.56 |

[Complete sampled curve and boundary results](memory-density-2026-09-25.json)
include the new resize region, not just favorable plateaus. All 12 processes
reconciled requested allocation bytes and block counts to zero after
destruction. Equal-size overwrites made no allocations in all ten boundary
cases. This does not imply that glibc returns all freed pages to the OS.

The source under test matches candidate `e79ea49` and control `7ede9f1` in the
KV and reactor paths; local diagnostics were built before their final version
labels. Local ReleaseSafe tests passed 276/276; Debug passed 275 with one skip.
Three Linux io_uring socket checks passed: concurrent size-changing writes,
idle expiry/WATCH, and receive-path fragmentation/bursts/reconnects.

The candidate also passed [GitHub's Debug/ReleaseSafe, Sentinel, documentation](https://github.com/vex-db/vex/actions/runs/36114802006),
quick chaos and [five-minute production-shape checks](https://github.com/vex-db/vex/actions/runs/36114801842). The separate sanitizer
job did not complete; it is not counted as passed. A previous repository run
likewise reached GitHub's six-hour timeout. Future sanitizer jobs are bounded
to 15 minutes.

The tag workflow published both architectures for `0.8.1-rc.2` (control) and
`0.8.1-rc.3` (candidate) to `ghcr.io/vex-db/vex`. **Matched throughput remains
unverified** until these images can be pulled with registry read access.
Neither a Redis/Dragonfly speed claim nor the full server memory target is
accepted from the native diagnostic alone. The pending comparison uses one
million keys, 32-byte and 256-byte values, 80% GET / 20% SET, pipeline one,
128 connections, six server CPUs, separate client nodes and three fixed runs
per engine/value size. Results must identify the runtime image digest and
executable hash.
