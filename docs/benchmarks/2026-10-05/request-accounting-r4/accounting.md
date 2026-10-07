# Request CPU accounting

Diagnostic context only. Whole-host counters include background services and wider measurement brackets. Host counters are reported in ticks because USER_HZ was not recorded for this host; do not compare their numeric values directly with cgroup microseconds.

| Cell | Connections × pipeline | Accepted / saved | Vex user µs/op | Vex system µs/op | Host system + softirq ticks / million ops |
|---|---:|---:|---:|---:|---:|
| c16-after-get | 512 × 1 | 2 / 3 | 1.286 | 9.047 | 1126.662 |
| c16-after-hash | 512 × 1 | 3 / 3 | 2.288 | 9.680 | 1202.014 |
| c16-after-zset | 512 × 1 | 0 / 3 | — | — | — |
| c8-after-get | 256 × 1 | 3 / 3 | 1.293 | 7.913 | 1163.369 |

Only completed cells with valid network checks and valid client measurements are used. Samples whose accounting bracket exceeds the measured runtime by 25% are excluded here, not erased from the original throughput evidence. Small accepted groups do not establish statistical significance. Compare host tick costs only within the same unchanged host cohort.

Raw source: `/Users/pratyushsingh/vex-co/vex-performance/bench/loadtest/runs/2026-10-01-request-scaling-aws-r4`.
