# Published versus compact/reusable Vex follow-up

Measured 2026-09-22 on the same c6a.2xlarge server and c6a.8xlarge client. One million fixed 16-byte keys, 128 connections, pipeline 1, server quota six vCPUs and 10 GiB, persistence disabled. Three 20-second samples after a 10-second warmup per group. These are point estimates, not confidence bounds.

| Value | Workload | Published ops/s | Compact ops/s | Published loaded / peak MiB | Compact loaded / peak MiB |
|---|---|---:|---:|---:|---:|
| 32 B | MIXED | 317,333 | 325,612 | 400.4 / 401.6 | 199.0 / 200.3 |
| 256 B | MIXED | 309,080 | 316,388 | 400.6 / 401.9 | 458.7 / 460.0 |
| 4096 B | GET | 287,905 | 296,006 | 4,325.9 / 4,329.0 | 4,124.3 / 4,126.5 |
| 4096 B | SET | 268,728 | 275,973 | 4,325.9 / 7,722.4 | 4,124.3 / 4,126.3 |
| 4096 B | MIXED | 271,959 | 283,645 | 4,325.9 / 7,884.8 | 4,124.3 / 4,127.8 |

The existing compact implementation halves loaded RSS for 32-byte values and reduces the 4 KiB mixed-phase peak by 47.6%. It increases 256-byte loaded RSS by approximately 14.5%. No claim of universally lower memory is warranted. The 4 KiB sequence shares one process across GET, SET and mixed; it measures allocator history under that sequence, not an isolated mixed-only process.

All 30 accepted timed runs passed the harness checks for errors and misses; pre/post sampled values and key counts passed. An interrupted 32-byte run was rerun from a fresh server process and is not an accepted sample. Extended compact-value TCP integration passed (8 clients × 1,000 updates, sizes through 8 KiB, binary values through 64 KiB). All 257 ReleaseSafe unit tests passed, including repeated 4 KiB overwrite without allocation.

Portable summary: [`value-buffer-followup.json`](../bench/loadtest/results/value-buffer-followup.json). Raw provenance and individual runs: `bench/loadtest/runs/value-size-followup/{followup-settings.json,comparison.json}`. These local raw artifacts are ignored by Git; preserve them when transferring this checkout. The source and binary identities are recorded in `docs/performance-execution-plan.md`. No engine change was newly authored to obtain these compact-value results.
