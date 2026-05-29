# Observability

[Back to README](../README.md) | [Configuration](configuration.md) | [Clustering](clustering.md) | [Separation of Concerns](separation-of-concerns.md)

---

Everything an operator needs to answer "what is vex doing right now?" — without attaching a debugger, without restarting, without external agents (other than a Prometheus scraper if you want one).

Field names mirror Redis 7's, so any tooling that talks to Redis (`redis-cli`, `redis_exporter`, Grafana dashboards, Datadog Redis integration, etc.) talks to vex unchanged.

---

## Quick map

| Need | Command / surface |
|---|---|
| Everything-in-one-page status | `INFO` |
| Slow commands | `SLOWLOG GET / LEN / RESET` |
| Slow operational events (fsync, snapshot, eviction) | `LATENCY LATEST / HISTORY / DOCTOR / RESET` |
| Who's connected | `CLIENT LIST / INFO / GETNAME / SETNAME / ID` |
| Key metadata | `DEBUG OBJECT <key>`, `MEMORY USAGE <key>` |
| Process memory snapshot | `MEMORY STATS` |
| Per-knob inspection / runtime tuning | `CONFIG GET *`, `CONFIG SET <key> <val>` |
| Cluster role + epoch + lag | `VEX.STATUS`, `INFO Replication` |
| Structured logs to a file / log shipper | `log-file`, `log-format json` config |
| Prometheus metrics | run `redis_exporter` alongside vex (see below) |

---

## INFO surface

`INFO` returns 11 sections, ~50 fields. Equivalent in field-name to Redis 7 wherever possible.

```
# Server
vex_version:0.7.4
engine:csr_soa_v2
os:linux
arch:x86_64
process_id:42
uptime_in_seconds:3412
tcp_port:6380

# Clients
connected_clients:7

# Keyspace
kv_keys:12345
kv_with_ttl:300
kv_tombstones:42
db_selected:0
db_max:16

# Graph
graph_nodes:1024
graph_edges:5678
graph_types:8
graph_delta_edges:0
graph_needs_compact:0

# Memory
used_memory_rss:91234304
maxmemory:1073741824
maxmemory_policy:allkeys-lru

# Persistence
aof_enabled:1
aof_current_size:182394
aof_buffer_length:0
aof_fsync_mode:everysec
aof_last_fsync:1779648058
aof_last_write_status:ok
last_save_time:1779647990

# CPU
used_cpu_user:14.231
used_cpu_sys:3.917

# Replication
role:master                              (or: role:slave / role:standalone)
connected_slaves:2
master_repl_offset:50231
master_replid:0000000000000000000000000000000000000000
cluster_epoch:7
slave0:addr=10.0.0.2:6380,offset=50231,applied=50230,lag_seq=1,lag_sec=0
slave1:addr=10.0.0.3:6380,offset=50231,applied=49998,lag_seq=233,lag_sec=4

# Cluster
graph_mutation_seq:12345

# Stats
total_commands_processed:9341822
total_connections_received:7
rejected_connections:0
total_net_input_bytes:0
total_net_output_bytes:0
total_error_replies:0
evicted_keys:0
expired_keys:142

# Commandstats          (only meaningful when enable-timings=true)
cmdstat_GET:calls=4523112,usec=0,usec_per_call=0,rejected_calls=0,failed_calls=0
cmdstat_SET:calls=1245533,usec=0,usec_per_call=0,rejected_calls=0,failed_calls=0
...
```

**Per-command call counts** are always populated (zero-cost per-worker single-owner increments). **Latency fields** (`usec`, `usec_per_call`) populate only when `enable-timings=true` (a configurable opt-in; default off to preserve bench numbers).

See [Configuration](configuration.md) for `enable-timings`.

---

## SLOWLOG

Redis-compatible. Per-worker bounded ring buffer (128 entries each, aggregated newest-first at read time). Only commands taking longer than `slowlog-log-slower-than` (microseconds) are recorded.

```
SLOWLOG LEN           -> integer total across all workers
SLOWLOG GET [count]   -> last N entries, newest first
                          each entry: [id, ts_sec, duration_us, [cmd, args...]]
SLOWLOG RESET         -> +OK; clears all rings
SLOWLOG HELP          -> array of help lines
```

Example:

