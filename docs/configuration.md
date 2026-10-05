# Configuration

[Back to README](../README.md) | [Commands](commands.md) | [Security](security.md) | [Deployment](deployment.md)

---

Vex can be configured via CLI flags, config files, or environment variables.

## Precedence Order (highest to lowest)

1. **CLI flags** -- always win
2. **`--config <path>`** -- explicit config file
3. **`VEX_CONFIG` env var** -- path to config file
4. **`./vex.conf`** -- default config file in current directory (silently skipped if missing)
5. **Built-in defaults**

---

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--port`, `-p` | 6380 | Listen port |
| `--host`, `-h` | 0.0.0.0 | Bind address |
| `--reactor` | off | Enable multi-reactor mode (recommended for production) |
| `--workers N` | auto (CPU cores, max 8) | Worker threads for reactor mode |
| `--sorted-set-partitions N` | 256 | Shared command locks and list/set/sorted-set map partitions in reactor mode; power of two, 1–4096; startup only |
| `--data-dir`, `-d` | ./data | Persistence directory |
| `--no-persistence` | off | Disable AOF/snapshot entirely |
| `--requirepass` | none | Password for AUTH |
| `--maxclients` | 10000 | Max concurrent connections |
| `--max-client-buffer` | 1048576 | Max unparsed data per connection (bytes) |
| `--maxmemory` | 0 (unlimited) | Memory limit. Supports `kb`/`mb`/`gb` suffixes |
| `--maxmemory-policy` | noeviction | Eviction policy: `noeviction` or `allkeys-lru` |
| `--tls-cert` | none | TLS certificate file (PEM format) |
| `--tls-key` | none | TLS private key file (PEM format) |
| `--log-level` | info | Log verbosity: `debug`, `info`, `warn`, `error` |
| `--log-file PATH` | stderr | Path for structured log output. Falls back to stderr if open fails. |
| `--log-format` | text | `text` (default) or `json`. JSON emits one `{"ts","level","msg"}` object per line. |
| `--appendfsync` | everysec | AOF durability: `always` / `everysec` / `no`. See [Persistence](persistence.md#durability-modes-appendfsync). |
| `--enable-timings` | off | Time every command — populates `cmdstat_*` usec fields in INFO + drives SLOWLOG entries. ~1.5% hot-path cost when on. |
| `--slowlog-log-slower-than US` | 10000 | Microseconds threshold; commands longer than this go to per-worker SLOWLOG ring. |
| `--latency-monitor-threshold US` | 100000 | Microseconds threshold for LATENCY monitor events (fsync, snapshot, eviction). |
| `--config` | none | Path to config file |
| `--cluster-config` | none | Path to cluster config file |
| `--profile` | off | Enable latency profiling |
| `--profile-every N` | 100000 | Print profile every N commands |
| `--keys-mode` | strict | KEYS command mode: `strict` (disabled for large DBs) or `autoscan` |
| `--engine-threads N` | auto | Thread count for scaled mode |
| `--unixsocket path` | none | Unix socket path for connections (in addition to TCP) |

### Examples

```bash
# Minimal
zig build run -- --reactor --port 6380

# Production
zig build run -- --reactor --port 6380 \
  --requirepass secret \
  --maxmemory 2gb --maxmemory-policy allkeys-lru \
  --tls-cert cert.pem --tls-key key.pem \
  --log-level info

# Benchmarking
zig build run -- --reactor --port 7379 --no-persistence --workers 8
```

---

## Config File

**Format:** one `key value` pair per line, `#` for comments, blank lines ignored.

