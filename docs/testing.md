# Testing

[Back to README](../README.md) | [Architecture](architecture.md)

---

## Running Tests

```bash
# Run all tests
zig build test

# Run test binary directly (verbose output with test names)
zig build test 2>&1 | grep -o '\./\.zig-cache[^ ]*test' | head -1
# Then run the binary path directly:
./.zig-cache/o/<hash>/test

# Run in ReleaseFast (includes multi-thread stress test)
zig build test -Doptimize=ReleaseFast
```

---

## Line Coverage

```bash
zig build coverage          # runs the test binary under kcov → coverage/index.html
```

Requires `kcov` on PATH (`brew install kcov` on macOS, `apt-get install kcov`
on Linux). The report covers `src/` only (std/libc and `vendor/`, `tests/`
are excluded). The output directory `coverage/` is git-ignored.

**Current line coverage: ~66% (5,234 / 7,897 lines in `src/`).**

The uncovered remainder is dominated by I/O and platform paths that unit
tests don't exercise directly — the io_uring/kqueue event loop, TLS
handshake, cluster replication wire I/O, and CLI/arg parsing. Those are
covered instead by the chaos suite (`make stress-quick`) and integration
runs, not by `zig build test`.

---

## Test Coverage

**233 tests total (232 passed, 1 skipped in debug mode)**

> The per-module counts below cover the engine + command + RESP core. The
> grand total also includes cluster, observability, storage, and server
> infrastructure tests not broken out here.

| Module | Tests | What's Covered |
|--------|-------|----------------|
| **kv.zig** | 15 | SET/GET, tombstone DEL, tombstone reuse, overwrite, exists, dbsize, compact, TTL tracking, keys skip tombstones, flushdb, glob matcher, memoryUsage, LRU eviction, noeviction error, last_access tracking |
| **concurrent_kv.zig** | 9 | Basic set/get, delete, overwrite, exists, flushdb+dbsize, multi-thread stress (8 threads x 1000 ops) |
| **list.zig** | 7 | LPUSH/RPUSH, LPOP/RPOP, LLEN, LRANGE, LINDEX, LSET/LREM, popTail→pushTail block-boundary cursor regression |
| **hash.zig** | 13 | HSET/HGET, HDEL, HMSET/HMGET, HGETALL (RESP2/RESP3, empty, large-value no-truncation, wire-cache invalidation), HLEN, HKEYS/HVALS, HEXISTS, HINCRBY |
| **set.zig** | 7 | SADD/SREM, SMEMBERS, SISMEMBER, SCARD, SUNION, SINTER, SDIFF |
| **sorted_set.zig** | 8 | ZADD/ZREM, ZCARD, ZRANK, ZSCORE, ZINCRBY, ZCOUNT, ZRANGE |
| **vector_store.zig** | 10 | Dual-tier store, f16 quantization, mmap save/load, lazy init, multi-field isolation |
| **hnsw.zig** | 7 | HNSW insert/search, recall accuracy, distance calculations, layer management |
| **rag.zig** | 2 | RAG search with graph expansion, vector+BFS integration |
| **graph.zig** | 14 | Add nodes/edges, duplicate node error, node properties, remove node, remove edge, compact (+ CH auto-build), type interning, type mask filtering, uniform weights flag, edge properties, all_base_edges_alive flag, vector field integration |
| **ch.zig** | 3 | CH basic correctness (5-node weighted graph vs Dijkstra), CH larger-graph validation (100-node, 50 random queries vs Dijkstra), dense-graph abort (bounded memory, no OOM) |
| **query.zig** | 12 | BFS traverse outgoing, shortest path, weighted shortest path (flat-array Dijkstra), neighbors, edge type filter, traverse after compact, traverse with delta only, shortest path via delta, parallel BFS, LIMIT, impact analysis, list_by_type |
| **handler.zig** | 27 | PING, SET/GET, GRAPH.ADDNODE, SELECT namespace isolation, MGET/MSET, INCR/DECR/INCRBY/DECRBY, EXPIRE/PERSIST/TTL, APPEND, BGSAVE, lists/hashes/sets/sorted sets, vector commands, UPSERT, RENAME, TYPE, GETEX/GETDEL, typed GRAPH.TRAVERSE/NEIGHBORS hits, multi-type EDGETYPE filter |
| **resp.zig** | 16 | Parse RESP array, null bulk string, serialize round-trip, inline command parse + Redis-compatible quoting (escapes, single quotes, unbalanced detection) |
| **aof.zig** | 4 | Write and replay, truncate, replay missing file, group commit buffer |
| **snapshot.zig** | 5 | Round-trip (KV + graph with properties), missing file, CRC corruption detection, CRC-32 known value |
| **worker.zig** | 4 | PubSubRegistry subscribe+getSubscribers, unsubscribe, unsubscribeAll, duplicate subscribe prevention |
| **log.zig** | 3 | Level parse (debug/info/warn/error/unknown), level filtering, timestamp format validation |
| **config.zig** | 3 | Config file parse (key-value, comments, boolean flags), empty config, comments-only config |
| **main.zig** | 1 | parseMemorySize (kb/mb/gb/bytes/empty/invalid inputs) |
| **event_loop.zig** | 2 | Pipe read triggers readable event, notify wakes poll |
| **cluster/config.zig** | 3 | Parse leader config, parse follower config, invalid config (missing self) |
| **cluster/protocol.zig** | 3 | Encode/decode repl_request, encode/decode write_forward, frame header size |
| **cluster/replication.zig** | 3 | isWriteCommand, follower promoted flag blocks forwarding, probeForLeader returns null |
| **shard_router.zig** | 5 | Key-to-shard routing, MPSC queues, worker dispatch |
| **tls.zig** | 1 | TLS context initialization |
| **property_store.zig** | 6 | Set/get, overwrite, delete, deleteAll, count, collectAll |
| **Other** | 8 | String intern (intern/resolve, find null, mask positions, max limit), comptime dispatch (unique keys, key computation, resp literals, findCommand) |

