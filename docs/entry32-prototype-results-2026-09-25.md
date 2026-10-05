# Entry32 prototype results — 2026-09-25

The isolated 32-byte-entry prototype meets the **memory portion** of the target at every sampled point in the 600k–800k and 1.2M–1.6M ranges for plain 256-byte values. Throughput is unverified, so this is **not promoted to production** and does not establish that the previous Vex speed lead survives.

## Matched server memory

Native ARM64 local Docker, 6 CPU/4 GiB container limits, identical 16-byte keys, 256-byte values, no TTL, no persistence, and noeviction. The same growth sequence, client, spot checks and independent process RSS sampling were used for all engines. Each case used a fresh process; these are single observations, not statistical estimates or throughput benchmarks.

| Keys | Original Vex | Dragonfly | Entry32 Vex | Redis |
|---:|---:|---:|---:|---:|
| 600k | 262.2 MiB | 184.1 MiB | **230.1 MiB** | 236.6 MiB |
| 750k | 306.1 MiB | 238.9 MiB | **273.7 MiB** | 283.5 MiB |
| 800k | 321.0 MiB | 249.6 MiB | **288.5 MiB** | 301.1 MiB |
| 1.2M | 517.9 MiB | 339.4 MiB | **453.6 MiB** | 457.3 MiB |
| 1.5M | 605.4 MiB | 448.5 MiB | **540.9 MiB** | 561.4 MiB |
| 1.6M | 634.6 MiB | 470.1 MiB | **569.9 MiB** | 588.4 MiB |

The 1.2M margin below Redis is only 3.7 MiB. Results cover sampled points, not proof of every intervening cardinality. All 12 growth observations, including failures of the target, are retained in the [results JSON](../bench/loadtest/results/entry32-prototype-2026-09-25.json).

The resize region is still expensive: at 850k/900k/1M, Entry32 uses 340.6/366.4/395.5 MiB versus Redis 318.7/336.4/371.7 MiB. The prototype does not erase the step function.

## What changed and what it costs

The striped StringHashMap, allocator, hashing, probing and locks stay the same. The table entry shrinks from 64 to 32 bytes: 24 bytes of direct inline/heap storage plus a tagged state word. Ordinary strings allocate no extra metadata or entry object. TTL, integer and LRU state use a separately owned 32-byte object with full-width timestamps and integers.

Native accounting confirms exactly 32 bytes saved per reserved slot: 32 MiB at 800k keys and 64 MiB at the larger flat stage. It independently reconciles table backing, keys, value buffers and live metadata with every requested allocation. The plain-value curve has zero metadata objects, and every tested process reaches zero tracked live allocations at destruction. Equal-size overwrites allocate nothing for each guarded value size.

The tradeoff is explicit: inline capacity falls from 32 to 24 bytes. At 800k keys in the standalone native diagnostic, 25- and 32-byte values use about **3.9 MiB more RSS** than the control (112.5 versus 108.6 MiB). Values of 24, 33 and 256 bytes save about 32 MiB. These standalone numbers exclude server workers and sockets. Small-value speed and metadata-heavy workloads remain unmeasured; the common-case result must not be advertised as universal.

## Reclamation and correctness

The prototype includes the separately tested [memory-release fixes](memory-lifecycle-2026-09-25.md). All 100k TTL keys were physically reclaimed by the 5-second observation. Ordinary DEL still retains table capacity and allocator residency: after deleting all 1.6M keys, RSS remained 569.9 MiB. Explicit FLUSHDB after refill brought it to 61.7 MiB; the overwrite/flush case ended at 52.4 MiB. These residuals depend on allocator history and are not guaranteed floors.

All 270 adapted general unit tests and the five-entry focused test runner passed, covering state transitions, inline boundaries, LRU, fresh-key INCR, allocation failures and metadata ownership. The full server passed idle expiry/WATCH/EXEC scenarios, concurrent varying-size values, binary data, fragmented requests, idle wakeup, large ordered bursts and reconnects. Independent source review found no remaining concrete correctness blocker. This is not a proof of general transaction atomicity.

## Reproducibility and next gate

Binary SHA-256: `e3d9e6845dafa001932350dc16e1c4692518f182aeefc5ebef22a27c94c3d730`. Local image: `vex-entry32:prototype-arm64`. Source, build options, test logs, native accounting and socket measurements are under `bench/loadtest/runs/2026-09-25-entry32-prototype/`. The [reviewable patch](../bench/loadtest/patches/entry32-candidate-2026-09-25.patch) applies to the release candidate; production files remain unchanged by this experiment.

The remaining acceptance gate is matched AWS throughput and p99 testing, including the 32-byte-value guard. The existing release-candidate control check is also still pending. Uploading compiled benchmark code to the private ECR repository requires explicit user approval after automatic approval review blocked the first upload. No new AWS machines were launched.