```conf
# /etc/vex/vex.conf

# Network
port 6380
host 0.0.0.0
reactor
workers 4

# Persistence
data-dir /var/lib/vex

# Security
requirepass mysecretpassword
tls-cert /etc/vex/cert.pem
tls-key /etc/vex/key.pem

# Memory
maxmemory 512mb
maxmemory-policy allkeys-lru
maxclients 10000

# Logging
loglevel info
log-file /var/log/vex/vex.log
log-format json

# Durability
appendfsync everysec

# Observability (default off; opt-in for prod with mild ~1.5% hot-path cost)
enable-timings yes
slowlog-log-slower-than 10000
latency-monitor-threshold 100000
```

### Config Key Reference

| Config Key | CLI Equivalent | Notes |
|------------|---------------|-------|
| `port` | `--port` | |
| `host` or `bind` | `--host` | Both aliases work |
| `data-dir` or `dir` | `--data-dir` | Both aliases work |
| `requirepass` | `--requirepass` | |
| `maxclients` | `--maxclients` | |
| `max-client-buffer` | `--max-client-buffer` | Bytes |
| `maxmemory` | `--maxmemory` | Supports `kb`/`mb`/`gb` suffixes |
| `maxmemory-policy` | `--maxmemory-policy` | `noeviction` or `allkeys-lru` |
| `reactor` | `--reactor` | Boolean flag (presence = enabled) |
| `workers` | `--workers` | |
| `sorted-set-partitions` | `--sorted-set-partitions` | Power of two, 1–4096; default 256 |
| `log-level` or `loglevel` | `--log-level` | Both aliases work |
| `log-file` or `logfile` | `--log-file` | Path; falls back to stderr if open fails |
| `log-format` or `logformat` | `--log-format` | `text` or `json` |
| `appendfsync` | `--appendfsync` | `always` / `everysec` / `no` |
| `enable-timings` | `--enable-timings` | Boolean (`yes`/`no` or implicit) |
| `slowlog-log-slower-than` | `--slowlog-log-slower-than` | Microseconds |
| `latency-monitor-threshold` | `--latency-monitor-threshold` | Microseconds |
| `tls-cert` | `--tls-cert` | |
| `tls-key` | `--tls-key` | |
| `keys-mode` | `--keys-mode` | `strict` or `autoscan` |
| `engine-threads` | `--engine-threads` | |
| `unixsocket` | `--unixsocket` | |
| `profile` | `--profile` | Boolean flag |
| `profile-every` | `--profile-every` | |

Unknown keys are silently ignored for forward compatibility.

## Runtime tuning via CONFIG SET

A subset of the keys above can be mutated at runtime without restart:

| Key | Effect |
|---|---|
| `log-level` | takes effect on next log emission |
| `latency-monitor-threshold` | takes effect on the next event |
| `appendfsync` | switches mode at runtime; joins/starts the background fsync thread as needed |

The rest accept `CONFIG SET` for client compatibility (returns `+OK`) but require a restart to actually take effect.

```
> CONFIG SET appendfsync always
+OK
> CONFIG GET appendfsync
1) "appendfsync"
2) "always"
```

See [Observability](observability.md) for the full CONFIG GET/SET surface.

### Config File Loading

Vex automatically loads `./vex.conf` from the current working directory on startup. No flag needed -- just place the file and run:

```bash
echo "port 6380\nreactor\nworkers 4" > vex.conf
zig build run
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `VEX_CONFIG` | Path to config file. Loaded after `./vex.conf`, before `--config` flag |

```bash
# Use env var for config
VEX_CONFIG=/etc/vex/production.conf zig-out/bin/vex --reactor

