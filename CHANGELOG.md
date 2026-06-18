# Changelog

### v0.8.0 — Production Hardening (Observability + Stability)

**Observability (Redis-compatible operator surface):**
- `INFO` expanded to ~50 fields across 11 sections. Field names mirror Redis 7 — `redis_exporter` works against vex unmodified.
- `SLOWLOG GET / LEN / RESET` with per-worker bounded rings.
- `LATENCY LATEST / HISTORY / DOCTOR / RESET` for rare slow events (fsync, snapshot, eviction).
- `CLIENT LIST / INFO` aggregated across workers via a global client registry.
- `DEBUG OBJECT / SLEEP`, `MEMORY USAGE / STATS`.
- `CONFIG GET *` (13 knobs); runtime-mutable: `log-level`, `latency-monitor-threshold`, `appendfsync`.
- Logger: file output + JSON format.

**Stability:**
- Atomic persistence (snapshot, AOF rewrite, HNSW/vector save) via tmp+fsync+rename+dir-fsync. `kill -9` mid-write preserves the previous version.
- `appendfsync` config: `always` / `everysec` (default) / `no`.
- Disk-full STOP-WRITE: ENOSPC → write commands return `-MISCONF`; reads continue.
- ConcurrentKV maxmemory enforcement (per-stripe sample-LRU).
- Slow-client backpressure: `max-client-buffer` enforced on output; slow consumers closed instead of OOMing.
- Cluster epoch mechanism: monotonic, persisted to `vex.epoch`, carried in heartbeats; stale-epoch frames rejected; old leaders self-demote.
- `VEX.PROMOTE <epoch>` / `VEX.STATUS` admin commands.
- True seq-precise replication lag per follower; atomic follower full-sync.

### v0.7.1
- HNSW index persistence (`.vhi`), vector persistence fix (`.vvf`), AOF flush fix in scaled mode, prop_mask rebuild fix, VVF bounds validation, Zig 0.17 migration.

### v0.6.0 — Vector Search, GRAPH.RAG & Performance
- `GRAPH.SETVEC/GETVEC/VECSEARCH` and `GRAPH.RAG` (vector ANN + graph BFS in one command). HNSW (M=16, ef=200/50), cosine similarity, graph-native results.
- io_uring recv/send for TCP I/O, async AOF fsync + Direct I/O. (SQPOLL was trialled here and later removed — it oversubscribes cores at `workers > 1`.)
- Batch commands (MGET/MSET/HMGET/HMSET/HGETALL), RESP serialization tuning, parallel BFS frontier, parallel vector load/save.

### v0.5.0 — Sets & Sorted Sets
All five Redis data types: Sets and Sorted Sets added.

### v0.4.0 — Lists, Hashes & WATCH
Lists, Hashes, WATCH/UNWATCH optimistic locking, 30 Redis-compat commands.

### v0.3.0 — Production Hardening
TLS, MULTI/EXEC, pub/sub, LRU eviction, BGSAVE, AOF group commit, config files, structured logging, automatic failover.

### v0.2.0 — Distributed KV + Graph Read Replicas
Leader/follower replication, full sync, heartbeat lag tracking, write forwarding.

### v0.1.0 — Initial Release
Redis-compatible KV + graph DB with multi-reactor architecture.
