# Kernel-bypass I/O for vex (AF_XDP) — design sketch

> Status: **exploratory**. This documents what a from-the-start kernel-bypass
> I/O layer would look like, what it buys, and what it costs — so the decision
> to build it (or not) is made with eyes open.

## Why consider it

Measured (2026-06): under unpipelined load, **~88% of every core is in the
kernel** (TCP, syscalls, softirq); vex's own engine is the ~12% remainder. Both
vex and Dragonfly converge to ~2.7–3.0M ops/s/instance unpipelined on c6gn —
because they share the *same kernel network stack*. No CPU-side optimization we
tried (spin, pinning, NAPI busy-poll, lock removal) moved that ceiling; only RFS
(which redistributes the kernel's softirq work) helped, and only at high cores.

**The only way to break the 88% ceiling is to stop going through the kernel
network stack.** That is what AF_XDP (or DPDK) does.

## What AF_XDP gives — and what it does NOT

AF_XDP is a Linux socket family that hands userspace **raw L2 frames** directly
from the NIC via mmap'd rings (UMEM + FILL/RX/TX/COMPLETION), with an eBPF XDP
program steering chosen packets to the socket (XSK). In **zero-copy mode** the
NIC DMAs straight into/out of userspace memory.

It removes, for the data path:
- **per-packet syscalls** (RX/TX is ring polling in userspace),
- **softirq scheduling** (the driver poll runs in the app's context),
- **skb alloc/free** and the generic netstack overhead,
- **context switches** (busy-poll the RX ring — what NAPI tried, but here with
  no kernel TCP behind it).

It does **NOT** give you TCP. AF_XDP delivers Ethernet frames, not byte streams.

## The catch: you must own a TCP/IP stack in userspace

To keep speaking RESP-over-TCP to unmodified clients, vex would have to
implement (or embed) a userspace stack: Ethernet/ARP, IPv4 (+checksums,
fragmentation), and **TCP** — handshake, sequence/ack, retransmission + RTO,
windowing, congestion control, RST/FIN, TIME_WAIT, SACK, PMTU. This is the
enormous, high-risk part, and it is exactly why **Redis and Dragonfly do NOT do
this** — the kernel TCP stack is battle-tested; replacing it is a permanent
correctness + interop liability (clients, proxies, load balancers, NAT).

Options for the stack:
- **Embed an existing one** — F-Stack (DPDK + FreeBSD stack), Seastar's stack
  (C++), lwIP (simple but not high-perf). None integrate cleanly into a Zig
  codebase; most assume DPDK, not AF_XDP.
- **Write a minimal TCP** tuned for the KV request/response pattern (short
  messages, mostly in-order). Tractable for the happy path; the long tail
  (retransmit/reordering/window/RTO/interop) is where months go.

## What changes in vex

- **The whole I/O layer is replaced.** `event_loop.zig` / `tcp.zig` /
  io_uring+epoll backends → an AF_XDP + userspace-TCP layer. The reactor model
  maps *well*: one XSK per NIC RX queue, pinned to a core → true shared-nothing
  (queue = core = worker = the "1 door, 1 chef" alignment, for free).
- **Connections become 4-tuples, not fds.** vex tracks conns by fd today; it
  would track them in a userspace connection table keyed by (src ip,port,dst
  ip,port). The RESP parser sits on top of the reassembled TCP stream.
- **TLS** must run over the userspace stream (OpenSSL BIO over our stream
  instead of a socket fd). Doable, more plumbing.
- **Control plane stays on the kernel** (recommended hybrid): replication is a
  TCP *client*, cluster gossip, admin/CONFIG — keep these on normal sockets so
  the userspace stack only handles the simple client request/response data
  plane. Limits scope a lot.
- **Deployment stops being "just run the binary."** Needs: a NIC/driver with
  AF_XDP support (AWS ENA supports XDP; zero-copy AF_XDP mode is limited — copy
  mode still helps but less), `CAP_NET_RAW`/privilege, dedicated cores
  busy-polling (so it's a poor fit for shared/bursty hosts — the same trade as
  NAPI/SQPOLL, but permanent), and ethtool queue/steering setup.

## Phasing (if we ever build it)

0. **Prototype** — AF_XDP RX/TX raw frames on one queue, echo. Measure raw
   packet rate (no TCP) → bounds the achievable ceiling. Cheap, decisive.
1. **Minimal userspace TCP** for one RESP connection (handshake, in-order data,
   basic retransmit).
2. **Connection table + per-core XSK**, wire to the reactor + RESP parser;
   hybrid kernel control-plane.
3. **Robustness** (RTO/window/reordering/edge cases), TLS, interop hardening.
   Phase 3 is the long tail and the real cost.

## Honest verdict

- AF_XDP is the **correct answer to "win unpipelined networked throughput by a
  large margin"** — it's the only lever that removes the measured 88% kernel
  cost, and architecting around it *from the start* is far easier than
  retrofitting (the I/O model, buffer ownership, and connection lifecycle all
  change). It would be a genuine moat: Dragonfly stays on the kernel stack.
- But it commits vex to **owning a TCP stack forever** — a large, permanent
  liability for a single benchmark axis, and it doubles down on the *networking*
  arms race, which is the opposite of vex's differentiated bet (embedded /
  fused vector→graph→KV, where there is **no socket to bypass**).

**Recommendation:** keep the kernel io_uring path as the **default** (portable,
robust, and already at parity-or-ahead of Dragonfly). Treat AF_XDP as a **later,
optional "turbo" deployment mode** for users who need maximum networked
throughput on dedicated hardware — pursued only if the networked-throughput
crown becomes a strategic priority. Do **Phase 0** first (a few days) to get the
real number before committing to Phases 1–3. The foundational bet belongs on the
off-TCP path, not on out-engineering the kernel.
