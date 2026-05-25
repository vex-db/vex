# Persistence

[Back to README](../README.md) | [Commands](commands.md) | [Configuration](configuration.md) | [Deployment](deployment.md)

---

Vex uses a dual persistence model similar to Redis RDB+AOF.

**Crash safety guarantee (since v0.8):** every on-disk file is updated atomically — write to a tmp file, `fsync` it, `rename` over the canonical path, then `fsync` the parent directory. After a `kill -9` at any point, the canonical file contains either the previous version or the new one; never partial. Combined with the `appendfsync` knob (default `everysec`), data loss on hard crash is bounded to ≤1s.

## Overview

| Component | File | Description |
|-----------|------|-------------|
| Snapshot | `vex.zdb` | Binary format with CRC-32 checksum. Full KV + graph state. Written atomically. |
| AOF | `vex.aof` | Append-only file. Every write command in binary format. Fsync per `appendfsync`. |
| Vectors | `vectors/*.vvf` | Per-field f16 vector embeddings. Written atomically. |
| HNSW Index | `vectors/*.vhi` | Serialized HNSW graph. Written atomically. |
| Cluster epoch | `vex.epoch` | u64, monotonically increasing. Written atomically on every leader promotion. |

### Lifecycle

1. **Startup**: (1) load `vex.epoch` (cluster mode), (2) load snapshot (`vex.zdb`), (3) replay AOF (`vex.aof`), (4) load vectors (mmap `.vvf` files + deserialize `.vhi` index or rebuild HNSW if `.vhi` missing)
2. **Runtime**: write commands are buffered in memory and flushed to AOF per event loop tick (group commit). fsync happens per `appendfsync` (default `everysec`).
3. **SAVE/BGSAVE**: writes a new snapshot atomically, truncates AOF, saves `.vvf` + `.vhi` files atomically
4. **BGREWRITEAOF**: compacts AOF — writes to tmp, merges in-flight writes under mutex, fsyncs tmp, atomic-rename, fsyncs parent dir
5. **Shutdown**: SIGTERM/SIGINT triggers a final AOF flush + background fsync thread stop

```bash
zig build run -- --data-dir /var/lib/vex      # persistence enabled (default)
zig build run -- --no-persistence              # disable for benchmarking
```

---

## Snapshot Format (v2)

Binary format with the following structure:

```
MAGIC ("ZGDB", 4 bytes)
VERSION (1 byte, currently 2)
TIMESTAMP (i64, milliseconds since epoch)
KV SECTION:
  count (u32)
  [key (length-prefixed) + value (length-prefixed) + has_ttl (u8) + expires_at (i64, if has_ttl)]*
TYPES SECTION:
  count (u16)
  [type_string (length-prefixed)]*
NODES SECTION:
  count (u32)
  [alive (u8) + key (length-prefixed) + type_id (u16) + prop_count (u32) + [prop_key + prop_value]*]*
EDGES SECTION:
  count (u32)
  [alive (u8) + from (u32) + to (u32) + type_id (u16) + weight (f64) + prop_count (u32) + [prop_key + prop_value]*]*
CRC-32 (u32, IEEE 802.3)
```

The CRC-32 checksum covers all bytes before it. If the checksum doesn't match on load, Vex reports `ChecksumMismatch` and refuses to load the corrupted snapshot.

**prop_mask rebuild**: `node_prop_mask` and `edge_prop_mask` are not stored in the snapshot format. During load, they are rebuilt by iterating each node/edge's property keys and setting the corresponding bits via the key intern table.

---

## Background Save (BGSAVE)

`BGSAVE` spawns a dedicated thread to write the snapshot without blocking command processing.

**How it works:**
1. Atomically sets `bgsave_in_progress` flag (CAS-based, prevents concurrent saves)
2. Spawns a new thread
3. Thread acquires read lock on graph engine (allows concurrent reads, blocks writes)
4. Serializes full KV + graph state to `vex.zdb`
5. Truncates AOF
6. Releases locks, clears `bgsave_in_progress` flag

```
127.0.0.1:6380> BGSAVE
"Background saving started"

127.0.0.1:6380> BGSAVE
(error) ERR Background save already in progress

127.0.0.1:6380> LASTSAVE
(integer) 1745477400
```

**vs SAVE:** `SAVE` runs in the foreground and blocks all commands until the snapshot is complete. Use `BGSAVE` in production.

---

## AOF Group Commit

Instead of writing to the AOF file on every command, Vex buffers commands in memory and flushes the entire batch at the end of each event loop tick.

### Before (per-command I/O)

```
SET k1 v1 → seek + write + flush     (3 syscalls)
SET k2 v2 → seek + write + flush     (3 syscalls)
SET k3 v3 → seek + write + flush     (3 syscalls)
                                      Total: 9 syscalls
```

