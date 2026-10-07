# Next server-throughput discriminator — 2026-09-24

**Completed:** the AWS controls and both diagnostic profiles are archived in
[the AWS report](aws-request-path-2026-09-24.md). All temporary compute was
removed. The protocol below is historical; no engine candidate was promoted.

## Active execution: remote-client AWS control and profile

The user subsequently authorized AWS. The local same-binary controls failed
the stability gate; the local bridge/loopback matrix below was **not run**.
Its protocol is retained as an unexecuted plan, not evidence. The active next
step is the following remote-client TCP baseline and one profile. Provisioning
and cleanup remain with the coordinator; this document authorizes no engine
change or additional benchmark sweep.

Use isolated, tainted on-demand ARM64 nodes in the same availability zone:
`c7g.2xlarge` for Vex and `c7g.8xlarge` for the client. Start with **six Vex
workers and a 6-CPU server quota**, and a **24-CPU client quota**. The initial c7g.4xlarge / 12-CPU client warmup failed the20%headroom
guard (15.86%remaining); its60-second control was not run. This capacity
revision precedes the accepted controls. Connect to the
server Pod IP directly from the separate client node. Verify node identities,
same-AZ placement, image digests, exact live native binary hashes, repaired
client clock metadata, actual CPU affinity/cpusets and quota. A six-CPU quota
does not reserve two particular server-node CPUs for IRQs or system services.

Run two fresh identical controls, each with the same preload, 10-second
warmup and 60-second measured load: 1M 16-byte keys, 256-byte values, 80:20
GET:SET, pipeline 1 and 128 connections. Keep experimental engine switches
off. Apply the established correctness, finite latency/concurrency bounds,
process/hash, CPU, client headroom, throttling, RSS, memory events and swap
checks. Stop if invalid/constrained or if throughput range divided by median
exceeds 5%. Record whole-node CPU and softirq counter brackets as context:
remote-network IRQ/ksoftirqd work may be outside the server process/cgroup.
These counters are not an exact per-request allocation of host work.

Only after valid controls, collect one separate 20-second workload inside the
existing longer perf recording window. Keep DEBUG PROBES off. Record all six
worker TIDs and the other Vex threads, per-thread CPU deltas, sample coverage,
loss and explicit user/kernel sample modes. Resolve kernel symbols using the
same node and boot; record resolution failures instead of assigning them to
TCP or locks. Use the existing scoped profiler capabilities, not host mounts,
host PID, global sysctl changes or privileged mode. Profile throughput is
excluded from the uninstrumented baseline. No concurrent builds or unrelated
benchmark workloads on either task node.

This six-worker point does not establish multicore scaling or support a
comparison with local Docker. Use the profile to select one demonstrated,
potentially removable request-path cost. If attribution and per-thread CPU
instead leave multicore efficiency as the unresolved decision, the bounded
follow-up is one lower endpoint: two fresh **2-worker/2-CPU** controls with
everything else matched, including the 24-CPU client and stability checks.
Report `Q6 / (3 * Q2)`; 0.80 efficiency is a goal, not a prediction. Do not
automatically run a 2/4/6 sweep. Prefer a concrete candidate experiment if the
profile already justifies one. No production default changes follow from a
baseline, profile or scaling ratio alone.

## Retained, unexecuted local topology plan

Choose one unchanged-engine TCP control before another request-path patch:
dedicated Docker bridge versus loopback TCP in a network namespace shared only
by the task's server and client. Target a material effect, at least 10% actual
pipeline-1 throughput. This is a local topology experiment, not an engine
optimization or a deployment recommendation.

## Evidence and limits

The resolved local profile contains 3,950 samples covering both workers.
Kernel mode accounts for 84.63%; 70.35% of all samples have `io_send` ancestry.
The prominent `__wake_up_sync_key` samples run through local TCP receive and
peer wakeup, not demonstrated Vex map-lock contention. See
`bench/loadtest/runs/2026-09-22-local-docker/strategy-analysis-notes.md` and
`perf-kernel-record-analysis-verified.json` in that directory. Inclusive stack
shares overlap and do not predict removable CPU or throughput. The capture's
old memtier latency telemetry is unreliable; its CPU stacks remain attribution
evidence, not a valid steady-state throughput measurement.

