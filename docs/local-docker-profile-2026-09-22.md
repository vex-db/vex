# Local Docker profile — 2026-09-22

Completed a native ARM64 baseline and one separate CPU profile. Two workers
processed **439,082 and 443,025 ops/s** (median **441,053**) with one million
16-byte keys, 256-byte values, pipeline 1 and 80/20 GET/SET traffic.
Run p99 was 0.527/0.455 ms; peak process RSS was **458.44 MiB**; median server
cgroup CPU cost was **4.46 µs/op**. This is a shared Docker Desktop VM result.
It is not a hardware-matched comparison with the earlier AWS runs.

Both 30-second samples passed raw configuration, duration, error/miss, key
count, sampled value-length and process/hash checks. Client CPU headroom was
20–21%; server throttling remained below 0.1%, client throttling was zero,
and neither benchmark container recorded memory-limit/OOM events or swap.
VM available memory stayed above 1.4 GiB in captured post-preload snapshots.
Memory PSI increases during the two baseline intervals were negligible.
Existing development services remained running and can affect repeatability.

The native ReleaseFast binary SHA256 is
`59ff67f50b15852d69a25cec41cd8ab9bc692dd94287ec93ac2126543c0c9b9f`.
All 257 native ReleaseSafe unit tests passed. The pinned Zig compiler archive
was authenticated using the [official Zig signing key](https://ziglang.org/download/).
An initial concurrent full build exceeded its 1.5 GiB limit; compiling Vex
alone and then the tests sequentially completed without memory events.

The 24-second process recorder captured the separate 20-second workload:
3,950 samples, zero reported loss, both workers covered (1,971/1,979 samples).
Explicit sample modes classify 84.63% as kernel and 15.37% as userspace.
Fresh same-boot kernel symbols resolve names. Largest leaf costs include
`__wake_up_sync_key` (17.09%), `__local_bh_enable_ip` (4.78%),
`arch_counter_get_cntvct` (4.28%), `Worker.submitUringWrite` (4.05%) and
`nf_conntrack_tcp_packet` (3.59%). Kernel send-path ancestry includes local
reply delivery and wakeups charged to the server context; it must not be
mistaken for storage-lock contention.

Full DWARF report rendering failed without profiler OOM/limit events.
Leaf-only reporting succeeded; raw stacks and data are retained. No workload
rerun was needed. Profile throughput is excluded from the baseline.
Astra is reviewing a conservative SQ-head caching hypothesis against the
exact pinned stdlib before any source patch or speedup claim.

Portable results: [`local-docker-2026-09-22.json`](../bench/loadtest/results/local-docker-2026-09-22.json).
Raw data and build/profiling provenance:
`bench/loadtest/runs/2026-09-22-local-docker`.

This is a bounded local sanity profile on Docker Desktop’s Linux VM. It uses
only the named network and containers `vex-local-profile-20260922-*`; existing
containers are neither removed nor changed. The server image is
`vex-perf-local:arm64`; the client is the pinned memtier image recorded in
`settings.json`. No host ports, host mounts, host PID namespace, privileged
mode, sysctl changes, or claims about AWS performance are involved.

The runner creates a two-worker server on CPUs 0–1 with a 2-CPU/1 GiB limit,
the client on CPUs 2–5 with a 4-CPU/512 MiB limit, and a root sampling profiler on
CPU 6 with PERFMON, SYS_PTRACE, SYSLOG, an unconfined seccomp profile, and a
256 MiB limit. The profiler shares only the server PID namespace and samples
kernel boot/capability/uid-map/kptr/perf-paranoid data and fresh `/proc/kallsyms`
from that namespace, plus server process PID/start tick/executable and live
binary hash.

`baseline` preloads one million fixed 16-byte keys with 256-byte values, then
runs a 10-second warmup and two 30-second pipeline-1 samples at 128
connections (32 threads × 4 clients, 80/20 GET/SET). Each sample validates raw
memtier configuration and duration, zero errors/misses, DBSIZE, sampled value
lengths, process identity, cgroup CPU deltas, throttling, client thread
headroom, server RSS, Docker memory, and before/after kernel pressure, memory
events, and swap snapshots for parent review.
After baseline, the coordinator captured one 20-second workload inside a
24-second `perf record`, with the saved kernel symbols from the same VM boot.

The temporary containers and network were removed after archiving evidence;
`vex-perf-local:arm64` remains for reuse. Both AWS instances and all their
associated benchmark Kubernetes resources were also removed.

For another baseline, use a fresh output directory with the retained image:

```sh
VEX_LOCAL_BINARY_SHA256=59ff67f50b15852d69a25cec41cd8ab9bc692dd94287ec93ac2126543c0c9b9f \
  python3 bench/loadtest/scripts/profile_local_docker.py setup --output bench/loadtest/runs/local-next
python3 bench/loadtest/scripts/profile_local_docker.py baseline --output bench/loadtest/runs/local-next
```

The setup refuses to replace existing task containers. Remove only its three
named containers and network after exporting results. The baseline command
records pressure for review; it does not automatically certify VM health.

## Retrospective timing audit

A later SQ-cache confirmation exposed inconsistent memtier latency counters.
The new runtime/concurrency bounds also reject this baseline's warmup and its
separate `profile-load.json`: both report a maximum latency longer than their
entire runs. **Both uninstrumented baseline timed samples pass** the new
checks. Warmup and profile-load metrics were already excluded from the baseline.

Preserve the raw data and flag those diagnostic latency values as unreliable.
The process-scoped perf records still describe sampled CPU work during that
capture; they do not establish steady workload performance or diagnose the
timing anomaly. The subsequent same-binary SQ-cache pilot's four timed samples
and four warmups all pass the added checks. Full audit records are in
`bench/loadtest/runs/2026-09-22-sq-head-confirmation/latency-consistency-audit.json`.
