# Vex sorted-set performance pitch

## Headline

**3.66 million sorted-set operations per second. An eight-CPU allocation.**

## Supporting copy

Vex’s experimental adaptive build delivered **4.65× Redis throughput and 1.91× Dragonfly throughput** in our
1,024-set AWS workload, while using **52% less server CPU time per operation
than Dragonfly**. All three engines received an eight-CPU quota and 8 GiB of
memory, with identical client settings and persistence disabled.

For mixed hot/cold traffic, Vex delivered **2.18× Redis throughput and 2.28×
Dragonfly throughput**. Its experimental adaptive ownership routes sustained
hot-key contention to one worker while allowing unrelated connections to keep
moving.

**See the workload. See the methodology. See the results.**

## Thirty-second pitch

“Vex combines fast sorted-set processing with experimental adaptive hot-key
ownership. In our matched eight-CPU AWS tests, it led Redis and Dragonfly on
mixed hot/cold traffic and both uniform workloads. The highest measured median
was 3.66 million operations per second—4.65 times Redis and 1.91 times Dragonfly
in that workload. Redis still won the single-hot-key test. We publish all four
workloads, CPU cost, latency, and the test setup so you can judge the trade-offs.”

## Three proof points

| Message | Evidence | Scope |
|---|---|---|
| High sorted-set throughput | 3.660M ops/s; 4.65× Redis; 1.91× Dragonfly | Uniform access to 1,024 sets × 256 members |
| Lower CPU cost than Dragonfly | 1.976 vs 4.124 µs/op; 52% lower | Same 1,024-set workload |
| Keep unrelated keys moving | 1.569M total ops/s vs Redis 0.718M and Dragonfly 0.689M | 24 hot connections and 8 cold connections across 63 other keys |

## Qualification to publish alongside the claims

Sorted-set benchmark, September 28–29, 2026. Approximately 80% ZINCRBY / 20%
ZRANK; 32 connections; pipeline 16; medians of three 60-second measurements.
Eight-CPU cgroup quota and 8 GiB per engine on AWS Graviton, with a separate
client host. This is a fixed-concurrency comparison, not peak capacity.
Vex used an experimental build with adaptive ownership explicitly enabled;
the feature is off by default. Redis led on a single hot key. Uniform tests
never activated ownership, so their performance is not attributed to that
mechanism. The 1,024-set comparison used a fresh matched host pair after an
interruption; all three engines were rerun for that workload.

[Complete results and methodology](../../benchmarks.md) · [Machine-readable results](results.json)

## Editorial limits

Do not turn these measurements into “always faster,” “25× Redis,” “linear
scaling,” production-default adaptive ownership, or infrastructure cost savings.
CPU time per operation is not a cloud bill. These tests do not establish GET/SET,
persistence, larger-dataset, long-running production or independently optimized
peak-capacity superiority. Describe measured sorted-set throughput precisely.
