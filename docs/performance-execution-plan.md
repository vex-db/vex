# Vex performance execution plan

## Latest completed run — 2026-09-24 AWS

The isolated AWS baseline and matched CPU profiles are complete, and all
benchmark compute resources have been removed. Six-worker median: 723,770 ops/s;
two-worker median: 331,783 ops/s; pipeline 1, 128 connections, 1M 16-byte keys / 256-byte values.
Both endpoints passed 5% throughput repeatability. The nominal 72.72% worker-scaling
ratio is not physical-core efficiency: whole-node network CPU/op was similar
while softirq accounting shifted across the Vex cgroup boundary. No material
engine patch was justified or promoted from this capture.

See [AWS results](aws-request-path-2026-09-24.md) and
[strategy handoff](aws-optimization-handoff-2026-09-24.md). Earlier local-only
status below is historical and superseded by this AWS authorization and run.

Status: the 2026-09-24 request-path investigation selected a TCP topology control, but two same-binary baseline runs varied 14.25%, above the 5% prerequisite. The reviewed runner and Docker connectivity checks pass; the performance matrix awaits repeatable conditions. No server source was changed. See `docs/request-path-controls-2026-09-24.md`.

## Priority correction — 2026-09-24

The user judged the dense-table result insufficient. Close that candidate as
a proposed general replacement. Keep its evidence, but do not spend another
iteration chasing a favorable occupancy or treating requested-byte savings
as delivered product performance. Defer vector expansion as well: it has not
demonstrated a meaningful speed gain for the Redis-alternative objective.

The next objective is a material improvement in real TCP GET/SET throughput
or core scaling at pipeline 1, while preserving p99 and memory. Aim for at
least 10% repeatable server throughput improvement on the primary workload;
this is a selection target, not a predicted result. Preserve the existing
correctness and regression guards. An isolated microbenchmark is diagnostic
evidence, not the success criterion.

The strongest existing attribution is the local profile with 84.63% kernel
leaf samples, including reply delivery and wakeups. This is sampled CPU work,
not an exact wall-time breakdown or a universal bottleneck. It nevertheless
does not justify prioritizing another hash-table implementation over the
server request path. Worker-owned shards remain deferred without measured
coordination or scaling evidence.

Assign Astra the next narrow strategy task: use the existing resolved profile
and current reactor source to identify a concrete removable cost, estimate
its plausible end-to-end contribution, and specify one falsifiable test. If
the evidence cannot support a material candidate, request one targeted
attribution/scaling measurement rather than another speculative rewrite.
Grouped probing and reduced hashing are conditional options, not automatic
next implementations.

Give Luna only the selected patch with explicit file ownership, correctness
cases and rollback criteria. The coordinator first establishes repeatable
same-binary TCP controls using the repaired client, then runs the chosen
control/candidate comparison with identical resources. Stop if variability
prevents interpretation; do not adjust thresholds or cherry-pick reruns.
Only a passing server result expands across core counts and value sizes.
Local Docker results remain development evidence, not AWS or competitor
comparisons. No new run or source change is claimed by this plan correction.

## Roles

GPT-6 Astra reviews hot paths and selects a bounded hypothesis. An independent Astra review challenges attribution and experimental design. GPT-5.6 Luna implements the selected change within explicit file ownership. The coordinating agent builds, checks correctness, runs matched measurements, and obtains strong-model review before recommending promotion. A model choice does not substitute for evidence.

## Evidence and current limits

The published baseline is binary SHA256 `b6ca260e4849bd73383cfea53d5b650aaed2aa16b11feb0dc1b18de147c99db1`. The existing compact/reuse candidate is source `3077b8934a171204d74ad187a8f260710aabfe7d`, binary SHA256 `200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e`. Compact entries, direct GET copying and reusable value buffers are already implemented; this iteration validates them and tests a new receive-path hypothesis.

