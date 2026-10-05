# Realistic workload pilot — 26 September 2026

The first complete AWS pilot exercised six memtier profiles plus a Locust cache-aside scenario on both Vex rc.2 and rc.3. This is one short sample per engine/profile, intended to validate the workload suite. It is not a release gate, a repeated performance ranking, or a long-duration memory-stability result.

## Memtier observations

Matched aggregate caps: 128,000 ops/s, rising to 384,000 during the burst stage. The client is closed-loop and rate-capped. Throughput is achieved throughput, not demonstrated maximum capacity.

| Profile / phase | Version | Achieved ops/s | p99 ms | p99.9 ms | GET misses | Target reached |
|---|---|---:|---:|---:|---:|---|
| baseline / measure | rc.2 | 127,948 | 0.399 | 0.543 | 0.0% | yes |
| baseline / measure | rc.3 | 127,973 | 0.311 | 0.391 | 0.0% | yes |
| hot / measure | rc.3 | 127,996 | 0.287 | 0.351 | 0.0% | yes |
| hot / measure | rc.2 | 128,120 | 0.255 | 0.327 | 0.0% | yes |
| mixed / measure | rc.2 | 128,001 | 0.295 | 0.367 | 0.0% | yes |
| mixed / measure | rc.3 | 128,000 | 0.303 | 0.399 | 0.0% | yes |
| churn / measure | rc.3 | 127,995 | 0.487 | 0.823 | 27.7% | yes |
| churn / measure | rc.2 | 127,944 | 0.295 | 0.495 | 27.7% | yes |
| burst / steady | rc.2 | 127,999 | 0.287 | 0.351 | 0.0% | yes |
| burst / burst | rc.2 | 383,952 | 0.295 | 0.359 | 0.0% | yes |
| burst / recovery | rc.2 | 127,965 | 0.271 | 0.335 | 0.0% | yes |
| burst / steady | rc.3 | 127,992 | 0.295 | 0.415 | 0.0% | yes |
| burst / burst | rc.3 | 383,957 | 0.287 | 0.407 | 0.0% | yes |
| burst / recovery | rc.3 | 127,954 | 0.279 | 0.359 | 0.0% | yes |
| soak / measure | rc.3 | 127,986 | 0.367 | 0.655 | 30.7% | yes |
| soak / measure | rc.2 | 128,013 | 0.439 | 0.623 | 30.7% | yes |

## Cache-aside journeys

These include a simulated 5 ms backend delay on misses. REDIS commands and JOURNEY events are counted separately; Locust’s combined aggregate is not database throughput.

| Version | Completed journeys | Journey p99 ms | GET miss fraction | Recorded failures |
|---|---:|---:|---:|---:|
| rc.2 | 201,675 | 7.0 | 15.7% | 0 |
| rc.3 | 201,324 | 8.0 | 15.7% | 0 |

## Memory observations

Peak RSS covers the entire profile, including preload/warmup. It is not comparable to the previous one-million-key benchmark. The shortened soak lasts only 90 seconds.

| Profile | rc.2 peak RSS MiB | rc.3 peak RSS MiB |
|---|---:|---:|
| baseline | 52.9 | 46.2 |
| hot | 76.5 | 72.9 |
| mixed | 109.0 | 111.0 |
| churn | 102.4 | 102.0 |
| burst | 110.7 | 112.5 |
| soak | 103.9 | 102.9 |
| locust | 13.6 | 13.5 |

## Method and validation

- Separate dedicated ARM64 c7g.2xlarge server and c7g.8xlarge client in ap-south-1a. Equal 6 CPU / 4 GiB server budgets; 24 CPU / 4 GiB client budget. Fresh server for every cell, engine order alternated between profiles.
- Memtier: 100,000 keys, 128 connections, pipeline 1, 80/20 GET/SET, 10-second warmup, 20-second measurement stages; 90-second short soak. Mixed value weights: 32 B 50%, 256 B 30%, 1 KiB 15%, 4 KiB 5%. Churn/soak TTLs 1–10 seconds.
- Locust: 10,000-key cache, 32 → 96 → 32 users, 20 seconds per stage, 5–20 ms think time, 10–60 second TTLs, 90% read-through and 10% invalidation tasks. Fresh cache starts cold.
- Original Vex executable hashes verified; private ECR mirrors retained the exact original image digests. Source snapshots, Python dependency versions, raw logs, JSON histograms/time series, CSV history, cgroup checks, and 0.5-second RSS/CPU samples were retained.
- Completed cells passed process/pod/node identity checks and no-swap/no-OOM checks. Memtier outputs passed clock, latency, error, effective-configuration, and client-headroom checks. Locust reported no request failures.
- Local checks covered all memtier profiles, both Vex versions with Locust, binary values and expiration, and deliberate corrupt-value rejection. Three unit checks cover workload parameters, validation failures, and the shared client-memory snapshot contract.

Two incomplete attempts are retained separately: the first could not pull private GHCR images; the second completed one workload but failed client-memory metadata validation in the new wrapper. Neither contributes to these tables. The complete run used a corrected wrapper with a regression test.

## Reuse and evidence

- [Workload definitions and commands](../bench/loadtest/REALISTIC-WORKLOADS.md)
- [Complete pilot JSON](../bench/loadtest/runs/2026-09-26-realistic-aws-pilot-v3/summary.json)
- [Raw pilot artifacts](../bench/loadtest/runs/2026-09-26-realistic-aws-pilot-v3/)
- [Local compatibility evidence](../bench/loadtest/runs/2026-09-26-realistic-smoke/validation.json)

The next performance investigation should repeat the relevant profiles at matched achieved rates with longer runs. A short capped-load pilot cannot resolve the earlier 32-byte p99 regression.

## Cleanup

Both dedicated EC2 instances for the completed pilot are verified terminated. No benchmark namespaces, NodePools, NodeClaims, or nodes remain from any of the three attempts. [EC2 termination evidence](../bench/loadtest/runs/2026-09-26-realistic-aws-pilot-v3/cleanup-ec2.json). The digest-identical private ECR image mirrors are retained for reruns.
