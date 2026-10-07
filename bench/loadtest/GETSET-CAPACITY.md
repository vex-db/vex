# GET/SET multicore capacity comparison

The September 29 sweep compares a frozen Vex development binary, Redis 8.10.1,
and Dragonfly 2.0.0 on the same isolated AWS server. It searches for the fastest
confirmed GET and SET throughput with p99 service latency at or below 5 ms.
This is a closed-loop concurrency sweep, not an offered-rate or production SLA test.

- CPU quotas: 1, 2, 4, 8, 16; server memory limit: 8 GiB.
- Server: c6gn.8xlarge; separate client: c7g.16xlarge, 48-CPU quota.
- One million preloaded keys, 16-byte keys, 256-byte values, uniform access.
- Separate 100% GET and 100% SET workloads; pipeline 1; persistence disabled.
- Vex workers, Redis I/O threads, and Dragonfly proactors equal CPU quota.
- Vex workers unpinned, 256 sorted-set partitions, adaptive ownership disabled.
- Same digest-pinned binaries and unchanged hosts throughout the run.

Search uses 32 memtier threads with 1, 4, 16, 64, and 120 clients per thread,
15 seconds per setting. The first setting above 5 ms p99 ends the search.
A client-limited or invalid setting also stops the search and is excluded.
The 3,840-connection ceiling stays below Vex’s fixed 4,096-entry descriptor table. The fastest
passing setting is repeated three times for 60 seconds. If any repetition fails,
the next passing search setting is tried. All confirmed values remain lower
bounds; a tested SLO boundary does not prove the exact maximum.

Validity requires no errors or GET misses, unchanged dataset size and sampled
value lengths, no eviction/OOM/swap/restarts, client thread CPU averages below
0.9 cores, aggregate client CPU below 38.4 cores, client throttling below 1% of
measurement duration, and no increase in ENA network allowance counters.
CPU cost per operation includes the snapshot interval and is diagnostic.

Quotas are not exclusive physical-core assignments and Redis I/O threads do not
make all command execution parallel. Describe the comparison accordingly.

## Finer load search

Add `--refine` for total connection counts of 32, 64, 128, 256, 384, 512,
768, 1,024, 1,536, 2,048, 3,072, and 3,840. The 32-connection point uses
32 client threads; all larger points use 64 threads. The baseline's sixteen-CPU
Vex SET search saturated one of its 32 client threads, so the finer sweep spreads
the same connection counts across more client threads without relaxing any CPU
validity gate. All engines use this same rule. Run the full comparison on a
fresh isolated pair; do not splice measurements from the two sweeps together.

Selection still maximizes confirmed throughput below 5 ms p99, rather than
forcing latency to reach 5 ms. A throughput plateau can occur well below the
latency limit. The refined grid is denser, not proof of an exact global maximum.
It retains three 60-second confirmations, a 3,840-connection ceiling, automatic
cleanup, and a bounded lifetime (7.5-hour controller deadline; 8-hour nodes).

Run from the repository root with a fresh output directory and namespace:

```sh
python3 bench/loadtest/scripts/test_getset_capacity.py
python3 -u bench/loadtest/scripts/compare_getset_capacity_aws.py \
  --output bench/loadtest/runs/2026-09-29-getset-capacity \
  --namespace vex-scaling-20260929-getset \
  --binary bench/loadtest/runs/2026-09-28-adaptive-three-aws/frozen/static-after/bin/vex
```

`--prepare-only` freezes the protocol and manifests without contacting AWS.
Per-phase raw output, command, stderr, CPU/memory snapshots, and per-cell results
are append-only. The runner requests resource deletion in `finally`, even on
failure. Verify EC2 termination separately. Baseline node lifetime is six hours;
the finer sweep allows eight hours.
Only complete, valid cells should feed the website. Never replace missing or
client-limited values with interpolated capacity claims.

If the controller exits after a credential interruption, refresh credentials
and add `--resume` to the same command. Do not start a second controller while
the original is running. Resume verifies the original node identities and
frozen binary, retains completed cells, and restarts an interrupted cell with
a fresh client. It refuses replaced hosts or resources already being deleted,
and retains the original run deadline.
