# Compact reactor values: memory and performance acceptance

Status: **draft — full size-sweep acceptance is not complete**. The selected pilot passed; AWS session expiry interrupted the subsequent sweep. No results from incomplete or invalid runs are used below.

## Change

`ConcurrentKV` uses its own compact entry with a 32-byte inline buffer. Larger values use owned allocations that are reused for equal-size or modestly smaller overwrites. Growing beyond capacity allocates before releasing the old value, so allocation failure preserves the old data. Shrinking below half capacity, or back into the inline range, releases excess storage. Frees use the original allocation length.

Stripe tables grow under the existing exclusive stripe lock instead of reserving 16,384 entries per stripe. Inline reads resolve the entry's current address after rehashing. String writes now consistently use the exclusive stripe lock, allowing GET/MGET to copy under the read lock without the former shared-lock seqlock write shortcut. The plain KVStore layout and wire formats do not change.

## Same-machine pilot

Baseline is main commit `bbb52591465e445323769f7186a2acdd73a17376`; candidate code is `54ea1fc`. Both used Zig `0.17.0-dev.314+eae06cf5c`, ReleaseFast, x86_64_v3. Binaries and hashes are recorded in the adjacent JSON result file.

A dedicated c6a.2xlarge server allocated six vCPUs and 4 GiB to the database. A separate c6a.8xlarge client allocated 24 vCPUs drove 128 connections, pipeline 1, with persistence disabled. Both versions used the same nodes. Fresh processes loaded one million 256-byte values, followed by five seconds of warmup and three 15-second measurements per workload. Candidate ran first, baseline second. The client recorded no CPU throttling.

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Loaded RSS | 597.9 MiB | 446.4 MiB | -25.3% |
| GET median ops/s | 316,129 | 321,058 | +1.6% |
| SET median ops/s | 320,877 | 322,474 | +0.5% |
| MIXED median ops/s | 321,807 | 320,995 | -0.3% |
| GET median run p99 | 0.623 ms | 0.615 ms | -1.3% |
| SET median run p99 | 0.615 ms | 0.631 ms | +2.6% |
| MIXED median run p99 | 0.639 ms | 0.655 ms | +2.5% |

The pilot shows about 25% lower loaded RAM with throughput essentially unchanged. Small point-estimate differences do not establish a universal improvement or a guarantee for untested workloads. Payloads are compressible repeated ASCII bytes, not representative of arbitrary application data. RSS excludes sidecars and whole-node memory.

## Required sweep before acceptance

Compare baseline and candidate with fresh processes, alternating version order:

- 100K, 500K, 1M, and 2M keys at 256-byte values.
- 1M keys at 32-, 128-, 256-, 257-, and 1024-byte values (the 256-byte case overlaps).
- Three 10-second mixed runs per case after a five-second warmup; 80% GET / 20% SET, 128 connections, pipeline 1.
- Check loaded and peak process RSS, throughput, p99, client CPU, and errors/misses. Investigate material regressions before marking the PR ready.

The first 100K-key baseline attempt reported one GET miss despite a successful preload-count check. It is excluded and its raw artifacts are retained. The fresh rerun was interrupted by expired AWS authentication. There are no accepted full-sweep groups yet.

To reproduce each dataset, start a fresh server (`--reactor --workers 6 --no-persistence --port 6379`), preload with:

```sh
memtier_benchmark -s SERVER -p 6379 -t 1 -c 1 --pipeline 128   --ratio 1:0 --requests KEY_COUNT --key-minimum 1 --key-maximum KEY_COUNT   --key-pattern S:S --data-size VALUE_BYTES
```

Check `DBSIZE`, then warm up and repeat the timed command (use `--test-time 5` for warmup; use 15 seconds for the focused pilot):

```sh
memtier_benchmark -s SERVER -p 6379 -t 32 -c 4 --pipeline 1   --ratio 1:4 --test-time 10 --key-minimum 1 --key-maximum KEY_COUNT   --key-pattern R:R --distinct-client-seed --data-size VALUE_BYTES   --print-percentiles 50,95,99 --json-out-file result.json
```

Use `0:1` for GET and `1:0` for SET. Preserve each repetition separately. Sample the database process's RSS every second; measure loaded RSS before warmup. Run all versions on the same hardware and verify client headroom.

## Correctness validation

- 255 ReleaseSafe unit tests passed, covering buffer reuse/shrinks, allocation failure preserving data and TTL, preallocated replacement ownership, mixed-size contention, inline/heap values across table growth, and flush/reuse.
- `tests/integration/compact_values.py` passed against the release binary: eight clients each perform 1,000 contended varying-size updates, followed by binary GET/MGET, DEL, INCR, and TTL transitions.
- The normal reactor dispatch's pre-existing COPY/PTTL gaps are outside this change; preallocated COPY buffer ownership is covered directly by the unit test.
- No GitHub check results had been attached when the draft was opened. Local isolated Linux build/test results are retained with the benchmark artifacts.

Pilot data: [`compact-values-pilot.json`](../bench/loadtest/results/compact-values-pilot.json).
