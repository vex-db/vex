# Performance tuning

vex is fast out of the box; these knobs are for squeezing the last bit on
many-core / network-optimized hardware. **Every number below was measured**
(AWS, 2026-06) — including the ones that turned out to be a wash, so you know
what *not* to bother with.

## TL;DR

| Lever | Default | Verdict | Use it when |
|---|---|---|---|
| **RSS queue count** (hardware) | — | **biggest lever** | unpipelined throughput matters — pick a network-optimized instance |
| **RFS** (kernel sysctl) | off | **+26–39% @ many cores** | high core count + few NIC queues |
| `VEX_PIN_WORKERS` | **on** | correct, ~neutral here | always on; helps under real multi-tenant load |
| `VEX_URING_FLAGS` | **on** | wash at saturation | leave on (it's the recommended io_uring config) |
| `VEX_NAPI_BUSY_POLL_US` | off | cuts ctx-switches, **no throughput gain** | latency-sensitive + CPU to burn |
| `VEX_POLL_SPIN_US` | off | no gain, burns CPU | experimental |

**The honest headline:** for unpipelined small-op throughput, vex is
*kernel-network-bound* — ~88% of CPU is the kernel TCP/softirq path, not vex. So
the two things that actually move the number are **hardware (RSS queues)** and
**RFS** (which redistributes the kernel's softirq work). The CPU-side knobs are
mostly neutral; they're here for completeness and edge cases.

## Hardware: RSS queues are the unpipelined ceiling

Under unpipelined load the bottleneck is how fast packets get *in*, which is
gated by the NIC's RSS (receive-side-scaling) queue count — not vex. Measured,
same vex binary, 64 cores, identical load:

| instance | RSS queues | unpipelined throughput |
|---|---|---|
| c5a.16xlarge | 8 | ~1× |
| c6in.16xlarge | 16 | **~2.3×** |
| c6gn.16xlarge | 32 | highest |

Check yours with `ethtool -l <nic>` (the `Combined` line). **If unpipelined
throughput matters, pick a network-optimized instance** (`c6gn`, `c6in`, `c5n`).
And **always saturate with ≥2 load-generator boxes** when benchmarking — a single
client caps ~1–3M ops/s and will make every engine look tied.

## RFS — the one big software lever (kernel sysctl, not a vex flag)

Receive Flow Steering co-locates each connection's RX softirq with the core
running its worker, instead of piling all softirq on the few RSS-queue cores. On
many-core boxes with few queues this **recovered +26% @ 32c and +39% @ 48c**
(it erases the high-core "cliff"). It *hurts* at low core counts (−17% @ 8c), so
only enable it on many-core deployments.

```sh
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
for q in /sys/class/net/<nic>/queues/rx-*; do
  echo 4096 > "$q/rps_flow_cnt"
  echo ffffffff,ffffffff > "$q/rps_cpus"   # candidate CPUs (mask for your core count)
done
```

## vex env-var knobs (Linux io_uring path)

### `VEX_PIN_WORKERS` — pin workers to cores (default: **on**)
Pins worker *i* to the *i*-th allowed CPU (respects `taskset`/cgroup cpusets),
which also clusters consecutive workers into the same cache cluster. Eliminates
CPU migrations (measured 172K–495K/5s → 0). Throughput-neutral on a dedicated
box, but real under multi-tenant load (warm caches, stable NIC↔worker affinity).
Set `VEX_PIN_WORKERS=0` to disable.

### `VEX_URING_FLAGS` — io_uring one-thread-per-ring flags (default: **on**)
Creates each worker's ring with `SINGLE_ISSUER | DEFER_TASKRUN | COOP_TASKRUN`
(completion task-work runs on the worker's own `io_uring_enter` instead of via
IPI/softirq). It's the recommended config for a reactor that owns one ring per
thread. Requires **Linux ≥ 6.1**; auto-falls-back to a plain ring, then epoll.
A/B at saturation measured it a **wash** (±4%, within noise) — so it doesn't
hurt, and it's correct. Set `VEX_URING_FLAGS=0` to force a plain ring.

### Receive poll-first experiment (2026-09-22, rejected)

An opt-in receive `POLL_FIRST` experiment passed correctness checks but measured
only +0.6% median throughput on 256-byte mixed pipeline-1 traffic in a same-binary
ABBA pilot, below the declared 3% gate. Median run p99 was 0.643 ms versus
0.627 ms. The production patch and toggle were removed. The generic receive
regression remains; the rejected patch, provenance and results are preserved in
`bench/loadtest/results/recv-poll-first*`. This applies to the tested workload,
not every possible use of the kernel feature.

### `VEX_NAPI_BUSY_POLL_US` — io_uring NAPI busy-poll (default: **0 / off**)
When > 0, the worker busy-polls the NIC's receive queue **inline** for up to N
microseconds before parking, processing the network softirq in its own context.
Requires **Linux ≥ 6.9**. Measured: **cut context-switches up to 23×** but gave
**no throughput gain** (−6% at 48c) while ~doubling CPU — because the bottleneck
isn't the wakeup, it's the kernel stack. It's a **latency** tool: useful if you
have spare cores and want lower tail latency, not for throughput. Try
`VEX_NAPI_BUSY_POLL_US=50`.

### `VEX_POLL_SPIN_US` / `VEX_POLL_SPIN_ADAPTIVE` — spin-before-park (default: **0 / off**, adaptive on)
Spin-peek the completion queue for up to N µs before parking. Measured: no
throughput gain, burns CPU (it spins an *empty* ring — completions are stuck in
softirq, which `VEX_NAPI_BUSY_POLL_US` addresses instead). Experimental; left
off. `VEX_POLL_SPIN_ADAPTIVE=0` makes it spin every tick instead of only when
recently busy.

### `VEX_SQ_HEAD_CACHE` — deferred-ring SQ-head cache (default: **0 / off**)
On the optimized `SINGLE_ISSUER | DEFER_TASKRUN | COOP_TASKRUN` io_uring path,
`VEX_SQ_HEAD_CACHE=1` caches the submission-queue head for recv/send batches and
refreshes it only on first use or an apparent full queue. Poll/AOF submissions,
plain rings, and fallback backends keep the standard path. This is experimental;
the native ARM64 Docker pilot measured +5.8% median throughput on one-million-key,
256-byte mixed pipeline-1 traffic with unchanged RSS. The write-only confirmation
stopped with failed guards, substantial variation and invalid latency telemetry;
the result is inconclusive and the flag remains off. See
`docs/sq-head-cache-experiment-2026-09-22.md` for limits and the exact protocol.

### Indirect reactor entries experiment (2026-09-22, rejected)

The prototype stored pointers in the reactor's striped hash tables and
allocated an Entry only for each live key. It retained the existing locks,
allocator, 32-byte inline values and reusable larger-value buffers. Native
allocation diagnostics at one million keys reduced loaded RSS by 35.3 MiB
for 256-byte values and 35.5 MiB for 32-byte values. All 262 native tests pass
with either prototype build setting.

The mixed-P1 server pilot failed: median throughput was 410,154 versus 453,091
ops/s, and both pairs failed CPU/op and p99 guards despite saving 35.3 MiB.
RSS after `FLUSHDB` was also substantially higher despite zero tracked live
allocations. The production patch and build option were removed; the exact
patch is archived as `bench/loadtest/results/entry-indirection-rejected.patch`.
Within-run drift limits causal precision, so this is a failed local screen,
not a universal speed penalty. See `docs/memory-io-followup-2026-09-22.md`.

## What we tried that did NOT help (so you don't)

Removing the stripe read-lock, reducing worker count, app-level spin and the
io_uring ring flags did not establish repeatable throughput gains in the
tested unpipelined workloads. The native local profile attributes substantial
work to the kernel, including local reply delivery and client wakeups. That
does not prove that kernel bypass is the only remaining improvement.
Worker-owned data shards and [AF_XDP](af-xdp-design.md) remain architectural
hypotheses requiring their own evidence; neither is enabled by these results.
For the full investigation, see the engine architecture notes and current
memory/I/O follow-up report.
