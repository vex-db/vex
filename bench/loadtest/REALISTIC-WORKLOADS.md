# Realistic cache workloads

This suite exercises cache behavior beyond the fixed 80/20, single-value-size,
all-hit throughput benchmark. The workload parameters are explicit synthetic
assumptions, not a claim to reproduce production traffic.

## Profiles

All memtier profiles use 80% GET / 20% SET, pipeline 1, deterministic distinct
client seeds, a fresh key prefix, a complete preload, and a separate warmup.
The default is one million keys and 128 connections (32 threads × 4 clients).
The default **aggregate rate cap** is 128,000 operations/s, converted to
1,000 operations/s per connection. Misses are reported, never counted as
connection errors.

| Profile | Behavior | Default measured duration |
|---|---|---:|
| `baseline` | Uniform access, 256-byte values; bridge to the earlier benchmark | 60 seconds |
| `mixed` | Uniform access; 32/256/1,024/4,096-byte values at weights 50/30/15/5; random payloads | 60 seconds |
| `hot` | Same sizes, Gaussian key access centered in the key range, standard deviation 1% of keys | 60 seconds |
| `churn` | Same sizes, writes with random 1–10 second TTLs; reads can miss | 60 seconds |
| `burst` | Same sizes, 128k → 384k → 128k ops/s rate caps; three separate result files | 3 × 60 seconds |
| `soak` | Same TTL workload, sustained to observe memory/latency over time | 30 minutes |

The preload has no TTL. In churn/soak, measured writes gradually replace those
records with expiring records, so the miss rate evolves rather than starting
at equilibrium. The short TTLs deliberately make churn observable in bounded
tests; use application-derived TTLs before claiming production equivalence.
Burst phases retain the same data but reconnect memtier clients between phases.

Memtier is a closed-loop generator with per-connection rate caps. A cap is not
a guaranteed arrival rate. `target_attained` reports whether achieved throughput
reached 95% of the cap. Inspect this and client CPU before comparing latency;
the suite does not correct coordinated omission or model an external request queue.

## Run memtier profiles

Use the existing patched **monotonic-clock memtier 2.5.1** client image. The
driver reuses `profile_local_docker.py` for clock, error, and latency validation.
Keep the repository directory depth when mounting/copying these files.

```sh
python3 bench/loadtest/scripts/realistic_workloads.py \
  --host DEDICATED_SERVER --profile mixed \
  --output bench/loadtest/runs/my-mixed-run

# Unlimited closed-loop throughput, separate from matched-rate latency runs:
python3 bench/loadtest/scripts/realistic_workloads.py \
  --host DEDICATED_SERVER --profile mixed --rate 0 \
  --output bench/loadtest/runs/my-mixed-capacity-run
```

The output directory must be new. It contains the binary hash, exact commands,
effective configuration, stdout/stderr, full JSON histograms/time series, and
validated summaries. Raw output is retained if validation fails. Each invocation
uses a new prefix and does not flush the server; non-expiring keys remain until
the dedicated server is removed. Use fresh servers between comparison cells.

## Locust cache-aside scenario

The application scenario has 90% read-through journeys and 10% invalidations.
80% of key selections target the hottest 20% of keys. A cache miss incurs a
simulated 5 ms backend delay, then populates a mixed-size binary object with a
10–60 second TTL. It checks returned object contents. User counts follow
32 → 96 → 32, with 5–20 ms think time and 60 seconds per stage. These are
**concurrent users**, not fixed request rates. Values can be overridden through
the environment variables in the example.

```sh
python3 -m venv /tmp/vex-realistic-env
/tmp/vex-realistic-env/bin/pip install -r bench/loadtest/requirements-realistic.txt

REDIS_URL=redis://DEDICATED_SERVER:6379 \
CACHE_PREFIX=vex-realistic:UNIQUE_RUN_ID: \
CACHE_KEYS=10000 CACHE_STAGE_SECONDS=60 CACHE_USERS=32 CACHE_BACKEND_MS=5 \
/tmp/vex-realistic-env/bin/locust \
  -f bench/loadtest/scripts/cache_locust.py --headless --only-summary \
  --csv /tmp/UNIQUE_RUN_ID --csv-full-history --stop-timeout 5
```