Current code already omits duplicate readiness polling for io_uring receive
connections (`src/server/event_loop.zig:447`), queues receive/send SQEs for the
next combined submit/wait (`src/server/worker.zig:1160`, `:1215`), and transfers
large response-buffer ownership without copying (`:1134`). TCP_NODELAY is set
on accepted sockets (`src/server/tcp.zig:1189`). These are not new candidates.
A direct-send or multishot-receive rewrite is not justified by inclusive
`io_send` samples: those stacks include ordinary TCP delivery after submission.
The earlier TCP/UDS comparison stopped for repeatability failure and cannot
establish either an engine win or a scaling curve.

## Bounded implementation handoff

Luna owns a new small runner using the existing local Docker measurement and
validation helpers. No changes to `src`, allocator, ring flags, SQ-head-cache
state, worker pinning, or listeners. Preserve unrelated containers.

- Use one unchanged native ARM64 binary and matching image/configuration
  throughout A/B/B/A. Create fresh server, client and profiler containers and
  preload the same dataset in every cell, avoiding inherited allocator history.
  Record image digest, live binary hash, PID/start time, startup environment
  and server network namespace identity. Require stable server identity within
  each cell; different cells have different server processes.
- A: server and client have distinct network namespaces on the task-owned
  bridge; memtier targets the server's bridge IP directly. B: the client uses
  `--network container:<task-server>` and targets `127.0.0.1` at the same TCP
  listener. The B client has a separate PID namespace and cgroup. Do not use
  host networking, host PID, host mounts, privileged mode or global changes.
- Keep server at two workers, CPUs 0–1, quota 2 and memory 1 GiB; client CPUs
  2–5, quota 4 and memory 512 MiB. These are VM CPU restrictions, not exclusive
  physical cores. Keep profiler configuration identical between fresh cells.
  Persist inspected namespace modes and resolved destinations so mislabeled
  A/B cells fail validation.
- Fixed workload: 1M owned 16-byte keys, 256-byte values, 80:20 GET:SET,
  pipeline 1, 128 connections; repaired monotonic-clock memtier throughout.
  Each cell gets the same 10-second warmup and 60-second measured interval.
  Stop after four cells; no worker-count, value-size or transport sweep.
- Reuse raw protocol/configuration, clock/latency bounds, key/value checks,
  process identity, client headroom, throttling, RSS, memory events, swap and
  VM pressure checks. Snapshot server and client CPU counters around each
  measured interval; report their separate and summed CPU seconds per completed
  operation. Do not divide warmup CPU by measured operations. Preserve raw
  counter brackets and interval durations. Task CPU totals do not include all
  VM softirq work or unrelated services; they are a cost-transfer guard, not
  whole-machine energy or CPU accounting.

## Predeclared decision

Stop as inconclusive on any invalid cell, material resource constraint, or
within-mode throughput range divided by mode median above 5%. Do not rerun
until favorable. Report both adjacent pairs (B1/A1 and B2/A2), mode medians,
and raw CPU/p99/RSS values.

A material topology signal requires median throughput gain at least 10% and
both paired gains at least 10%, lower server CPU/op and lower summed task
CPU/op in both pairs, no more than 5% p99 regression and no more than 2% server
RSS increase. A lower server CPU figure with higher total task CPU/op is not
an efficiency win. Classify smaller or mixed effects explicitly; do not
silently weaken the gate.

Even a passing result changes no engine default. It establishes that this
local topology materially limits the current TCP measurement and identifies
a cleaner local control for subsequent engine work. It does not isolate
bridge, conntrack, namespace, routing or wakeup costs individually, nor prove
remote-client benefits. A valid null result rules out this tested topology
change as the requested material improvement; it does not demonstrate a Vex
lock bottleneck or authorize a speculative I/O rewrite. Archive all four cells,
summarize this one decision, and remove only the task's resources.