```
127.0.0.1:6380> CONFIG SET enable-timings yes
OK
127.0.0.1:6380> CONFIG SET slowlog-log-slower-than 1000
OK
127.0.0.1:6380> DEBUG SLEEP 0.05
OK
127.0.0.1:6380> SLOWLOG GET 1
1) 1) (integer) 1
   2) (integer) 1779648750
   3) (integer) 50321
   4) 1) "DEBUG"
      2) "SLEEP"
      3) "0.05"
```

Gating: requires `enable-timings=true`. With it off, nothing is recorded (zero hot-path overhead).

---

## LATENCY monitor

For events that aren't commands — fsync stalls, snapshot saves, AOF rewrites, eviction sweeps. SLOWLOG sees per-command time; LATENCY catches the "vex hiccupped for 200ms because the disk paused" cases.

Five event kinds tracked:

| Kind | When it fires |
|---|---|
| `aof-fsync` | wraps `AOF.flush` |
| `aof-rewrite` | wraps `AOF.rewriteFromState` |
| `snapshot-save` | wraps `snapshot.save` |
| `snapshot-load` | wraps `snapshot.load` (startup + follower full-sync) |
| `eviction-cycle` | wraps the LRU evict loop (only when an eviction is actually needed) |

```
LATENCY LATEST                      -> array of [event, ts_sec, latest_ms, peak_ms]
LATENCY HISTORY <event>             -> ring of [ts_sec, duration_ms] (newest first)
LATENCY RESET [event ...]           -> clear one, several, or all rings
LATENCY DOCTOR                      -> human-readable summary
LATENCY HELP
```

Threshold-gated: events shorter than `latency-monitor-threshold` (microseconds, default 100ms) are dropped. Lower the threshold to capture more events.

```
127.0.0.1:6380> CONFIG SET latency-monitor-threshold 1
OK
127.0.0.1:6380> SAVE
OK
127.0.0.1:6380> LATENCY LATEST
1) 1) "snapshot-save"
   2) (integer) 1779648900
   3) (integer) 2
   4) (integer) 2
2) 1) "aof-fsync"
   ...
```

---

## CLIENT LIST / INFO

Process-wide registry of active connections. Aggregated across all workers via brief mutex (rare command, no hot-path cost).

```
CLIENT LIST    -> one Redis-shaped line per connection
CLIENT INFO    -> same line for the calling connection
CLIENT ID      -> integer client id
CLIENT GETNAME / SETNAME <name>
```

Line format:
```
id=N addr=ip:port fd=N name=... age=N idle=N flags=N|P|x db=N qbuf=N obl=N cmd=NAME
```

Flags: `N` = normal, `P` = pubsub, `x` = in MULTI.

```
127.0.0.1:6380> CLIENT LIST
id=1 addr=127.0.0.1:54338 fd=10 name=alpha age=15 idle=0 flags=N db=0 qbuf=0 obl=0 cmd=CLIENT
id=2 addr=127.0.0.1:54339 fd=11 name=beta age=10 idle=10 flags=N db=0 qbuf=0 obl=0 cmd=CLIENT
id=3 addr=127.0.0.1:54355 fd=12 name=gamma age=5 idle=5 flags=N db=0 qbuf=0 obl=0 cmd=CLIENT
```

---

## DEBUG

```
DEBUG OBJECT <key>   -> Value at:0x0 refcount:N encoding:embstr|raw serializedlength:N lru:0 lru_seconds_idle:0
DEBUG SLEEP <secs>   -> +OK after sleeping (capped at 60s)
DEBUG HELP
```

`DEBUG SLEEP` is useful for testing `SLOWLOG` + `LATENCY` end-to-end.

---

## MEMORY

```
MEMORY USAGE <key>      -> integer bytes (value + small overhead)
MEMORY STATS            -> flat key/value RESP array (peak.allocated, clients.normal,
                           aof.buffer, keys.count, uptime.ms, etc.)
MEMORY HELP
```

---

## CONFIG GET / SET — 13 runtime-tunable knobs

```
CONFIG GET *                                -> all known config keys + their live values
CONFIG GET appendfsync                      -> appendfsync\neverysec
CONFIG SET latency-monitor-threshold 50000  -> +OK (atomically mutates)
```

Keys exposed:

