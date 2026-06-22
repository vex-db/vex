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
- **Reactor fd limit:** `RLIMIT_NOFILE` is raised to `maxclients` at startup (was the OS default, often 1024), and the TCP accept loop now backs off + rate-limits on error instead of tight-spinning. Fixes a throughput collapse past ~1024 connections where `accept()` spun on `EMFILE` (single-core SET cratered 320K→105K at 1600 conns; now flat across 200–6400).
- **Reactor (macOS):** don't `TCP_NOPUSH`-cork replies on macOS — sub-MSS replies could wedge in the kernel send buffer and hang rapid request/reply clients (Linux `TCP_CORK` unaffected).
- **Epoch reclaim:** O(1) early-exit when the safe epoch hasn't advanced, removing an O(n²) blowup on the write path.

**Docs:**
- `docs/benchmarks.md`: per-core ceiling + latency anatomy from AWS `c6in.8xlarge` runs (single pinned core, separate client over ENA, saturated) — vex beats single-thread Redis per-core at P1 (1.16–1.32×), latency is ~99.5% network/kernel (SET ~110 ns, total vex CPU ~1.3 µs), with the must-saturate-the-core caveat.

### v0.7.5 — SET fast path + agent-first docs
- **256-byte inline SET fast path.** Values up to 256 B now take the lock-light inline path; previously only ≤32 B did, so larger SETs fell to a write-lock + heap allocation. Big SET-throughput win for the 64–256 B values typical of cache/session workloads (`INLINE_BUF_SIZE` 32→256).
- **Opt-in io_uring / worker tuning knobs, all default-off:** `VEX_PIN_WORKERS` (worker→CPU affinity), `VEX_NAPI_BUSY_POLL_US`, `VEX_POLL_SPIN_US` / `_ADAPTIVE` (spin-before-park), `VEX_URING_FLAGS` (SINGLE_ISSUER|DEFER_TASKRUN|COOP_TASKRUN). See [docs/tuning.md](docs/tuning.md).
- **Fix:** R_DISABLED io_uring rings are now enabled lazily on first poll, so paths that drive an event loop directly (e.g. unit tests) no longer hit `BADFD`.
- **Docs:** agent-first rewrite against the real APIs; honest Dragonfly head-to-head and vector memory figures (f32 write buffer vs f16 mmap); new `docs/tuning.md` and `docs/usage-patterns.md`; `zig build check-docs` drift guard in CI.

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
