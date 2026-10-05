# SQ-head cache experiment — 2026-09-22

Status: local ARM64 mixed pilot passed; confirmation stopped with failed gates,
large time variation and inconsistent latency telemetry. The optimization
remains experimental and disabled by default. The evidence does not support
default promotion.

The resolved native ARM64 profile placed 4.05% of leaf samples in
`submitUringWrite`, with hot addresses around inlined submission-queue entry
acquisition/preparation. Sample positions do not establish the acquire load's
individual cost. This justifies one bounded, default-off cache experiment.

`VEX_SQ_HEAD_CACHE=1` applies only to optimized single-issuer io_uring rings.
Recv/send reuse a conservative acquired head within a poll batch; first use
and apparent-full checks refresh it. The real producer tail, wrapping
arithmetic, SQE preparation, buffer ownership and fallback paths stay intact.
The cache invalidates at poll boundaries. Astra reviews the code and focused
fake-ring tests; Luna implements it; the coordinator builds and measures it.

All 258 native ReleaseSafe tests passed. Existing fragmented/binary/socket and
concurrent value integration suites passed with the flag off, on, and requested
on a plain ring. The same-binary off/on/on/off pilot used fresh processes/preloads:
two workers, one million 16-byte keys, 256-byte values, P1/c128, 80/20 GET/SET,
10-second warmups and 60-second samples. Preserve the local CPU/memory limits.

Acceptance requires at least 3% median throughput gain exceeding observed
within-mode variation, both pairs faster with lower CPU/op, p99 no more than
5% worse, peak RSS no more than 2% worse, zero errors/misses, and no significant
resource constraint. A passing pilot still requires follow-up workload checks
before default promotion. Archive and remove a rejected experiment without
reverting unrelated changes. Raw artifacts use
`bench/loadtest/runs/2026-09-22-sq-head-cache`.

## Pilot result

