# vex-bench

In-cluster throughput benchmark comparing **vex / Redis / Dragonfly** in the
**4–8 core regime**, driven by `memtier_benchmark`. Intended for CI / release
regression: *"did this release hold throughput vs Redis and Dragonfly?"*

It deploys three server workloads (each pinned to `serverCores` of CPU quota)
plus an orchestrator `Job` that, for each enabled server in sequence, preloads
the keyspace and runs a SET/GET × pipeline-1/30 sweep, emitting a CSV.

## Quick start

```sh
# Install into namespace scrum6 (default), 4 cores per server:
helm install vex-bench ./bench/loadtest/helm -n scrum6 --set serverCores=4

# Follow the run and read the CSV results:
kubectl logs -f job/vex-bench-vex-bench-runner -n scrum6
```

> The Job pod name is `<release>-<chart>-runner`, i.e. for release `vex-bench`
> it is `vex-bench-vex-bench-runner`. Find it with:
> `kubectl get job -n scrum6 -l app.kubernetes.io/component=runner`.

The CSV (`server,cmd,pipeline,ops`) is printed between markers so a plain
`kubectl logs` yields the data:

```
===RESULTS-START===
server,cmd,pipeline,ops
vex,SET,1,...
vex,GET,30,...
redis,...
dragonfly,...
===RESULTS-END===
```

### Namespace

Default namespace is **`scrum6`** by convention. It is not hard-coded in the
chart — pass `-n <ns>` to `helm install`. All in-namespace references
(Service DNS, RoleBinding subject) use `.Release.Namespace`.

## Configuration (`values.yaml`)

| Key | Default | Meaning |
| --- | --- | --- |
| `serverCores` | `4` | CPU requests==limits per server pod; also templated into vex `--workers` and Dragonfly `--proactor_threads`. |
| `serverMemory` | `2Gi` | Memory requests==limits per server pod. |
| `servers.<name>.enabled` | `true` | Toggle a server in/out of the sweep. |
| `servers.<name>.image` | per-server | Container image. |
| `servers.<name>.port` | per-server | Server port (vex 6380, redis/dragonfly 6379). |
| `servers.<name>.args` | per-server | Container args; literal `%CORES%` is replaced by `serverCores` at render time. |
| `loadgen.threads` | `16` | memtier `-t`. **Do not raise** (see below). |
| `loadgen.connections` | `64` | memtier `-c`. **Do not raise** (see below). |
| `loadgen.keyMaximum` | `1000000` | `--key-maximum`; preload writes this many keys. |
| `loadgen.testTime` | `30` | `--test-time` seconds per sweep cell. |
| `loadgen.image` | `redislabs/memtier_benchmark:latest` | Orchestrator/runner image. |
| `writeConfigMap` | `false` | Also persist CSV to a ConfigMap (adds SA + Role/RoleBinding). |
| `imagePullPolicy` | `IfNotPresent` | Pull policy for all pods. |

### Disabling a server

```sh
helm install vex-bench ./bench/loadtest/helm -n scrum6 \
  --set servers.dragonfly.enabled=false
```

## How the orchestrator works

Servers are benchmarked **sequentially** (the Job's shell calls
`bench_server` once per enabled server, in `values.yaml` order: vex, redis,
dragonfly). Sequential — not parallel — so each server gets the load
generator's full attention and they don't contend for node CPU.

For each server:

1. **Wait for readiness** — a tiny `memtier -n 1` probe loops (up to ~2 min)
   until the server answers.
2. **Preload** — `memtier -t 16 -c 64 --ratio 1:0 --key-pattern P:P
   --key-maximum <KMAX>` writes the full keyspace, so subsequent **GET**s hit
   real data instead of misses.
3. **Sweep** — for `cmd ∈ {SET, GET}` (ratios `1:0` / `0:1`) and
   `pipeline ∈ {1, 30}`, run `memtier --test-time <testTime>` and parse
   **the `Totals` line, column 2** (Ops/sec).
4. **Emit CSV** between `===RESULTS-START===` / `===RESULTS-END===`.

### Results: logs vs ConfigMap

By default results go to **stdout only** (read via `kubectl logs`) — zero extra
RBAC, simplest path. Set `--set writeConfigMap=true` to *also* write the CSV
into a ConfigMap (`<fullname>-results`, key `results.csv`); this creates a
ServiceAccount + Role/RoleBinding granting the Job `get/create/update/patch` on
ConfigMaps in its namespace.

> Note: `redislabs/memtier_benchmark` does **not** ship `kubectl`. With
> `writeConfigMap=true` the Job logs a WARN if `kubectl` is absent — supply a
> `loadgen.image` that bundles both `memtier_benchmark` and `kubectl` to
> actually populate the ConfigMap. The stdout CSV always works regardless.

## Validated benchmark facts (do not "tune up")

- **`-t 16 -c 64` (1024 conns) is the ceiling.** Higher (e.g. `-t 32 -c 200`)
  exhausts client ephemeral ports on the load-gen pod → `0.00` results.
- **Parse `Totals` column 2** for Ops/sec. We use full output + `grep ^Totals`,
  so the memtier `-q` quirk does not apply.
- **Preload + matching `--key-maximum` is required** so GET measures hits, not
  misses.

## Honest constraints

- **CPU quota, not pinned cores.** A k8s CPU limit of N gives the pod N cores of
  CPU *quota*, not N exclusive pinned cores — unless the kubelet `static`
  CPU-manager policy is enabled, which it usually is **not** on shared clusters.
  So this measures *"N cores of quota,"* good for **relative, release-over-release
  regression**, not for absolute scaling claims.
- **Node ceiling is 8 vCPU on this cluster**, so keep `serverCores` ≤ ~6 to
  leave headroom for the kubelet, the load-gen pod, and system daemons.
- **For the 48-core *scaling* story, use the sibling `terraform/` module**
  (big-node EC2), not this chart. This chart is the 4–8 core regression tool.

## Validate (no cluster needed)

```sh
helm lint ./bench/loadtest/helm
helm template vex-bench ./bench/loadtest/helm
```

Do **not** `helm install` against a cluster as part of CI lint — that actually
schedules pods and runs the benchmark.