### After (group commit)

```
SET k1 v1 → append to memory buffer  (0 syscalls)
SET k2 v2 → append to memory buffer  (0 syscalls)
SET k3 v3 → append to memory buffer  (0 syscalls)
[end of tick] → seek + write + flush  (3 syscalls)
                                      Total: 3 syscalls
```

With pipeline depth of 100, this reduces syscall overhead by ~100x.

### Implementation

- `logCommand()` appends binary record to an in-memory `group_buf` (under mutex, no I/O)
- `flush()` writes entire buffer to AOF file in one `write()` syscall
- Worker calls `aof.flush()` at end of each event loop tick (after processing all commands from one `poll()`)
- Falls back to direct per-command writes when group buffer is not initialized (during AOF replay at startup)

### AOF Binary Record Format

Each record in the AOF is:

```
TIMESTAMP (i64, 8 bytes, little-endian, milliseconds since epoch)
ARG_COUNT (u16, 2 bytes, little-endian)
[ARG_LENGTH (u32, 4 bytes) + ARG_DATA (variable)]*
```

---

## BGREWRITEAOF

Compacts the AOF by serializing the current in-memory state:

1. Creates a temp file (`vex.aof.rewrite.tmp`)
2. Iterates all live KV entries and graph nodes/edges
3. Writes equivalent SET/GRAPH.ADDNODE/GRAPH.ADDEDGE commands
4. Atomically renames temp file over the current AOF

This eliminates redundant operations (e.g., 1000 SETs to the same key become 1 SET).

---

## Vector Persistence (.vvf files)

Vector embeddings are stored in separate `.vvf` (Vex Vector Field) files, one per vector field:

```
data-dir/
├── vex.zdb          # KV + graph snapshot
├── vex.aof          # Append-only file
└── vectors/
    ├── field_0.vvf  # Embeddings for field 0
    ├── field_0.vhi  # HNSW index for field 0
    ├── field_1.vvf  # Embeddings for field 1
    └── field_1.vhi  # HNSW index for field 1
```

### .vvf Format

20-byte header followed by vector data:

```
MAGIC "VXVF" (4 bytes)
VERSION (1 byte)
DTYPE (1 byte: 0=f32, 1=f16)
DIMENSION (u32, 4 bytes)
COUNT (u32, 4 bytes)
RESERVED (6 bytes)
[vector data: (4 + dimension * dtype_size) * count bytes]
```

On load, the file is validated: `entry_stride * count + header_size` must fit within the actual file size. A mismatch indicates corruption and the load is rejected.

### Lifecycle

- **First SETVEC**: VectorStore is lazily initialized (zero cost when vectors unused)
- **Runtime**: new vectors are written to an in-memory f32 buffer
- **SAVE/BGSAVE**: vectors are quantized to f16 and written to `.vvf` files; HNSW indices serialized to `.vhi` files
- **Shutdown**: final vector save triggered alongside KV snapshot
- **Startup**: `.vvf` files are mmap'd for zero-copy reads; `.vhi` files are deserialized to restore HNSW indices instantly (falls back to parallel rebuild if `.vhi` is missing or corrupt)

---

## HNSW Index Persistence (.vhi files)

HNSW indices are serialized to `.vhi` (Vex HNSW Index) files during SAVE/BGSAVE and deserialized on startup, skipping the expensive index rebuild.

```
data-dir/
└── vectors/
    ├── field_0.vvf  # Vector data
    ├── field_0.vhi  # HNSW index for field 0
    ├── field_1.vvf
    └── field_1.vhi
```

### .vhi Format

40-byte header followed by graph structure:

```
MAGIC "VXHI" (4 bytes)
VERSION (1 byte)
MAX_LEVEL (1 byte)
M (u16)
M_MAX0 (u16)
EF_CONSTRUCTION (u16)
DIMENSIONS (u32)
ENTRY_POINT (u32)
COUNT (u32)
CAPACITY (u32)
HIGHER_LAYER_COUNT (u16)
RNG_STATE (u64)
--- layer 0 neighbors (per node: u16 count + up to M_max0 * u32 neighbor IDs)
--- node levels (per node: u8)
--- higher layers (per layer per node: u16 count + up to M * u32 neighbor IDs)
```

Writes use atomic `.vhi.tmp` + rename to prevent partial files on crash. On load, magic and version are validated; mismatches fall back to HNSW rebuild from vectors.

---

## Advanced AOF Features

### Per-Worker AOF Shards

In reactor mode, each worker writes to its own AOF shard to avoid mutex contention on the main AOF:

```
data-dir/
├── vex.aof          # Worker 0 (primary)
├── vex.aof.shard1   # Worker 1
├── vex.aof.shard2   # Worker 2
└── vex.aof.shard3   # Worker 3
```

