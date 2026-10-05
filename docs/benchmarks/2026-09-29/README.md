# Current benchmark package — September 29, 2026

This is the current package for the verified Vex adaptive / Redis / Dragonfly
sorted-set comparison. Superseded public material has been deleted; raw evidence is retained.

| File | Purpose |
|---|---|
| [pitch.md](pitch.md) | Headline, supporting copy, short pitch and publication qualifications |
| [comparison.png](comparison.png) / [comparison.svg](comparison.svg) | Throughput and CPU chart for all four workloads |
| [results.csv](results.csv) / [results.json](results.json) | All 12 engine/workload summaries, including ranges and latency |
| [reproduce.md](reproduce.md) | Chart regeneration and AWS reproduction commands |
| [provenance.json](provenance.json) | Source hashes and deletion inventory |
| [Original protocol](2026-09-28-adaptive-three-aws-protocol.json) | Original host pair and settings |
| [Completion protocol](2026-09-29-adaptive-three-aws-final-protocol.json) | Fresh matched pair for all three 1,024-set configurations |

[Current public benchmark page](../../benchmarks.md) ·
[Full measurement report](../../adaptive-three-engine-aws-2026-09-28.md)

Raw measurements and frozen binaries remain in `bench/loadtest/runs/`. Shared
runner dependencies remain in `bench/loadtest/scripts/`; moving those scripts
would break reproduction, so they are retained. The package describes measured
experimental-build behavior, not default-release performance or maximum capacity.