| Run | Cache | Ops/s | p99 (ms) | CPU (µs/op) | Peak RSS (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | Off | 440,977 | 0.503 | 4.464 | 458.29 |
| 2 | On | 464,454 | 0.439 | 4.236 | 458.15 |
| 3 | On | 468,666 | 0.447 | 4.207 | 458.33 |
| 4 | Off | 441,017 | 0.479 | 4.467 | 458.34 |

Mode medians are 440,997 off versus 466,560 on: **+5.80%** throughput,
exceeding the observed 0.90% within-mode range. Both pairs improved. Median CPU
cost fell 5.48%; the median of run p99 values fell from 0.491 to 0.443 ms.
Peak RSS was effectively unchanged. All samples had zero checked errors/misses,
stable binary/process identity, no memory-limit events or swap, and no
significant benchmark CPU throttling. This is a four-sample pilot, not a
confidence interval or a portable production result.

Candidate SHA256:
`4f3df6e8916d9c127a6a9a7940701e1faa3e11182c5809310f7bd2c42fc047d6`.
Both modes use the same native ARM64 binary and immutable Docker image. The
Linux VM is shared with existing development containers; CPU sets do not
establish exclusive physical-core isolation. Compare this candidate with its
matched local control, not with the earlier AWS numbers.

## Predeclared confirmation

Use 100,000 keys for every confirmation scenario (IDs 1000000–1099999,
identical 16-byte keys). The 4 KiB payload alone is 390.625 MiB; this smaller
matched dataset fits the existing 1 GiB server limit with room for overhead.
Keep two workers, 128 connections, the same binary and resource assignments.
Every cell gets a fresh process, complete preload, 10-second warmup and
60-second measurement. The million-key pilot remains a separate result.

Run off/on/on/off for each scenario in this order:

1. 32-byte SET-only, pipeline 1.
2. 32-byte GET-only, pipeline 1.
3. Mixed pipeline 1 at 32, 128, 256, 1024 and 4096 bytes.
4. 256-byte mixed pipeline 8, as a regression guard.

Stop immediately for malformed results, errors/misses, changed identity/hash,
wrong key count/value lengths, memory-limit events, swap, VM available memory
below 512 MiB, significant CPU throttling (at least 1%), or insufficient client
headroom (below 5% or any thread at least 95%). After each complete scenario,
each pair must keep throughput within −2%, CPU/op within +2%, p99 within +5%,
and peak RSS within +2%. Any failure blocks default promotion; noisy or
borderline results remain inconclusive without automatic reruns.

The 256-byte mixed pipeline-1 scenario also must repeat the pilot efficacy
gate: at least 3% median throughput gain exceeding within-mode variation,
both throughput pairs positive and both CPU/op pairs lower. Other scenarios
are regression guards and need not gain 3%. Stop after these 32 timed cells;
no extra profile or CPU-count sweep is part of this confirmation.

## Confirmation outcome: stop, no promotion

The first scenario (32-byte SET-only, 100,000 keys, pipeline 1) completed all
four samples, then stopped at its declared regression gates. The remaining
seven scenarios were **not run**.

| Run | Cache | Recorded ops/s | Recorded p99 (ms) | CPU (µs/op) | Peak RSS (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | Off | 472,090 | 0.447 | 4.161 | 21.04 |
| 2 | On | 392,153 | 0.663* | 4.988 | 21.12 |
| 3 | On | 290,198 | 0.823 | 6.690 | 21.10 |
| 4 | Off | 396,675 | 0.735 | 4.929 | 21.07 |

The recorded throughput medians are 434,382 off versus 341,176 on (−21.46%),
and both pairs fail throughput, CPU/op and p99 guards. These aggregates do not
establish a code-caused regression. Within-mode throughput ranges are 17.36%
off and 29.88% on. The first enabled sample starts around 475k ops/s before an
abrupt drop, while the final control climbs from about 340k to 428k ops/s.
Client CPU/op deteriorates alongside server CPU/op. A later host snapshot
cannot identify the historical cause.

The starred sample has unreliable latency telemetry. Independent raw review
found impossible timing values: the first
enabled timed sample reports a 603,979.775 ms aggregate maximum latency in a
60,002 ms run, and one time bucket reports 4,294,967.077 ms. Its accumulated
latency exceeds the closed-loop concurrency bound. The second enabled warmup
has a similar anomaly. Treat the affected latency telemetry as unreliable;
do not assume its p99 is unaffected. The original pilot's four timed samples
and four warmups pass these additional consistency checks.

The retrospective check also rejects the earlier baseline warmup and separate
profile-load latency counters; its two uninstrumented timed samples pass.
Those diagnostic metrics were already excluded from the baseline. Across 20
local raw samples, 16 pass and four show impossible timing. This does not
identify the source of the anomaly.

Other recorded checks passed: configuration, image/binary hashes, within-cell
identity, durations, zero errors/misses, key counts/value lengths and sampled
RSS. Benchmark memory-limit events and swap were zero, available VM memory
stayed above 2.4 GiB, memory PSI was negligible, client headroom remained above
15%, and server throttling stayed below 0.1%. These checks do not establish
exclusive host CPU scheduling or clock stability.

The local runner now rejects impossible aggregate/bucket latency and
closed-loop accumulated latency before accepting a sample. This correction
does not rewrite the original raw data or retroactively validate the failed
confirmation. No additional measurements or speculative source fixes were
made. Keep the candidate for further controlled investigation, explicitly
default off. The first pilot remains a limited positive result; the change
is not ready for broad performance claims.

All task-owned benchmark containers, networks and the completed builder were
removed. Existing development containers were untouched; the baseline and
candidate images are retained. Portable evidence is under
`bench/loadtest/results/sq-head-cache-2026-09-22.json` and
`sq-head-cache-confirmation-2026-09-22.json`; complete raw confirmation data is
under `bench/loadtest/runs/2026-09-22-sq-head-confirmation`.
