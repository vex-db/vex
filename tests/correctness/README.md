# Public correctness checks

Four locally installed tools, independent of AWS benchmarks and the website:

| Tool | Pinned version | Purpose |
|---|---|---|
| Redis upstream Tcl suite | 8.10.1, exact revision in `upstreams.json` | Redis command behavior and edge cases |
| Valkey upstream Tcl suite | 8.1.3, exact revision in `upstreams.json` | Additional upstream compatibility tests |
| Hypothesis | 6.140.3 | Randomized sequential Vex/Redis comparison with shrinking |
| Porcupine | 1.0.3 | Concurrent history checking for registers and sorted sets |

Latest local repair results: [shared key lifecycle repair](KEY-LIFECYCLE-FIXES.md).
Earlier patch: [integer/expiry repair](INTEGER-FIXES.md).
The smoke runner also executes `integer_regressions.py` and
`key_lifecycle_regressions.py` against each server.

## Install

From this directory, use Python 3.11+, Go 1.24+, Git, and Tcl 8.5+:

```sh
python3 setup.py
```

On this Mac, use `/opt/homebrew/bin/python3.13 setup.py`; `/usr/bin/python3`
is too old. Dependencies and upstream checkouts live in `.venv/` and `.cache/`.
No database is started by setup. Python versions and Go checksums are recorded
in `requirements.txt` and `linearizability/go.sum`. Upstream source and licenses
remain in the pinned checkouts; source assertions are not rewritten.

## Run all four locally

The included smoke runner starts three disposable Docker containers: one Vex
server and two Redis 8.10.1 servers (target and reference for a positive control).
It binds random ports on loopback, limits each container to 2 CPUs / 2 GiB,
and removes only its own containers in `finally`. It runs an increment-file
sample from each upstream, 10 × 30 Hypothesis actions, and three bounded
Porcupine histories per model. Vex uses four workers; these are correctness
checks, not throughput measurements.

```sh
.venv/bin/python smoke.py \
  --vex-binary ../../bench/loadtest/runs/2026-09-29-dynamic-fd/final/bin/vex \
  --vex-image vex-perf-local:arm64 \
  --output results/smoke-001
```

Supply a Linux binary and runtime image for the same architecture. The paths
above use the existing local development build. `--adaptive` enables experimental
adaptive ownership, but a short smoke pass does **not** prove ownership transitions
occurred. The existing `../integration/adaptive_owner.py` tests those transitions.
Output directories must be new, so earlier failures are not overwritten.
The runner exits nonzero when Vex fails; this does not mean installation failed.

## Redis and Valkey suites

Point these commands only at a **disposable** server. Upstream tests issue
FLUSHALL and may change configuration. Each run saves commands, raw logs,
revision, exclusions, and counts in `results.json`.

```sh
.venv/bin/python upstream.py redis --port 6380 --core-profile \
  --allow-destructive --output results/redis-core-001
.venv/bin/python upstream.py valkey --port 6380 --core-profile \
  --allow-destructive --output results/valkey-core-001
```

Default files: strings, integer operations, lists, sets, hashes, sorted sets,
and expiry. Use repeated `--suite unit/type/incr` options to narrow the run.
`--test 'INCR against non existing key'` selects a named upstream test;
`--test '/^INCR '` selects by upstream regular-expression syntax.

The explicit **core profile** excludes the bootstrap `FUNCTION FLUSH` and
`SCRIPT FLUSH` calls because the tested Vex reactor does not support them.
It makes an isolated copy of the test files, logs each exclusion as `ignore`,
and saves the exact `core-profile.patch`. It does not replace any test assertion.
Omit `--core-profile` for the strict upstream bootstrap; that currently fails
on Vex before reaching the command tests. The same core profile is used for
the Redis positive control.

Both modes use upstream `--singledb`, `--ignore-encoding`, `--ignore-digest`,
and exclude tests tagged as needing internal debug, replication, persistence,
or selected configuration hooks. The exact tag list is in `upstream.py` and
each report. `--durable` lets upstream record a command error and continue;
it does not turn failures into passes.

Reports separate passed assertions, failed assertions, unsupported-command
errors, explicit skips, ignored entries, and initialization exceptions.
Ignored entries may represent a group or bootstrap operation, **not a count
of covered assertions**. Unsupported errors still make the run fail. Empty,
interrupted, and timed-out runs never pass. This is a scoped compatibility
report, not a claim of complete Redis compatibility.

## Hypothesis differential tests

Use separate disposable Vex and Redis endpoints. This harness only deletes
its own UUID-prefixed keys; the servers must not have concurrent writers to
those keys.

```sh
.venv/bin/python differential.py \
  --target redis://127.0.0.1:6380 --reference redis://127.0.0.1:6381 \
  --seed 20260929 --examples 100 --steps 100 \
  --output results/hypothesis-001
```

Actions cover strings, conditional writes, increments, hashes, lists, sets,
sorted sets, immediate expiry, and TTL sentinels. After each action, all
generated keys are compared by type and logical contents. Sorted-set score
formatting and unordered collections are normalized; errors compare classes,
not exact message wording. Unsupported commands fail rather than disappearing.
Timed expiry races, transactions, durability, and every Redis option are not
yet covered by this initial state machine.

Failures save the shrunk command trace and Hypothesis reproduction in
`failure-trace.json` and `failure.txt`. Repeat the seed with the same versions
to replay the generated test; timing-dependent failures can still vary.
Shrinking can run more examples than the requested successful-example budget.

The first two discovered mismatches also have minimal regression probes
(the expiry mismatch is timing-sensitive and may not fail on every attempt):

```sh
.venv/bin/python regressions.py --target redis://127.0.0.1:6380
```

## Porcupine concurrency tests

```sh
.cache/linearizability -addr 127.0.0.1:6380 -kind register \
  -clients 8 -rounds 10 -operations 40 -output results/register-001
.cache/linearizability -addr 127.0.0.1:6380 -kind zset \
  -clients 8 -keys 1 -rounds 10 -operations 40 -output results/hot-zset-001
```

The register model covers integer-valued SET/GET/INCRBY/DEL. The sorted-set
model covers ZADD/ZINCRBY/ZSCORE/ZRANK/ZREM, including score ties. Calls use
independent connections, automatic retries are disabled, and atomic sequence
numbers bracket calls and responses. Each round starts with new/cleared
UUID-prefixed keys. The checker partitions only by independent keys; these
models do not claim multi-key transaction correctness.

Every history is saved. A checker timeout or request error is `Unknown`
(inconclusive) and exits nonzero. Only `Ok` passes. Failed/inconclusive checked
histories include `history.html` for visualization. Replay without a server:

```sh
.cache/linearizability -replay results/hot-zset-001/round-000/history.json \
  -output results/replay-001
```

Offline harness checks:

```sh
.venv/bin/python test_harness.py
go -C linearizability test ./...
```

The Go checks include intentionally lost increments and incorrect ranks, so
we verify that the checker rejects bad histories as well as accepting good ones.
See `VALIDATION.md` for the initial setup results and current Vex failures.
