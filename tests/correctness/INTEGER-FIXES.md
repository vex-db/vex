# First correctness patch — 29 September 2026

This local patch addresses integer arithmetic and string-key expiry. The broader
compatibility suite still fails. It is not a release or correctness certification.

## Changes

- Checked i64 addition for stored strings and cached integers. Overflow returns an
  error without changing the value. DECRBY rejects negation of the minimum i64.
- Exact argument counts for INCR, DECR, INCRBY and DECRBY.
- Reactor increments share ConcurrentKV's stripe write lock, including INCRBY and
  DECRBY; the unchecked atomic INCR shortcut has been removed. This also routes
  successful INCR through the existing WATCH-version update. Performance impact
  has not been measured.
- Incrementing a live numeric string preserves its expiry. The legacy handler
  preserves the absolute expiry too; network smoke validation uses reactor mode.
- EXPIRE/PEXPIRE with zero or negative TTL deletes string keys synchronously.
- Expiry checks consistently treat the exact deadline as expired in both KV
  implementations and reactor lookup paths.
- Reactor PTTL reads ConcurrentKV rather than the unused legacy KV table.

## Validation

- Linux arm64 ReleaseFast binary built separately at
  `.cache/integer-fix/bin/vex`; SHA-256
  `2299b3ea28e9642640ff364dbf30dac3523700f46e5e5def96790b7cad258618`.
- `zig build test -Doptimize=ReleaseSafe`: **284/284 passed**.
- `integer_regressions.py`: passed against both Redis control and Vex, including
  overflow rejection without mutation, wrong arity, TTL preservation across all
  four integer commands, and immediate expiry.
- Porcupine: all three register and three sorted-set histories passed for Vex
  and Redis control, 720 operations per model per server.
- Harness self-check passed (`.venv/bin/python test_harness.py`).

| Upstream increment file, core profile | Before: pass / fail / unsupported | After: pass / fail / unsupported |
|---|---|---|
| Redis 8.10.1 | 17 / 3 / 12 | 18 / 1 / 13 |
| Valkey 8.1.3 | 16 / 3 / 12 | 17 / 1 / 13 |

The mixed unhappy-path test now passes its integer-arity checks and reaches an
unsupported INCRBYFLOAT assertion, explaining the increased unsupported count.
The other remaining assertion failure is INCR against a list accepting the key
instead of returning WRONGTYPE. No assertions were waived.

Evidence: `results/integer-fix-verified-20260929/`, including raw outputs, binary
hash and successful cleanup of all three disposable containers. Earlier setup
results are retained. No AWS resources or frozen benchmark binaries were changed.

## Remaining priorities

1. Unify key-type validation and lifecycle across stores, with consistent locking.
   TYPE currently reports none for hashes; DEL omits hash/list/set stores. The
   Hypothesis HSET mismatch can therefore arise from stale keys surviving cleanup;
   the isolated new-key probe returned 1 correctly but TYPE still returned none.
   Do not treat this as a proven empty-value storage bug.
2. Check wrong-type errors and mutation atomicity under concurrent cross-type
   operations. This patch does not fix those or certify timed-expiry races.
3. Track unsupported commands such as INCRBYFLOAT separately, then expand upstream
   coverage and randomized/concurrent campaigns, including adaptive ownership.

## Test-first follow-up

The [key lifecycle unit-test baseline](KEY-LIFECYCLE-TESTS.md) adds 29 contract
groups before the next implementation repair. All 284 existing tests still
pass; 7 new groups pass and 22 fail. No engine changes are part of that follow-up.
