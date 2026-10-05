# Request-path investigation — 2026-09-24

The same-binary TCP control failed the repeatability prerequisite. The next
bridge-versus-loopback comparison is prepared but has not been timed. There
is no new engine improvement or topology result from this run.

Astra's independent reviews found that GET/SET already bypasses the global
command mutex. Existing resolved stacks place much of server CPU in local
TCP peer delivery; disjoint netfilter/bridge-related leaf samples provide a
plausible material budget for a topology control. These are sampled CPU
observations, not proof of removable overhead, sleeping time, or io_uring
inefficiency. Existing CQ/SQ batching and response-buffer ownership already
cover the obvious small request-path changes. No evidence-supported source
patch targeting a 10% end-to-end gain was selected.

## Same-binary controls

Two fresh native ARM64 server/client/profiler sets, the identical server
binary and repaired monotonic-clock client, one million 16-byte keys with
256-byte values, 128connections, pipeline 1, 80/20 GET/SET. Each process gets a
full preload, 10-second warmup and 60-second sample. Server: two workers and
CPU quota 2 on CPUs 0–1; client: quota 4 on CPUs 2–5. SQ-head caching is explicitly
off. No unrelated containers or host configuration were changed.

| Control | Ops/s | p99 ms | Server CPU µs/op | Client CPU µs/op |
|---|---:|---:|---:|---:|
| 1 |393,915|0.607|4.991|7.994|
| 2 |454,344|0.447|4.340|6.980|

The range divided by the median is **14.25%**, exceeding the preregistered
5% repeatability limit. Both runs pass raw configuration/duration, clock and
latency sanity, binary/process identity, key count/value length, zero errors
and misses, benchmark memory/swap and CPU-headroom checks. Server throttling
is below 0.1%; client throttling is zero; client headroom is about 21%. Server
RSS is 458.27/458.36 MiB. VM swap-in/out counters do not increase during either
complete process run, including preload.

The first run's successive 10-second throughput means are 379k, 398k, 391k, 396k,
395k and 405k. The second's are 456k, 454k, 461k, 448k, 448k and 459k. These are two
different performance levels, not evidence that trimming one startup bucket
would fix the comparison. Both server and client CPU/op improve together.
No cause has been established from these observations.

A post-run snapshot found low CPU utilization in unrelated development
containers, but it cannot explain conditions during the earlier intervals.
Host thermal telemetry was unavailable. There is no basis here for blaming
one application, clock correction, throttling or a Vex code path.

## Ready next experiment

Luna's bounded runner compares bridge TCP with loopback TCP when the client
joins only the task server's network namespace. It preserves separate CPU
quotas/cgroups and the same TCP/RESP protocol, request count and fanout. No
host networking, privileged containers, global settings or production code
changes are involved. Astra reviewed the runner's namespace and measurement
checks. Both offline self-tests pass; bridge and shared-namespace loopback
also pass 32-key Docker smoke checks for connectivity, exact values, namespace
identity, server binary identity and strict monotonic-clock metadata. These
are correctness checks, not additional performance samples. The exact experiment and causal limits are in
[the handoff](server-next-candidate-2026-09-24.md).

Use fresh server/client/profiler/preload per ABBA cell. A material signal needs
at least 10% median and both paired throughput gains, lower server and combined
accounted task CPU/op, p99 within +5%, RSS within +2%, and repeat variation at most
5%. Combined task CPU excludes some VM-wide work; it guards against shifting
CPU from server to client rather than measuring whole-machine cost.

Execution needs a repeatable control in a quieter local session or an isolated
benchmark environment. No automatic retry or adjusted threshold was used.
Both control sets were removed; existing development services remain running.

Evidence: [request-path-controls-2026-09-24.json](../bench/loadtest/results/request-path-controls-2026-09-24.json).
Raw data: `bench/loadtest/runs/2026-09-24-request-path/controls/`.