### Skipped Test

The `concurrent_kv multi-thread stress` test is skipped in Debug mode:

```zig
if (@import("builtin").mode == .Debug) return error.SkipZigTest;
```

**Reason:** Zig's debug HashMap has a `pointer_stability` safety check that conflicts with external rwlock synchronization. The test passes in ReleaseFast mode where this check is disabled. This is not a bug -- it's a known interaction between Zig's debug safety checks and low-level pthread locking.

To run it:
```bash
zig build test -Doptimize=ReleaseFast
```

---

## Test Design Patterns

### Handler Tests

Handler tests create a standalone `CommandHandler` with a real `KVStore` and `GraphEngine`, execute commands, and verify the RESP response bytes:

```zig
test "command handler SET/GET" {
    var kv = KVStore.init(allocator, io);
    defer kv.deinit();
    var handler = CommandHandler.init(allocator, io, &kv, &g, null, &db, .strict);

    const set_args = [_][]const u8{ "SET", "mykey", "myvalue" };
    try handler.execute(&set_args, &writer);
    try std.testing.expectEqualStrings("+OK\r\n", written());
}
```

### PubSub Tests

PubSub tests exercise the `PubSubRegistry` directly without a full server:

```zig
test "PubSubRegistry subscribe and getSubscribers" {
    var ps = PubSubRegistry.init(allocator);
    defer ps.deinit();
    try ps.subscribe("news", 10);
    var subs = std.array_list.Managed(i32).init(allocator);
    ps.getSubscribers("news", &subs);
    try std.testing.expectEqual(@as(usize, 1), subs.items.len);
}
```

### Persistence Tests

Persistence tests write to temp files in `/tmp/` and clean up with `defer`:

```zig
test "snapshot round-trip" {
    const path = "/tmp/vex_test_v2.zdb";
    defer std.Io.Dir.cwd().deleteFile(io, path) catch {};
    try save(io, allocator, &kv, &graph, path);
    try load(io, allocator, &kv2, &g2, path);
    // Verify restored state matches
}
```