Use a fresh prefix for each run. The scenario creates data through real misses;
it does not preload. CSV results separate `REDIS` commands (including GET hits
and misses) from the end-to-end `JOURNEY` named `cache-aside`, whose time includes
the simulated backend delay. **Do not use Locust's combined Aggregated row as
database throughput**: it contains both command and journey events. A missing
key is not a failure; Redis errors and corrupt values are failures. The process
exits nonzero on recorded failures or an empty test. The backend delay is a
simulation, not a database or HTTP service.

## AWS comparison

`realistic_aws.py` clones the dedicated server/client manifests and pinned
engine images from a saved run into a **new** `vex-realistic-*` namespace.
It uses separate c7g.2xlarge and c7g.8xlarge nodes in ap-south-1a, with the
existing 6-CPU server and 24-CPU client budgets. It recreates the server for
every profile/engine cell and alternates engine order across profiles/rounds.
Locust dependencies are installed only on the temporary client; the resolved
package versions are saved. Existing Vex source changes are not built or deployed.

```sh
# Short functional pilot: 14 cells, one per profile/engine. Not a release gate.
python3 bench/loadtest/scripts/realistic_aws.py \
  --source-run bench/loadtest/runs/2026-09-25-packed-growth-aws \
  --image-overrides bench/loadtest/results/realistic-image-mirrors-2026-09-26.json \
  --output bench/loadtest/runs/UNIQUE_RUN_ID \
  --namespace vex-realistic-UNIQUE_RUN_ID \
  --seconds 20 --soak-seconds 90 --keys 100000 --rate 128000 --rounds 1
```

The image override file maps the two original GHCR images to the same digests
in the existing private ECR repository. This avoids a missing namespace-scoped
GHCR pull secret. Executable hashes are still verified after every server start.
The runner rejects secret-dependent source manifests without overrides before
creating AWS resources; it does not copy registry credentials into the cluster.

For a repeated investigation use at least three rounds. Choose longer durations
deliberately: defaults include a 30-minute soak per engine/round. The inherited
pods have a four-hour deadline, so split large matrices into separate runs.
By default it compares rc.2/rc.3. Use `--engines candidate redis dragonfly` for
a matched three-engine pilot (21 cells), or `--engines redis dragonfly` to test
only those engines. Engine order rotates across profiles and rounds. The saved
inventory pins Redis 8.10.1 with six I/O threads and Dragonfly 2.0.0 with six
proactor threads, under the same CPU/memory budgets. YCSB is not part of this suite.

The AWS runner saves before/after process hashes and pod/node identities,
server RSS/CPU samples every 0.5 seconds, cgroup memory counters, per-phase
memtier client CPU, and all raw results. Profile RSS includes preload/warmup;
it is not a phase-specific peak. Inspect time series for soak behavior rather
than inferring a leak from a single peak. Resource changes or measurement errors
stop the run; they are not silently retried. An incomplete directory is not
resumed automatically.

Cleanup is requested in `finally`, including on failure, for only the new
namespace and its two NodePools. Save/inspect `nodes-active.json` and verify
the corresponding EC2 instances actually terminate. A killed local process
cannot execute `finally`; use the saved `resources.json` to clean up that run.

## Checks

```sh
python3 bench/loadtest/scripts/test_realistic_workloads.py
```

These cover per-connection rate conversion, burst stage rates, expected versus
unexpected misses, missing monotonic-clock metadata, empty runs and errors.
Local container smoke tests are compatibility checks, not AWS performance data.

References: [memtier](https://github.com/redis/memtier_benchmark),
[Locust custom clients](https://docs.locust.io/en/stable/testing-other-systems.html).
