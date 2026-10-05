# Reproduce the published benchmark

Run these commands from the repository root. Do not overwrite the retained runs.

## Validate the harness and regenerate the chart

```sh
PYTHONDONTWRITEBYTECODE=1 python3 bench/loadtest/scripts/test_adaptive_aws.py
python3 -m venv /tmp/vex-benchmark-charts
/tmp/vex-benchmark-charts/bin/pip install matplotlib
MPLCONFIGDIR=/tmp/vex-benchmark-mpl /tmp/vex-benchmark-charts/bin/python \
  docs/benchmarks/2026-09-29/render.py
```

The chart reads only this package's `results.json`, checks 12 unique valid
engine/workload rows, and preserves the throughput min–max ranges. Current
results were combined from two retained runs; inspect `source_run` on each row.

## Repeat the measured configuration on AWS

This command provisions two isolated AWS instances through the configured
`scrum-fresh` EKS context. It requires working AWS and Kubernetes credentials,
the pinned runtime images and node class configured by the retained manifests.
Use a unique namespace/output directory. The runner requests cleanup in `finally`;
verify EC2 termination and absence of labeled Kubernetes resources afterward.
A credential interruption can prevent that cleanup request from succeeding.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 bench/loadtest/scripts/compare_adaptive_aws.py \
  --binary bench/loadtest/runs/2026-09-28-adaptive-three-aws/frozen/static-after/bin/vex \
  --client bench/loadtest/runs/2026-09-28-adaptive-three-aws/key-owner-client \
  --namespace vex-scaling-YYYYMMDD-comparison \
  --output bench/loadtest/runs/YYYY-MM-DD-comparison \
  --shapes hot mixed uniform64 uniform1024

PYTHONDONTWRITEBYTECODE=1 python3 bench/loadtest/scripts/summarize_adaptive_aws.py \
  bench/loadtest/runs/YYYY-MM-DD-comparison
```

The frozen binaries are intentionally used here. Building a changed source tree
is a new experiment and must receive a new results directory and binary hashes.
The published Linux Vex SHA-256 is
`a0d74431a5a761903864ff276f698287d78afe54b6b1ca2cc82ed4bfc733472c`;
the client SHA-256 is
`d41ff0a85814fc238845fa721470ce9f1bf3b5a77e2190e18582e18b96473847`.

Use `--resume` only for the same still-live hosts and original output directory;
the runner checks identities and hashes, retains completed cells, archives
incomplete cells and starts a fresh client. If hosts expired, run all three
engines for the unfinished workload on a new matched pair and document it.

## Interpretation

Eight-CPU quota / 8 GiB per engine; no persistence; 32 connections; pipeline 16;
three 60-second measurements following a ten-second warmup. Vex is unpinned with
256 partitions and experimental adaptive ownership enabled. Latency is the full
pipeline round-trip p99. Uniform traffic did not activate ownership. These are
fixed-concurrency comparisons, not peak-capacity, unpipelined, durability, or
production-soak claims. See the [full report](../../adaptive-three-engine-aws-2026-09-28.md)
for hardware, versions, exact-data checks and CPU/network gates.
