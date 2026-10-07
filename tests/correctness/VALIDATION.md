# Initial setup validation — 29 September 2026

All four tools are installed and runnable. **Vex does not pass the initial
compatibility sample.** The failures below are product findings, not waived
assertions or installation failures. No engine source was changed in this task.

## Tested setup

- Vex development binary: `bench/loadtest/runs/2026-09-29-dynamic-fd/final/bin/vex`.
- Binary SHA-256: `a208c4d0f6f8a292e7122ac0a0d3609d577f42b6d0d23eb38a30c32f1561d4f1`.
- Four Vex workers, unpinned, no persistence, adaptive ownership disabled.
- Separate disposable Redis 8.10.1 target and reference containers; exact
  image digest recorded by `smoke.py`.
- All containers limited to 2 CPUs and 2 GiB, published only on loopback.
- This is a local correctness smoke check, not an AWS/performance comparison.

## Results

| Check | Redis control | Vex |
|---|---|---|
| Redis 8.10.1 increment file, core profile | 32 passed, 0 failed, 0 unsupported | 17 passed, 3 failed, 12 unsupported |
| Valkey 8.1.3 increment file, core profile | 31 passed, 0 failed, 0 unsupported | 16 passed, 3 failed, 12 unsupported |
| Hypothesis, seed 20260929, 10 examples × up to 30 actions | Passed, 300 actions | Failed; shrank to a single HSET |
| Porcupine register model | 3 histories passed | 3 histories passed |
| Porcupine sorted-set model | 3 histories passed | 3 histories passed |

Each Porcupine history has eight clients × 30 operations (720 operations per
model per server). These short samples are **not** a concurrency certification.
Offline checker tests also reject deliberate lost updates and incorrect ranks,
generate failure visualizations, and classify request errors as inconclusive.

The Redis core profile logs three ignored entries: FUNCTION FLUSH bootstrap,
SCRIPT FLUSH bootstrap, and one debug-tagged test. Valkey logs two: FUNCTION
FLUSH bootstrap and one debug-tagged test. No explicit per-test skips occurred
in this sample. Strict unadapted upstream startup fails on Vex's unsupported
FUNCTION FLUSH, before testing command semantics. Core adapters and raw logs
are retained; no test assertion was replaced.

## Findings to fix

1. **Integer overflow:** `SET k 0` then `DECRBY k -9223372036854775808`
   returns `-9223372036854775808` on Vex; the upstream test expects an error.
2. **Wrong type:** INCR against a list returns `1` instead of WRONGTYPE.
3. **Arity:** INCR with an extra argument executes instead of returning an error.
4. **HSET with an empty field value:** Hypothesis reduced a mismatch to
   `HSET k a ""` on a new key: Vex returned `0`, Redis returned `1`.
   The separate regression probe also observed an incorrect final key state.
5. **Immediate expiry visibility:** an earlier exploratory Hypothesis run found
   `INCRBY k 0; EXPIRE k 0` followed by TYPE reporting `string`, although GET
   returned nil; Redis reports `none`. The later single-shot regression passed
   this case, so preserve it as a timing-sensitive finding, not an always-failing
   reproducer. The earlier run was exploratory; the final runner correctly
   applies its explicit seed to the state-machine factory.

The twelve unsupported cases are tests encountering unknown commands (notably
INCRBYFLOAT); they remain failures in the run exit status and are counted
separately from incorrect results for executed commands. Redis and Valkey's
three failing assertions overlap; do not count them as six distinct defects.

## Evidence and cleanup

- `results/setup-verified-20260929/`: final control/Vex sample, raw upstream logs,
  core-profile patches, JSON reports, shrunk Hypothesis trace, histories,
  server logs, and cleanup results.
- `results/setup-smoke-20260929/`: original unadapted bootstrap failures and
  exploratory expiry counterexample.
- `regressions.py`: focused HSET and immediate-expiry probes.

Every smoke-run container was removed; a final Docker query found no remaining
containers with the smoke-test label. Results and downloaded dependencies are
git-ignored; this summary and the runnable harnesses are ordinary source files.

Still to do: the full selected upstream files, longer randomized campaigns,
ownership-transition histories, multi-key atomicity, timed-expiry races, and
persistence/recovery testing. No complete Redis-compatibility or production
correctness claim follows from this setup validation.