# Env var + CLI override (CLI wins)
VEX_CONFIG=/etc/vex/base.conf zig-out/bin/vex --port 7380
```

---

## Memory Size Format

The `--maxmemory` flag and `maxmemory` config key accept human-readable sizes:

| Input | Bytes |
|-------|-------|
| `1024` | 1,024 |
| `64kb` | 65,536 |
| `256mb` | 268,435,456 |
| `1gb` | 1,073,741,824 |
| `256MB` | 268,435,456 (case-insensitive) |

See [Memory Management](memory.md) for eviction policy details.

### Sorted-set partition tuning

`--reactor --workers 8 --sorted-set-partitions 512` starts eight workers with
512 shared command lock partitions. List and set maps use the same partition
count and key hash, allowing single-key collection operations and expiry cleanup
to run under one lock. Hash maps retain their internal stripe locks as well.
The equivalent config entry is
`sorted-set-partitions 512`; CLI values override config values. Invalid or missing
counts fail startup. The count is fixed for the lifetime of the store and logged
with the reactor worker count. It does not automatically follow CPU count.

Keep 256 unless a representative benchmark supports another count. More partitions
can reduce collisions between unrelated keys, but do not parallelize
access to a single hot key. They also use more memory and require more lock
operations for global commands such as FLUSHALL and EXEC. The historical option
name is retained; it now also sizes the reactor list and set map partitions.

### Experimental fixed key owner

For diagnostic use only, `VEX_EXPERIMENTAL_OWNER_KEY=lb:0` routes ordinary
sorted-set commands for that exact key in DB 0 to reactor worker 0. It requires
`--no-persistence` and no replication configuration. The default is disabled;
the sorted-set partition default remains 256.

The prototype forwards consecutive complete RESP commands in batches of up to
32. It waits for the owner before processing more commands on the originating
worker, preserving request-buffer lifetime and per-connection reply order.
That wait can delay unrelated connections assigned to the same worker. Inline
and larger commands use the same owner individually. Transactions, global
commands, other databases and other keys retain their existing execution paths
and locks; this is not an exclusive lock-free ownership scheme.

This setting does not pin CPUs, detect hot keys, migrate ownership, or provide
general key-based sharding. Evaluate throughput, CPU cost and cold-key latency
before using it beyond a disposable benchmark.

### Experimental adaptive key owner

`VEX_EXPERIMENTAL_ADAPTIVE_OWNER=1` enables sampled hot-key detection and
asynchronous routing to worker 0. It is disabled by default, mutually exclusive
with `VEX_EXPERIMENTAL_OWNER_KEY`, and requires reactor workers, no persistence,
no replication and no TLS. `0` disables it; other values are rejected.

The first prototype tracks one dominant sorted-set key in DB 0, up to 128 bytes.
Workers sample roughly one in 64 sorted-set commands. Activation requires three
consecutive 200 ms windows with at least 128 samples, at least 60% candidate
share, at least 10% contended candidate samples and average candidate lock wait
of at least 500 ns. These are experimental thresholds, not an automatic sizing
formula. The sorted-set partition default remains 256.

Ownership lasts at least one second. Three consecutive windows with fewer than
128 samples, less than 20% active-key share, or average sampled owner queue wait
over 5 ms release it, followed by a two-second cooldown. Decisions advance when
samples arrive; an idle server does not run a separate policy timer.

Requests are copied and batched, with at most 32 commands per batch. Only the
requesting connection waits for its reply; unrelated connections on its worker
can continue. Queued requests are limited to 1,024 batches and 16 MiB of payload;
these limits exclude completed replies. Allocation or queue-limit failures fall
back to ordinary execution. Existing partition locks, transaction handling and
WATCH invalidation remain authoritative across routing changes. This prototype
does not provide multiple simultaneous owners, choose a less busy worker, or
make execution lock-free. Benchmark uniform traffic, hot/cold transitions and
cold-client latency before considering a production default.

## Reactor file descriptors

On Linux, each worker's descriptor table grows on demand when registering a
higher file descriptor. There is no fixed 4,096-entry table limit. Lookup stays
constant-time; registration may allocate memory, and allocation failure closes
the new connection without discarding existing registrations. The table keeps
its allocated capacity until the worker exits.

The operating system's file-descriptor limit (`ulimit -n`) and available memory
still bound concurrent connections. Configure the process/container limit high
enough for client sockets plus listeners, notification descriptors, and storage
files. Table growth does not raise that operating-system limit automatically.