| Key | Live mutable? | Notes |
|---|---|---|
| `maxmemory` | no | requires restart |
| `maxmemory-policy` | no | requires restart |
| `maxclients` | no | requires restart |
| `appendonly` | no | implicit (`--no-persistence` flag) |
| `save` | n/a | always returns empty |
| `databases` | n/a | fixed at 16 |
| `log-level` | yes | takes effect on next log emission |
| `log-file` | no | requires restart |
| `log-format` | no | requires restart |
| `enable-timings` | no | requires restart |
| `slowlog-log-slower-than` | no | requires restart |
| `latency-monitor-threshold` | yes | takes effect on next event |
| `appendfsync` | yes | switches mode at runtime, joins/starts background thread as needed |

The mutable ones use atomic globals (no thread coordination needed). The non-mutable ones return OK on SET for client compatibility but log a warning.

---

## VEX.PROMOTE / VEX.STATUS (cluster admin)

Cluster-mode admin commands used by `vex-sentinel`. See [Clustering](clustering.md).

```
VEX.STATUS               -> flat map: role, epoch, repl_offset, connected_slaves
VEX.PROMOTE <epoch>      -> +OK; validates epoch > current, persists vex.epoch atomically
```

---

## Structured logging

`log-format json` produces one JSON object per line:

```json
{"ts":"2026-05-24T11:36:45Z","level":"INFO","msg":"vex v0.7.4 ready: port=17999 kv_keys=0 graph_nodes=0 aof_replayed=0"}
{"ts":"2026-05-24T11:36:46Z","level":"WARN","msg":"repl-leader: broadcast to fd=14 failed (ConnectionClosed); closing follower"}
```

Configure via:

```
# vex.conf
log-file /var/log/vex/vex.log
log-format json
log-level info
```

Then point Vector / Fluentbit / promtail at `/var/log/vex/vex.log`. See [Separation of Concerns](separation-of-concerns.md#log-shipping-vector--fluentbit--promtail) for example.

---

## Prometheus — via `redis_exporter` (today)

Vex's INFO field names mirror Redis 7. Run `redis_exporter` against vex unmodified:

```bash
redis_exporter -redis.addr=redis://vex-host:6380 -web.listen-address=:9121
```

Grafana dashboards designed for `redis_exporter` populate against vex. Per-command stats need `enable-timings=yes` set on vex for non-zero usec values.

What Vex-specific metrics aren't exposed via `redis_exporter`?

- `aof_fsync_mode`, `aof_last_fsync`, `aof_last_write_status` (Vex-specific)
- `cluster_epoch` (Vex-specific)
- Per-follower `applied`, `lag_seq` (Vex-specific)
- Graph counters (`graph_nodes`, `graph_edges`, etc.)

For these, either:

1. Use `redis_exporter -include-system-metrics=false -script <path>` with a small custom script that polls `INFO` and emits these as additional gauges.
2. Wait for **native `/metrics` via Zig Prom library** (planned post-stability, see roadmap).

Either way, the data is there in `INFO` — only the transport is what varies.

---

## Performance guarantees

| Knob | Default | Hot-path cost when off | Cost when on |
|---|---|---|---|
| `enable-timings` | off | 0% (single-owner counter increment only, no clock_gettime) | ~1.5% (two `clock_gettime` calls per command) |
| `slowlog-log-slower-than` | 10000 (10ms) | n/a (only fires when `enable-timings=yes`) | 0% (only on slow commands) |
| `latency-monitor-threshold` | 100000 (100ms) | n/a (events fire on rare ops only) | 0% (only on threshold-exceeding events) |
| `appendfsync` | `everysec` | 0% (background thread) | `always`: ~5ms per flush on rotational, ~50µs on NVMe |

Default config = 0% hot-path overhead vs. before observability shipped. Bench numbers unchanged.

---

## What's not yet observable

- **Per-key access stats** (Redis's `OBJECT FREQ` for LFU). Not implemented.
- **Per-database stats**. Single global counters only.
- **MONITOR command** (live command stream). Intentionally deferred — typically costs 10-30% throughput while active and operators rarely use it in prod (SLOWLOG + LATENCY cover the diagnostic need).
- **Real structured logging with key/value pairs** instead of formatted strings. JSON format escapes the formatted message; full structured fields would require an API change at every call site.
