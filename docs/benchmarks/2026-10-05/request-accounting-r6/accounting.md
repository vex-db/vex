# Request CPU accounting

Diagnostic context only. Whole-host counters include background services and wider measurement brackets. Host counters are reported in ticks because USER_HZ was not recorded for this host; do not compare their numeric values directly with cgroup microseconds.

| Cell | Connections × pipeline | Accepted / saved | Vex user µs/op | Vex system µs/op | Host system + softirq ticks / million ops |
|---|---:|---:|---:|---:|---:|
| c2-after-get | 32 × 1 | 3 / 3 | 1.096 | 6.638 | 1091.008 |
| c2-after-hash | 32 × 1 | 3 / 3 | 2.039 | 6.603 | 1114.187 |
| c2-after-zset | 32 × 1 | 3 / 3 | 2.492 | 6.623 | 1085.901 |
| c4-after-get | 128 × 1 | 3 / 3 | 1.163 | 6.885 | 1154.187 |
| c4-after-hash | 64 × 1 | 3 / 3 | 2.030 | 7.184 | 1172.985 |
| c4-after-zset | 128 × 1 | 3 / 3 | 2.542 | 7.098 | 1142.716 |
| c8-after-get | 256 × 1 | 3 / 3 | 1.229 | 8.023 | 1186.598 |
| c8-after-hash | 256 × 1 | 3 / 3 | 2.210 | 8.431 | 1214.444 |
| c8-after-zset | 256 × 1 | 3 / 3 | 2.669 | 8.323 | 1226.748 |

Only completed cells with valid network checks and valid client measurements are used. Samples whose accounting bracket exceeds the measured runtime by 25% are excluded here, not erased from the original throughput evidence. Small accepted groups do not establish statistical significance. Compare host tick costs only within the same unchanged host cohort.

Raw source: `/Users/pratyushsingh/vex-co/vex-performance/bench/loadtest/runs/2026-10-05-request-scaling-aws-r6`.
