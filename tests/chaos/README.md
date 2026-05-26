# vex chaos test harness

SRE-owned scripts that validate the stability guarantees vex shipped in the
v0.8 stability sweep (S1–S13). Each script targets a specific durability or
availability invariant from the plan and either prints **PASS** or **FAIL**.

These are **bash/python**, not Zig — subprocess orchestration and signal
injection are awkward in Zig, and chaos scripts live in operator-land.

## Scripts

| Script | Validates | Slices covered |
|---|---|---|
| `kill-and-restart.sh` | After `kill -9` at any point during writes, vex restarts cleanly and no acked write is lost beyond the `appendfsync` bound. | S1, S2, S3 |
| `slow-disk.sh` | A 100ms-per-fsync injected delay does not stall the hot path under `appendfsync everysec`; under `always` throughput drops but no writes are silently dropped. | S2 |
| `partition-leader.sh` | A network partition that severs the leader from quorum produces a single new leader (no split-brain); the partitioned old leader self-demotes when it sees a higher epoch. | S10, S12 |
| `fill-disk.sh` | Filling the data dir to ≤1MB free puts vex in STOP-WRITE mode; reads continue; clearing the disk + `CONFIG SET appendfsync no` releases the flag. | S4 |

## Running

```bash
# All scripts assume a built vex binary at zig-out/bin/vex.
zig build -Doptimize=ReleaseSafe

# Each script runs against an ephemeral data dir under /tmp/vex-chaos.
./tests/chaos/kill-and-restart.sh
./tests/chaos/slow-disk.sh
./tests/chaos/partition-leader.sh
./tests/chaos/fill-disk.sh
```

Run individually or via `make chaos` (TODO: wire a top-level target).

## Status

All four scripts are currently **scaffolds** — they exit 0 with `TODO`. They
are committed so the SRE side of the tree exists, the scope is documented,
and the implementation can be picked up by anyone (operator-shaped work,
not core Zig work).
