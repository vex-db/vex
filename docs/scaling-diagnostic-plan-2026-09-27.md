# Investigating the four-to-eight-worker latency plateau

Status: all 27 diagnostic phases and both profiles completed; both EC2 instances are verified terminated. See [retained diagnostic evidence](../bench/loadtest/runs/2026-09-27-scaling-diagnostics-v2/). The expanded four-CPU comparison completed all 27 cells and both instances are verified terminated. See [retained comparison evidence](../bench/loadtest/runs/2026-09-27-expanded-four-cpu/). The original hypothesis and predeclared controls below are preserved as the experiment plan.

## What the existing data says

Vex's uncapped throughput increased from 754,651 ops/s at four workers to 1,282,942 ops/s at eight on the original host pair (about 1.70×). The confirmed rate under the 1 ms scheduled-p99 target stayed at 640k. That is a tail-latency/search-resolution result, not evidence of zero throughput scaling.

At an offered 800k, the original four-worker trial recorded 91.60 ms scheduled p99, versus 1.33 ms at eight workers. Both therefore failed the same threshold despite substantially different latency. The prior coarse search did not establish an exact ceiling between 640k and 800k.

The saved eight-worker 800k boundary trial contains a more specific lead:

| Pinned worker cores | Worker CPU seconds during snapshot bracket | Node softirq ticks per core |
|---|---:|---:|
| 0, 1, 2, 3, 4, 6 | 18.70–19.01 | 671–706 |
| 5 | 28.61 | 1,933 |
| 7 | 28.39 | 1,921 |

The same worker positions were hotter in repeated trials on both host pairs. CPU-quota throttling was zero in this boundary trial. Softirq ticks are node-wide per-core accounting, not engine-exclusive time; snapshots include launch/export overhead. The observation supports testing interference between worker placement and networking/soft-interrupt processing, but does not yet prove a NIC/IRQ routing cause or rule out contention.

[Extracted evidence](../bench/loadtest/results/scaling-4-to-8-evidence-2026-09-27.json) preserves 14 relevant phase records. The released executable remains digest-pinned; the current checkout is not assumed to be identical to that binary.

## Paired diagnostic experiment

Use one unchanged c7g.8xlarge server and one c7g.16xlarge client in ap-south-1a. Engine memory remains 4 GiB, with 100k 256-byte values, uniform 80% GET / 20% SET, 1,024 connections, and pipeline 1. Client capacity is either 24 CPUs / Go GOMAXPROCS 20 or 48 CPUs / Go GOMAXPROCS 40 on the same client host. Every phase validates the actual cgroup quota and retained 80% headroom rule.

Three rounds rotate condition order. Each listed rate runs for 90 seconds:

| Condition | Workers / CPU quota | Worker placement | Client CPUs | Offered ops/s |
|---|---:|---|---:|---|
| Four-worker control | 4 / 4 | Cores 0–3 | 48 | 640k, 720k, 800k |
| Eight-worker control | 8 / 8 | Cores 0–7 | 48 | 640k, 720k, 800k |
| Client-capacity control | 8 / 8 | Cores 0–7 | 24 | 800k |
| Unpinned control | 8 / 8 | Scheduler chooses | 48 | 800k |
| Shifted placement | 8 / 8 | Cores 8–15 | 48 | 800k |

The shifted-placement test changes only thread affinity on the dedicated benchmark process; it does not change NIC/host interrupt configuration. Verify effective affinity before measuring. If the environment cannot apply the requested affinity, stop rather than mislabel the condition.

Preserve scheduled latency, service latency, loss/errors, client dispatch delay, per-worker user/kernel CPU time, and node softirq counters. The diagnostic client also exports its existing per-connection counters and latency summaries after timing, without adding work to the request loop. Inspect connection skew alongside worker counters.

After baseline trials, separately enable the engine's existing timing probes for 20-second four/eight-worker runs. Compare operation counts and time in storage/locks, response handling, and event-loop waits. Instrumented throughput is excluded from performance claims. A placement improvement would support a scheduling/network-work interference hypothesis; a client-capacity improvement would implicate the driver; elevated lock costs would motivate a targeted engine investigation. No production fix is selected in advance.

## Stronger four-CPU comparison

After interpreting the diagnostics, use the same pinned Vex/Redis/Dragonfly versions for a separate four-CPU comparison with 4 GiB per engine, one million keys (10× the earlier scaling dataset), and a 48-CPU client:

- Uniform 256-byte values.
- Uniform mixed sizes: 32/256/1,024/4,096 bytes at 50/30/15/5% weights.
- The same mixed sizes with a Gaussian hot-key distribution.

Run three 120-second measurements per engine/profile, rotating engine order and recreating/preloading the server for each cell. Use uncapped memtier, 1,024 connections, and pipeline 1. Record throughput, service p99/p99.9, client headroom, misses/errors, memory, throttling, and host/process identity. These are closed-loop workload comparisons; their p99 values do not establish the open-arrival 1 ms SLO. Do not mix the two interpretations in marketing claims.

The four-CPU scope tests the current proposed marketing claim. This suite does not claim to resolve 16-CPU limits or Dragonfly's separate 16-thread memory-setting incompatibility.

## Recorded commands

The diagnostic command below records the successful resume after credential expiry; that run is now complete and cleaned up. Do not rerun it against the completed output. For a fresh experiment, choose unused output and namespace names and omit `--resume`. Resume is only for an interrupted run whose original hosts remain alive with unchanged boot IDs.

The driver was built at `/private/tmp/vex-openloop-diagnostics-arm64` from `bench/loadtest/openloop`. The runner attempts cleanup in a `finally` block, but cleanup requires valid AWS authorization.

```sh
python3 -u bench/loadtest/scripts/scaling_diagnostics_aws.py \
  --source bench/loadtest/runs/2026-09-26-realistic-three-engine-v2 \
  --output bench/loadtest/runs/2026-09-27-scaling-diagnostics-v2 \
  --namespace vex-scaling-20260927-diagnostics-v2 \
  --binary /private/tmp/vex-openloop-diagnostics-arm64 \
  --suite diagnostics --seconds 90 --rounds 3 --resume

python3 -u bench/loadtest/scripts/scaling_diagnostics_aws.py \
  --source bench/loadtest/runs/2026-09-26-realistic-three-engine-v2 \
  --output bench/loadtest/runs/2026-09-27-expanded-four-cpu \
  --namespace vex-scaling-20260927-expanded \
  --binary /private/tmp/vex-openloop-diagnostics-arm64 \
  --suite workloads --seconds 120 --rounds 3 --keys 1000000
```

Verify EC2 termination independently after each suite. Do not run the expanded comparison until the diagnostic findings have been assessed; if a confirmed engine fix is needed, pin and identify that candidate explicitly rather than silently replacing the tested build.

Both instrumented profiles completed and were exported. The saved watcher is retained at `bench/loadtest/runs/2026-09-27-scaling-diagnostics-v2/source/capture_diagnostic_perf.py`; it is no longer running.