All shards are replayed on startup based on the configured shard count.

### Direct I/O (Linux)

On Linux, the AOF can use `O_DIRECT` to bypass the page cache:

- Opens a second file descriptor with `O_DIRECT` flag
- Uses a 4KB-aligned staging buffer for sector-aligned writes
- Falls back gracefully on systems without `O_DIRECT` support

### Async AOF Fsync (io_uring)

When io_uring is available, AOF writes and fsyncs are submitted asynchronously:

- `prepareAsyncFlush()` returns pending data for io_uring submission
- Double-buffered: `group_buf` accumulates while `flush_buf` is written
- Worker submits write+fsync to io_uring, falls back to synchronous `write()` if unavailable

---

## Durability Modes (`appendfsync`)

| Mode | Behavior | Loss on crash | Hot-path cost |
|---|---|---|---|
| `always` | fsync after every AOF flush, inline | ~0 | high (~5ms per flush on rotational, ~50µs on NVMe) |
| `everysec` (default) | background thread fsyncs every 1s | ≤1s | 0% (background thread does the work) |
| `no` | rely on OS page cache flush | unbounded (until OS flushes) | 0% |

Set via CLI flag or config:

```bash
zig build run -- --appendfsync always       # max durability
zig build run -- --appendfsync everysec     # default — Redis-equivalent
zig build run -- --appendfsync no           # fastest, least durable
```

Or runtime-switchable via `CONFIG SET`:

```
127.0.0.1:6380> CONFIG SET appendfsync always
OK
127.0.0.1:6380> INFO | grep aof_fsync_mode
aof_fsync_mode:always
```

The implementation uses a separately-opened raw fd (`fsync_fd`) so fsync calls don't round-trip through Zig's writer interface. On macOS the call is `fcntl(fd, F_FULLFSYNC)` — the only path that pushes through the drive's write cache; plain `fsync` returns after data hits the drive cache. On Linux it's plain `fsync(fd)`.

`INFO` exposes:
- `aof_fsync_mode` — current mode
- `aof_last_fsync` — wall-clock timestamp of the last successful fsync (updated by the background thread or the inline `always` path)
- `aof_last_write_status` — `ok` or `err` (see STOP-WRITE below)

## STOP-WRITE on Disk Full

When the AOF flush path encounters an unrecoverable I/O error (ENOSPC, EIO), vex sets a process-wide `persistence_broken` flag. The dispatch hot path checks this flag and rejects write commands with `-MISCONF`. Reads continue. Without this, the previous behavior was: client receives `+OK` for a write that never made it to disk, then a crash loses the data silently.

Operator escape hatch: `CONFIG SET appendfsync no` clears the flag (explicit trade of durability for availability). Otherwise, restart after fixing the underlying disk issue.

```
127.0.0.1:6380> SET k v
-MISCONF Errors writing to the AOF file: persistence is in STOP-WRITE state.

127.0.0.1:6380> GET k          # reads still work
"v"

127.0.0.1:6380> CONFIG SET appendfsync no
OK
127.0.0.1:6380> SET k v
OK
```

Field surface: `aof_last_write_status:err` in `INFO Persistence`. Polled by `redis_exporter` (and matches Redis's field name) for alerting.

## Durability Guarantees (after v0.8)

| Scenario | Data Loss |
|----------|-----------|
| Clean shutdown (SIGTERM) | None. Final snapshot + AOF flush + fsync |
| `SAVE` or `BGSAVE` + crash mid-write | None. tmp+fsync+rename+dir-fsync makes the snapshot atomic. |
| `BGREWRITEAOF` + crash mid-rewrite | None. In-flight writes are merged into the tmp before rename. |
| Hard crash (`kill -9`, power loss), `appendfsync=always` | None (≤ a few uncommitted commands in the worst case). |
| Hard crash, `appendfsync=everysec` (default) | ≤1s of commands. Bounded by the background fsync cadence. |
| Hard crash, `appendfsync=no` | Unbounded (whatever the OS hadn't flushed). |
| AOF tail corruption | Stops at first torn record; warns with offset + records-replayed via the logger. Commands after the torn record are lost. |
| Snapshot corruption | CRC mismatch detected, snapshot rejected. Falls back to empty state + AOF replay. |
| Disk full mid-flush | STOP-WRITE state engages; writes return `-MISCONF`; reads continue. No silent loss. |
| Follower full-sync + crash mid-load | AOF truncated **before** snapshot install; if process dies mid-install, restart gets the old snapshot + empty AOF (consistent). |

See [Separation of Concerns](separation-of-concerns.md) for how this fits alongside vex-sentinel (failover coordination), `redis_exporter` (metrics), and chaos testing.

For maximum durability, ensure periodic `BGSAVE` (e.g., via cron or application-level timer).
