# Vex load-test harness

## Latest request benchmarks

The [October 5 report](../../docs/benchmarks/2026-10-05/request-scaling/comparison.md)
contains all 15 confirmed GET/hash/sorted-set CPU combinations. The 2/4/8-CPU
cohort has 27 validated confirmations on the same hosts; the full 1–16 CPU
inventory spans cohorts and must not be plotted as one same-host scaling curve.

From the repository root, reproduce the consolidated report and offline guards:

```sh
python3 bench/loadtest/scripts/summarize_current_request_scaling.py
python3 bench/loadtest/scripts/test_current_request_scaling.py
python3 bench/loadtest/scripts/test_request_accounting.py
```

The consolidator uses versioned summaries. `analyze_request_accounting.py RUN OUTPUT`
uses retained raw evidence and excludes delayed accounting brackets without
inventing CPU costs or discarding separately validated throughput. All run-owned
October 5 AWS resources were verified removed.

## Historical three-engine comparison

Use the [September 29 sorted-set benchmark package](../../docs/benchmarks/2026-09-29/README.md)
for the historical matched three-engine data, charts and reproduction instructions. Its AWS
runner is `scripts/compare_adaptive_aws.py`; offline validation is
`scripts/test_adaptive_aws.py`; summaries use `scripts/summarize_adaptive_aws.py`.
Superseded public claims and charts were deleted; raw runs and shared helpers remain.

## Other benchmark tools

For mixed sizes, hot keys, TTL churn, burst/recovery, soak tests, and a Locust
cache-aside application scenario, see [Realistic workloads](REALISTIC-WORKLOADS.md).

For CPU-quota scaling with increasing offered load, scheduled-arrival latency,
and load-generator headroom checks, see [CPU scaling](SCALING.md).

The older tools below cover other protocols. Pinning, offered load, latency
measurement and saturation checks vary; use each run’s recorded protocol.
The historical adaptive comparison uses unpinned workers and fixed concurrency.

Two packagings:
- `terraform/` — big-node "autorun" on dedicated EC2 (the 4→48-core *scaling*
  story; Dragonfly's regime). `terraform apply` provisions a server + several
  client boxes, runs the sweep, ships results to S3, tears down.
- `helm/` — in-cluster regression benchmark for the 4–8 core regime (vex's
  target), runnable on every release. `helm install` → results in pod logs.
  Limited to the cluster's node size (scrum nodepools cap at 8 vCPU).

`scripts/` holds the validated benchmark logic (server orchestrator + client
setup + analysis) shared/adapted by both.

See [docs/benchmarks.md](../../docs/benchmarks.md) for methodology and results.

### Sorted-set partition override

Use `--sorted-set-partitions N` with reactor mode to vary lock partitions without
rebuilding Vex (default 256; power of two, 1–4096). See [configuration](../../docs/configuration.md) for the option and
[retained AWS partition-sweep evidence](runs/2026-09-28-partition-sweep-aws/) for the original measurements.

### Reproducing the current request-scaling checks

`fixtures/aws-scaling/` contains the small, pinned deployment templates and
engine inventory used by the AWS comparison helpers. These templates refer to
the original `scrum-fresh` cluster and its node class; adapt them to your own
cluster before provisioning. Importing the helpers and running their offline
tests does not provision resources.

Run `python3 scripts/test_request_scaling.py` and
`python3 scripts/test_getset_capacity.py` for the current sweep's offline checks.
The full `scripts/test_*.py` set also validates historical protocols.
`sweep_request_scaling_aws.py` accepts an explicit frozen binary, namespace and
output directory; consult `--help` before running an authorized AWS study.

Raw `runs/` directories, frozen executables and caches are not versioned.
Historical profilers referencing a dated run require that separately retained
local evidence; published summaries are under `docs/benchmarks/`. The October 1
request-scaling runs remain incomplete and must not be combined across host
cohorts into a completed scaling curve.
