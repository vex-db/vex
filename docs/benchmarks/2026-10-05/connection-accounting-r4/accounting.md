# Request CPU accounting

Diagnostic context only. Whole-host counters include background services and wider measurement brackets. Host counters are reported in ticks because USER_HZ was not recorded for this host; do not compare their numeric values directly with cgroup microseconds.

| Cell | Connections × pipeline | Accepted / saved | Vex user µs/op | Vex system µs/op | Host system + softirq ticks / million ops |
|---|---:|---:|---:|---:|---:|
| connection-diagnostic | 32 × 1 | 3 / 3 | 2.158 | 10.702 | 1678.077 |
| connection-diagnostic | 32 × 16 | 3 / 3 | 1.147 | 1.355 | 201.826 |
| connection-diagnostic | 512 × 1 | 3 / 3 | 1.230 | 9.137 | 1124.733 |

Only completed cells with valid network checks and valid client measurements are used. Samples whose accounting bracket exceeds the measured runtime by 25% are excluded here, not erased from the original throughput evidence. Small accepted groups do not establish statistical significance. Compare host tick costs only within the same unchanged host cohort.

Raw source: `/Users/pratyushsingh/vex-co/vex-performance/bench/loadtest/runs/2026-10-01-connection-diagnostic-r4`.