Matched 4 KiB measurements at one million fixed 16-byte keys, six-vCPU quota and pipeline 1 reduce mixed-phase peak RSS from 7,884.8 to 4,127.8 MiB. Mixed throughput is 271,959 versus 283,645 ops/s, a preliminary point estimate. GET, SET and mixed share allocator history within each fresh process. The separate 32-byte matched mixed check records loaded RSS 400.4 versus 199.0 MiB. The 256-byte matched check records a memory regression: 400.6 versus 458.7 MiB loaded RSS (approximately 14.5% higher). The old 598 MiB baseline cannot justify savings against the published version. This tradeoff needs a separate decision; the receive-path experiment compares against the same compact build to isolate its effect.

Lifetime cgroup counters attribute about 90% of candidate CPU to system time. This is not function-level attribution and includes preload. Four-KiB GET payload rates approach 9.5–9.7 Gbit/s. The six-vCPU quota is not six exclusive physical cores: pods are Burstable. These limits prevent declaring the engine universally network-bound.

## First implementation: receive poll-first experiment

Hypothesis: after an io_uring receive completion reports an empty socket, polling before the next receive attempt avoids unnecessary kernel work in pipeline-1 traffic.

Ownership: `src/server/event_loop.zig`, `src/server/worker.zig`, `tests/integration/recv_poll_first.py`, and an experimental note in `docs/tuning.md`.

* Opt in with `VEX_RECV_POLL_FIRST=1`; default behavior stays unchanged.
* Propagate `IORING_CQE_F_SOCK_NONEMPTY` through receive events. Initial receives remain normal; subsequent empty-socket rearms can request `IORING_RECVSEND_POLL_FIRST` in SQE `ioprio`.
* Enable only after successful existing modern-ring initialization with DEFER_TASKRUN. Plain-ring, epoll and kqueue fallbacks remain unchanged.
* Preserve buffer ownership, recv-pending state, EOF and error handling. No multishot receive, extra polling SQEs, MSG_DONTWAIT, retry loops, or unrelated refactors.

Correctness: ReleaseSafe suite plus real TCP tests with option off/on and modern-ring flags disabled. Cover idle wakeup, fragmented requests, requests exceeding a read buffer, exact reply count/order, disconnect/reconnect, concurrent varying-size values, and heap-buffer overwrite allocation failure.

## Measurement and decision

Use the same candidate binary with option off/on, fresh processes in ABBA order, persistence disabled, identical keys, quotas, clients and nodes. Primary workload is 256-byte mixed traffic at pipeline 1; confirm 32-byte mixed if promising. Use 10-second warmups and at least 60-second timed samples. Keep pipeline-8 diagnostics separate from product claims. Save every run, binary hash, kernel/backend, affinity, interval cgroup user/system/throttling counters, and RSS. Inspect network/per-core counters when available before assigning causation.

Promotion requires a repeatable throughput gain of at least 3%, exceeding observed run variation, p99 no more than 5% worse, RSS no more than 2% worse, and zero errors/misses/stalls. Confirm other small-value GET/SET and pipelined traffic do not regress materially (over 2%) before enabling by default. A short pilot can reject a change, but cannot establish broad promotion. If within noise or regressing, retain the finding and leave the option disabled or remove the experiment.

## Next decision, conditional on results

If the receive change fails, measure saturation at 32/128/512 connections and pipeline 1, then pipeline 8 at 128 connections. Obtain interval attribution and a short sampled profile before selecting the next source change. Expand accepted changes to the existing machine-size matrix and all five value sizes (32, 128, 256, 1024, 4096 bytes). Do not repeat previously unsuccessful spin/pinning/NAPI changes or start a userspace TCP stack without new evidence.

The completed receive experiment used namespace/NodePool `vex-optimize-20260922`. The saturation study uses `vex-saturation-20260922`; archive artifacts and remove its resources when the experiment ends. Other namespaces and shared EC2NodeClasses are outside scope.

## Existing correctness issue discovered during validation

The concurrent-KV reactor fast path for multi-key `DEL` only processes `args[1]`, unlike the general handler. Receive-test cleanup now deletes its isolated keys individually. This is a pre-existing compatibility issue, not evidence against or for the receive experiment, and should be fixed separately with a multi-key regression before broader Redis-compatibility claims.

