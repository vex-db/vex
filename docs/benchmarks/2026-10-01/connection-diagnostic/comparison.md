# Connection concurrency versus pipelining

16 Vex workers / 16-CPU quota on the same c6gn.8xlarge host. Separate 48-CPU client; 32 load-generator threads in every configuration. Uniform GET, one million 256-byte values, no persistence. Three 60-second trials per configuration, rotating their order.

| Connections | Pipeline | Maximum in flight | Median ops/s | Median p99 ms | CPU µs/op | Server CPUs used | Busiest 16 threads: CPU CV |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 32 | 1 | 32 | 417,326 | 0.119 | 12.87 | 5.40 | 0.059 |
| 32 | 16 | 512 | 3,841,175 | 0.199 | 2.50 | 9.71 | 0.045 |
| 512 | 1 | 512 | 1,536,151 | 1.303 | 10.37 | 15.79 | 0.001 |

With the same 512-request ceiling, 32 connections at pipeline 16 delivered 2.50× the throughput and 75.9% less CPU per operation than 512 connections at pipeline 1. Relative to 32 connections at pipeline 1, throughput rose 9.20×. This is a configuration effect on the same binary, not an engine code optimization.

| Connections × pipeline | User CPU µs/op | Kernel CPU µs/op |
|---|---:|---:|
| 32 × 1 | 2.16 | 10.70 |
| 32 × 16 | 1.15 | 1.35 |
| 512 × 1 | 1.23 | 9.14 |

User/kernel CPU split comes from process-thread counters; it excludes networking work attributed elsewhere on the host. It identifies a cost category, not a particular syscall or proven engine defect.

Only measurements passing client CPU/headroom, process continuity, memory, data validation and ENA allowance checks are reported. These diagnostic rates are not selected as 5 ms capacity maxima.

The two 512-in-flight configurations have the same maximum outstanding request count, but pipelining also batches work and changes latency accounting. Similar throughput supports insufficient concurrency as an explanation; a gap alone does not prove a connection-management defect.

Thread CV is the standard deviation divided by the mean CPU-time delta among the 16 busiest process threads, not an instrumented worker-identity measure. Lower values indicate more balanced CPU work. Cgroup CPU includes measurement-boundary overhead and excludes some host networking work.

Raw evidence: `/Users/pratyushsingh/vex-co/vex-performance/bench/loadtest/runs/2026-10-01-connection-diagnostic-r4`.

Cleanup verified: both EC2 instances terminated; no run-owned Kubernetes resources or nodes remain.
