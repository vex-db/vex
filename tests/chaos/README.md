# vex chaos test harness

SRE-owned scripts that validate the stability guarantees vex shipped in the
v0.8 stability sweep (S1–S13). Each script targets a specific durability or
availability invariant from the plan and either prints **PASS** or **FAIL**.

These are **bash/python**, not Zig — subprocess orchestration and signal
injection are awkward in Zig, and chaos scripts live in operator-land.

## Scripts

### Durability + availability (durability sweep)

| Script | Validates | Slices covered | Status |
|---|---|---|---|
| `kill-and-restart.sh` | After `kill -9` at any point during writes, vex restarts cleanly and no acked write is lost beyond the `appendfsync` bound. | S1, S2, S3 | scaffold |
| `slow-disk.sh` | A 100ms-per-fsync injected delay does not stall the hot path under `appendfsync everysec`; under `always` throughput drops but no writes are silently dropped. | S2 | scaffold |
| `partition-leader.sh` | A network partition that severs the leader from quorum produces a single new leader (no split-brain); the partitioned old leader self-demotes when it sees a higher epoch. | S10, S12 | scaffold |
| `fill-disk.sh` | Filling the data dir to ≤1MB free puts vex in STOP-WRITE mode; reads continue; clearing the disk + `CONFIG SET appendfsync no` releases the flag. | S4 | scaffold |

### Concurrency regression suite (B-series fixes)

Repro scripts for the four memory-safety / availability bugs found in the
0.7.2 audit. Each script PASS/FAILs based on whether the corresponding fix
holds under load. Useful as regression gates before tagging a release.

| Script | Validates | Failure mode pre-fix |
|---|---|---|
| `pubsub-cross-worker-tls.sh` | **B1.** Cross-worker PUBLISH delivery routes through the owning worker's queue, never raw `write()` onto a TLS-wrapped fd. | glibc `realloc(): invalid next size` inside OpenSSL within ~3-10s of TLS pub/sub load. |
| `bgrewriteaof-availability.sh` | **B2.** `BGREWRITEAOF` runs on a background thread and the originating worker returns immediately; other workers' GETs stay responsive throughout the rewrite. | All workers stall on `kv_mutex` for the full rewrite (~5s+) and then abort commands with `-ERR`. |
| `hotpath-rehash.sh` | **B3.** Hot-path `GET`/`SET`/`MGET`/`INCR` take stripe `rdlock` around `map.getPtr`, surviving a concurrent rehash from `setInternal`. | Segfault or Zig panic when a stripe grows past its 16 384 pre-allocation. |
| `bgsave-snapshot.sh` | **Bonus.** `BGSAVE` snapshots `kv.map` under `kv_mutex` briefly, then iterates the snapshot for the file write — race-free even under heavy concurrent SET load. | Subtle (race depends on writer interleaving); manifested as occasional snapshot panics or load-time corruption. |

## Running

```bash
# All scripts assume a built vex binary at zig-out/bin/vex.
zig build -Doptimize=ReleaseSafe

# Each script runs against an ephemeral data dir under /tmp/vex-chaos-*.
./tests/chaos/pubsub-cross-worker-tls.sh
./tests/chaos/bgrewriteaof-availability.sh
./tests/chaos/hotpath-rehash.sh
./tests/chaos/bgsave-snapshot.sh

# Tunables — every script accepts env-var overrides. Examples:
DURATION=300 SUBSCRIBERS=64 PUBLISHERS=16 ./tests/chaos/pubsub-cross-worker-tls.sh
N_KEYS=1000000 ./tests/chaos/bgrewriteaof-availability.sh
TOTAL_KEYS=5000000 DURATION=120 ./tests/chaos/hotpath-rehash.sh
```

Run individually or via `make chaos` (TODO: wire a top-level target).

## Status

- The four **durability-sweep** scripts (`kill-and-restart.sh`, `slow-disk.sh`,
  `partition-leader.sh`, `fill-disk.sh`) are still **scaffolds** — `exit 0`
  with `TODO`.
- The four **concurrency-regression** scripts (`pubsub-cross-worker-tls.sh`,
  `bgrewriteaof-availability.sh`, `hotpath-rehash.sh`, `bgsave-snapshot.sh`)
  are implemented and produce explicit PASS / FAIL.
