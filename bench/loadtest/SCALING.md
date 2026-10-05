# CPU scaling with scheduled arrivals

For the follow-up worker-placement diagnostics and longer four-CPU comparison,
see the [retained diagnostic evidence](runs/2026-09-27-scaling-diagnostics-v2/) and
[experiment plan](../../docs/scaling-diagnostic-plan-2026-09-27.md).

This experiment finds workload capacity under a 1 ms p99 target. It does not reuse
the old 128k rate cap as a capacity estimate. A fixed-size, uniform 80% GET / 20%
SET workload isolates CPU scaling: 100,000 keys, 256-byte values, no TTL,
1,024 connections, one outstanding request per connection. This changes the
connection count from the earlier 128-connection realistic pilot.

The server and client each use a dedicated c7g.8xlarge in the same availability
zone. Server CPU quota and worker/I/O-thread count vary together through
1, 2, 4, 8, and 16. Server memory stays at 4 GiB. The client receives 24 CPUs and
4 GiB; Go uses 20 execution threads. This measures CPU allocation scaling on one
machine, not price scaling across EC2 instance sizes or exclusive core pinning.
Server CFS throttling is retained as a measurement, not rejected.

## Arrival model and accounting

`openloop/main.go` uses four independent schedulers on dedicated OS threads and absolute monotonic deadlines. Their busy-wait CPU cost is included in client accounting.
Every request keeps its planned arrival time; responses never move the schedule.
Requests enter bounded queues, one per connection. Workers perform one command
at a time. Full queues cause explicit drops. Offered arrivals, enqueued, sent,
completed inside the arrival window, completed during the one-second drain,
dropped, errors, and uncompleted requests are reported separately.

Scheduled latency runs from intended arrival to response completion, including
client queueing. Service latency starts at socket write. Dispatch delay measures
how late the independent scheduler was, before queue admission. Ten-microsecond
histogram buckets report upper bounds; values of at least 100 ms use an overflow
bucket. The 1 ms violation counter uses exact durations, not histogram rounding.
Missing GETs, unexpected values, and non-OK SET replies are errors. Connections
are established and commands pre-encoded before the measurement clock starts.
There are no retries or reconnections.

This is deterministic offered traffic with bounded admission, not Poisson
arrivals or unlimited network pipelining. Overload may queue at the client;
that waiting is deliberately included in the latency target.

## Search and acceptance

After preload and warmup, start at 32k offered ops/s (or a prior confirmed rate as an explicit search hint) and double until failure or
the configured safety ceiling. Refine a valid pass/fail bracket twice, then run
three longer confirmations at the highest exploratory passing rate and one longer upper-boundary check. An uncapped, 10-second memtier cross-check uses the same connection count and data shape after the open-arrival phases. Preserve
failed trials and inconclusive client-limited trials. A rate passes only with:

- At most 1% of all offered requests exceeding 1 ms or remaining uncompleted.
- Zero errors and zero uncompleted requests after the bounded drain.
- At least 99% completed within the arrival window.
- Client dispatch p99 no greater than 0.25 ms, average CPU below 80% of its quota,
  and cgroup throttling below 1% of elapsed time.

If confirmations fail, preserve those failures, step down to a previously passing rate (or reduce by 20%), and repeat all three confirmations, with at most two step-downs. Report no confirmed capacity if none of those sets passes. Reaching the rate ceiling yields a lower bound, not a
scaling limit. A client-limited upper rate is refined toward the last passing rate. If a valid failing boundary cannot be found, report a client-limited lower bound and retain the inconclusive trials.

Long-running commands are detached on the AWS client and persist stdout, stderr, and exit status before collection. A lost launch acknowledgment does not trigger a duplicate trial.

Each engine/CPU cell uses a fresh server, digest-pinned images and checked binary
hashes. Record node/pod/process identities, cgroup limits and counters, memory
checks, Redis I/O-thread config, Vex disabled probes, and eviction counters.
CPU snapshots bracket subprocess execution and include startup/export overhead. For the client gate, total captured CPU seconds are divided by the arrival-window duration: this is a conservative upper bound on CPU usage inside that window. The independent dispatch-delay gate also checks client timing directly.

## Run

Build with `GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build` in `openloop/`.
`go test -race ./...` exercises deliberate server stalls, queue overflow,
unchanged offered-arrival accounting, and latency including client waiting.

Use `scripts/scaling_aws.py --help` for the AWS runner. Its source run is the saved
three-engine realistic v2 run; it clones only that run's dedicated manifests and
creates a new `vex-scaling-*` namespace and NodePools. Cleanup is attempted in a
`finally` block. Verify EC2 termination independently after completion or failure.

## Observed limits in the September 26–27 run

At 16 workers, Dragonfly rejected the unchanged `--maxmemory=3gb` setting, reporting that 4.00 GiB was required. The container limit was 4 GiB; this was an explicit startup exit, not an OOM kill. A future higher-memory comparison should first check that every intended worker/memory configuration starts, then use the same memory budget across engines.

High-rate 16-CPU trials also exceeded the client headroom gate. Such trials do not establish database capacity. Resolving those upper limits requires a larger or distributed load generator, with scheduled-arrival latency and client validation retained. Credential interruption and startup failure required three host pairs; the results report records provenance and repeated overlap points.