## First execution status

Luna implemented the bounded receive experiment and Astra reviewed its production diff without finding an actionable correctness defect. All 257 ReleaseSafe tests passed and the ReleaseFast x86_64_v3 build succeeded. The raw-socket and compact-value integration suites passed with the option off, on, and with `VEX_URING_FLAGS=0`; all six worker startup logs confirm the intended activation state. The existing 32-client, pipeline-32, 8 KiB response stress test passed a 30-second smoke run. This is correctness evidence, not a measured speedup.

## Next bounded Luna handoff if the receive experiment is rejected

Own only a new `bench/loadtest/scripts/profile_saturation.py` and an attribution report; no `src/` edits. Reuse the existing benchmark, validation and result helpers. Do not invoke older scripts with hardcoded namespaces.

Use the current compact binary, 256-byte values, one million fixed 16-byte keys, six workers and 80/20 mixed traffic. Measure pipeline 1 at 32, 128 and 512 connections, then pipeline 8 at 128 connections. Use 10-second warmups and two 30-second samples per cell in forward/reverse order. Record exact operation count, errors/misses, p99, RSS, client CPU/thread headroom, server/client throttling and bracketed CPU time per completed operation. Validate duration, configuration, backend, binary hash and unchanged node/pod identity.

Separately capture one 20-second DEBUG PROBES run and one process-scoped 20-second perf cpu-clock profile at 99 Hz at the first saturated pipeline-1 cell. Diagnostic runs never enter accepted throughput results. Record kernel symbol visibility. If symbols are unresolved, report system-time dominated but unattributed; do not claim TCP attribution or repeatedly alter permissions/sysctls.

Stop after the matrix and one profile. Astra reviews the evidence and selects another source change only if a resolved component or explicit scaling result supports it. This handoff is planned, not yet executed.

## First throughput decision

The four 60-second ABBA runs measured 316,156 / 317,723 / 318,971 / 316,496 ops/s (off/on/on/off). Mode medians are 316,326 off versus 318,347 on, only +0.64%, below the declared 3% acceptance gate. Median run p99 is 0.627 versus 0.643 ms. All runs had zero checked errors/misses; observed server throttling was negligible and client throttling zero. This is not evidence of a meaningful improvement.

The production patch was removed, with its diff and portable measurements archived as `bench/loadtest/results/recv-poll-first-rejected.patch` and `recv-poll-first.json`. The generic receive regression is retained. The next bounded handoff above remains planned; no saturation/profile run is claimed here.

Cloud cleanup verified: namespace and NodePool `vex-optimize-20260922` deleted, both temporary EC2 instances terminated, and no associated nodes or NodeClaims remain.

Saturation study started on user instruction: Luna owns the bounded measurement runner and attribution report; Astra reviews experiment validity and interpretation. Parent owns cloud provisioning, execution, diagnostic captures and cleanup.

## Saturation-study outcome

Completed eight uninstrumented samples plus separate timing-probe and CPU-profile diagnostics. Unpipelined throughput reached 321,560 ops/s at 128 connections; 512 connections added latency without throughput. The CPU profile covered all six workers with zero reported lost samples and 94.3% kernel leaf samples, whose function names remain unavailable. Existing ring entries already batch about 11.64 commands. Astra found no supported production source change.

The next Luna handoff is a four-sample ABBA Service-ClusterIP versus direct-Pod-IP control at P1/c128. Own only an optional destination parameter in `profile_saturation.py`, a small `compare_service_path.py` wrapper, and its report. Preserve the current default destination; validate raw server/port and require the same sole ready endpoint/process throughout. Both paired comparisons must agree, median throughput difference must reach 5%, CPU/op must move consistently, and p99 may not regress more than 10%. Stop after four cells, with no source promotion implied. This is planned for a later execution; no new experiment is claimed here. Full findings are in `docs/performance-attribution-2026-09-22.md`.

Cleanup verified: the saturation namespace and NodePool were deleted, both temporary EC2 instances terminated, and no associated nodes or NodeClaims remain.

