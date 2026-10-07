# Key lifecycle contracts — test-first baseline, 30 September 2026

Follow-up: [the implementation repair](KEY-LIFECYCLE-FIXES.md) resolves these
failures. The results below preserve the original test-first baseline.

Added `tests/unit/command/key_lifecycle_test.zig` to the normal `zig build test`
entry point. **No engine implementation changed in this pass.** The suite is
intentionally red until the remaining compatibility defects are repaired;
there are no skips or expected-failure exemptions.

## Scope

29 test groups use fresh, fully initialized stores with the testing allocator.
Each runs against both CommandHandler backends: legacy KVStore and ConcurrentKV.
The fixture stays at a stable address, initializes locks, frees owned responses,
and destroys all stores after each independent case.

| Contract | Coverage |
|---|---|
| Wrong type, unchanged data | 46 commands × four incompatible types × two backends = 368 cases. Reads, writes, removals, integers, and set operations check the WRONGTYPE error class, original contents, and absence of hidden entries in another store. |
| Key visibility | TYPE, EXISTS (including duplicate keys), DBSIZE, KEYS, TTL/PTTL sentinels across string/hash/list/set/sorted set. |
| Delete and reuse | DEL duplicate/missing counts; all 25 before/after type combinations per backend; read every store to catch surviving data. |
| Replace | SET, SET NX/XX, MSET; deleting the replacement must not resurrect the former collection. MGET's nil response for non-string keys is covered separately. |
| Collection lifecycle | Missing reads/removals, empty hash values, duplicate fields/members, partial removal, final HDEL/LPOP/RPOP/SREM/ZREM, and LTRIM to empty. |
| Expiry | Immediate EXPIRE/PEXPIRE with zero/negative TTL for every type; positive PEXPIRE, PERSIST return values, update preservation, SET clearing TTL, and clean recreation. |
| Database scope | SELECT isolation; DEL and FLUSHDB affect only the selected database; FLUSHALL clears both databases for every type. |
| Rejected mutations | Malformed HSET, invalid LSET indices, integer argument counts, invalid integers, signed boundaries, and overflow of cached integers. |

Checks keep running after response mismatches within a scenario so later state
checks can expose mutation. A failed group is not a count of distinct defects;
several assertions can share the same missing type/lifecycle check.

## Results

`zig build test -Doptimize=ReleaseSafe` on Linux arm64, using the existing
`vex-perf-local:arm64` compiler image with networking disabled:

| Tests | Passed | Failed |
|---|---:|---:|
| Existing suite | 284 | 0 |
| New groups | 7 | 22 |
| Total | 291 | 22 |

The passing new groups cover MGET's type exception, hash empty values and field
counts, malformed HSET atomicity, invalid list indices, and three integer
boundary/arity/cached-overflow groups. Wrong-type matrices and the remaining
key lifecycle groups expose failures. All groups compile and finish; the run
returns nonzero because assertions fail.

An isolated Redis **8.10.1** control passed **435 assertions** validating the
explicit command sequences, wrong-type response matrix, and selected generic
key/expiry expectations. This is an oracle check, not a second run of the Zig
suite. The control container was removed. Sorted-set contents are checked
by member and numeric score membership, avoiding textual float-format noise.

Raw unit output, machine-readable results, failed group names, source hash,
and the control probe are retained under `results/key-contract-*-20260930.*`
(git-ignored local evidence).

## Run and next repair

```sh
zig build test -Doptimize=ReleaseSafe
```

Use the existing compiler image on this Mac if Zig is not available locally:

```sh
docker run --rm --network none \
  -v "$PWD:/app" -w /app --entrypoint zig \
  vex-perf-local:arm64 build test -Doptimize=ReleaseSafe
```

Next, unify key-type validation and lifecycle with the required locking, then
rerun these contracts and the upstream/differential/concurrent checks. Keep
unsupported commands (including INCRBYFLOAT) separate from implemented-command
behavior. INCR performance remains unmeasured after the earlier repair.

These units do **not** execute Worker's reactor fast dispatch, simulate competing
threads, prove timed-expiry races or multi-key atomicity, or cover persistence,
replication, every Redis command/option, RESP3, or adaptive ownership. Existing
ConcurrentKV tests cover deterministic string expiry and increment concurrency;
network and concurrent-history checks remain necessary after implementation.
The new TTL tests use a generous live deadline without sleeps; exact deadline
behavior remains in the existing engine tests.

Expected semantics follow the pinned Redis suite and Redis documentation:
[SET](https://redis.io/docs/latest/commands/set/),
[EXPIRE](https://redis.io/docs/latest/commands/expire/), and
[HDEL](https://redis.io/docs/latest/commands/hdel/).