## Service-path control execution

The user authorized continuation. Run the four predeclared samples in the isolated namespace/NodePool `vex-service-path-20260922`, with raw artifacts under `bench/loadtest/runs/2026-09-22-service-path`. Luna implements the runner extension; Astra independently reviews it and the resulting evidence. The coordinating agent provisions, executes and cleans up the two temporary machines. Keep the accepted compact binary unchanged.

Completed: direct Pod access showed +0.912% and +3.899% in the two pairs, below the 5% median gate. All four samples were valid. The independent raw-artifact audit passed. Both temporary instances are terminated and their namespace, NodePool, nodes and NodeClaims are gone. See `docs/service-path-comparison-2026-09-22.md`.

## Local Docker direction

The user requested moving performance work to local Docker for now. Use a native ARM64 build of the same source and pinned compiler on Docker Desktop's Linux VM. Local measurements are development evidence, not comparable hardware measurements against the earlier AWS results. Preserve existing development containers and use separate task-owned containers/network with bounded resources.

Use two Vex workers (CPU set 0–1, 2-vCPU quota, 1 GiB), memtier on CPUs 2–5 (4-vCPU quota, 512 MiB), and an isolated profiler on CPU 6 (1-vCPU quota, 256 MiB). Keep one million 16-byte keys, 256-byte values and P1/c128 mixed traffic if memory checks remain healthy. Record memory pressure, CPU throttling and client headroom. These CPU sets do not exclude unrelated workloads or establish physical-core isolation.

The scoped profiler adds `SYSLOG`, `PERFMON` and `SYS_PTRACE`, joins only the server container's PID namespace, and uses no host mounts, host PID namespace, privileged mode or sysctl changes. Its preliminary symbol check succeeded: all 66,427 entries had nonzero addresses. After uninstrumented baseline samples, capture one separate 20-second process profile with fresh symbols and matching kernel/boot identity. Select an engine change only after Astra reviews resolved costs.

Completed two native ARM64 baseline samples: 439,082 and 443,025 ops/s, peak RSS 458.44 MiB, zero checked errors/misses, no significant benchmark throttling or memory events. All 257 native unit tests passed. One separate profile captured 3,950 samples with both workers represented and zero reported losses. Kernel leaf share is 84.63%; local reply delivery and wakeups contribute substantial sampled work. Resolved `submitUringWrite` samples led Astra to inspect repeated SQ-head acquisition in the pinned io_uring implementation. The proposed conservative cache remains an experiment, not an accepted improvement.

The local benchmark containers/network were removed and the native image retained. See `docs/local-docker-profile-2026-09-22.md` and `bench/loadtest/results/local-docker-2026-09-22.json`.

## SQ-head cache pilot and confirmation

Luna implemented a default-off conservative SQ-head cache for recv/send on the
optimized single-issuer ring. Astra reviewed the algorithm, focused fake-ring
test, benchmark runner, and raw evidence. All 258 native unit tests and socket
integration checks in all three ring/flag modes passed. The same-binary local
million-key ABBA pilot measured 440,997 off versus 466,560 on median ops/s
(+5.80%), with lower CPU/op and p99 and unchanged RSS. All validity gates passed.

The predeclared 100,000-key confirmation stopped after its first scenario,
32-byte SET-only. Recorded throughput medians fell 21.46%, but large time drift
and impossible latency values prevent assigning that difference solely to
Vex. The remaining seven scenarios were not run. Keep the candidate explicitly
experimental/default off; no source correction is supported by these results.
The runner gained a latency consistency guard, with an offline recheck of the
original pilot. Preserve both positive and inconclusive evidence in
`docs/sq-head-cache-experiment-2026-09-22.md`.

The next measurement needs stable local CPU/timing conditions, followed by the
same declared guards. Do not tune acceptance thresholds or resume a partial
matrix to obtain a positive result. No further run is scheduled. All task-owned
Docker resources are cleaned up and reproducible images retained. AWS
machine-size and cross-engine comparisons remain outside this local iteration.
