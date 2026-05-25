# Clustering

[Back to README](../README.md) | [Configuration](configuration.md) | [Deployment](deployment.md) | [Separation of Concerns](separation-of-concerns.md)

---

## Overview

Vex supports leader/follower replication for read scaling and data safety. One leader accepts all writes and broadcasts mutations to followers via a binary VX protocol.

**Cluster mode is split between mechanism (in `vex`) and policy (in `vex-sentinel`).** The vex binary implements the wire protocol, epoch enforcement, non-blocking broadcast, ack frames, and atomic full-sync. The separate `vex-sentinel` binary (planned, see [Separation of Concerns](separation-of-concerns.md)) drives failover decisions — who promotes, when, quorum across multiple sentinels. Until vex-sentinel ships, failover is **manual** via the `VEX.PROMOTE <epoch>` admin command.

---

## Quick Start

```bash
# Start a 3-node cluster
docker compose -f docker-compose.cluster.yml up --build -d

# Write to leader (port 16380)
redis-cli -p 16380 SET hello world

# Read from any follower (replicated)
redis-cli -p 16381 GET hello    # "world"
redis-cli -p 16382 GET hello    # "world"

# Writes on followers are forwarded to leader automatically
redis-cli -p 16381 SET fromfollower value
redis-cli -p 16380 GET fromfollower  # "value" (on leader)
redis-cli -p 16382 GET fromfollower  # "value" (replicated)
```

---

## Cluster Configuration

Create a `cluster.conf` file:

```
node 1 leader 10.0.0.1:6380
node 2 follower 10.0.0.2:6380
node 3 follower 10.0.0.3:6380
self 1
```

Each node runs with:
```bash
zig build run -- --reactor --cluster-config cluster.conf
```

The `self` line identifies which node this instance is. The role (leader/follower) determines behavior.

---

## Leader/Follower Replication

### Leader Behavior
- Accepts all write commands directly
- Broadcasts every mutation to connected followers via binary VX protocol on port + 10000
- Provides full snapshots to new followers on connect
- Sends heartbeats every 5s with `mutation_seq` for lag tracking

### Follower Behavior
- Serves read commands locally (no leader round-trip for GET, EXISTS, etc.)
- Forwards write commands to leader transparently (client sees the response as if local)
- Receives mutation stream from leader and replays locally
- Requests full sync (snapshot) on initial connection

### Write Forwarding

When a follower receives a write command:
1. Encodes the command as a binary `write_forward` frame
2. Sends it to the leader via a persistent TCP connection
3. Leader executes the command, sends RESP response back
4. Follower returns the response to the client

The client doesn't know it's connected to a follower -- writes work transparently.

---

## Epoch Mechanism

Every leader generation has a monotonically-increasing **epoch** (u64) persisted to `<data_dir>/vex.epoch` via atomic write+fsync. The epoch is carried in every heartbeat frame.

**Followers** reject heartbeats from a stale leader (`hb.epoch < current_epoch`), preventing an old leader that comes back after a partition from continuing to apply writes. When a follower sees a higher epoch, it adopts the new value.

**Old leaders** receiving a heartbeat with a higher epoch self-demote — stop broadcasting, become a follower of the new leader.

This is the **split-brain prevention mechanism**. It does not perform leader election — it just enforces that, once an epoch has been declared, lower-epoch frames are rejected. The decision of *which* node should bump the epoch is the orchestrator's responsibility.

```
Before failover:        Network partition:       After heal:
  N1 (LEADER, epoch=5)    N1 (alone, epoch=5)      N1 sees N2's heartbeat at epoch=6
  N2 (follower)            N2 (LEADER, epoch=6) ←   → self-demotes to follower
  N3 (follower)            N3 (follower of N2)      → reconnects to N2 for full sync
```

## Promotion

Two paths exist for incrementing the epoch and starting leader role:

### Automatic (legacy, in vex)

Implemented today. On heartbeat timeout, the highest-priority surviving follower (lowest node ID) auto-promotes:

1. Bumps `current_epoch` and persists `vex.epoch` atomically
2. Closes forward connection to the (presumed-dead) old leader
3. Starts a new `ReplicationLeader` on its replication port
4. Begins broadcasting mutations at the new epoch

This is unsafe under network partition (two followers may both think they're the highest surviving priority). Will be deprecated when vex-sentinel ships.

### Manual / sentinel-driven (recommended)

Operator (or `vex-sentinel`) calls the new admin command:

```
> VEX.PROMOTE 7
OK
```

The command:
1. Validates `7 > current_epoch` (rejects stale)
2. Atomically writes `7` to `vex.epoch`
3. Updates the in-memory `current_epoch` atomic
4. (Note: starting `ReplicationLeader` and updating `current_leader_ptr` is still the responsibility of the existing main.zig path — VEX.PROMOTE only persists the epoch. vex-sentinel will drive both together in a follow-on PR.)

`vex-sentinel` will coordinate the epoch bump across multiple nodes via quorum, picking the new leader by priority + applied-seq lag.

## VEX.STATUS — sentinel's health poll

```
> VEX.STATUS
1) "role"
2) "follower"
3) "epoch"
4) (integer) 7
5) "repl_offset"
6) (integer) 12453
7) "connected_slaves"
8) (integer) 0
```

Returns a flat key/value map. `vex-sentinel` polls each node every ~1s and uses this to detect failures + pick promotion targets.

---

## Replication Protocol (VX v2)

Binary frame-based protocol on port + 10000.

| Frame Type | Direction | Content |
|------------|-----------|---------|
| `heartbeat` | Leader → Follower | epoch (u64) + mutation_seq (u64) + timestamp (i64). v1 (16B, no epoch) accepted for one-version back-compat. |
| `repl_request` | Follower → Leader | Request replication stream from seq N (u64) |
| `repl_data` | Leader → Follower | Forwarded write command bytes for follower to replay |
| `full_sync_data` | Leader → Follower | Complete snapshot bytes (used on initial connect or after divergence) |
| `write_forward` | Follower → Leader | Write command that arrived at the follower; leader executes |
| `write_forward_response` | Leader → Follower | RESP response from the forwarded write |
| `repl_ack` | Follower → Leader | follower's applied_seq (u64) + epoch (u64) — emitted on every heartbeat received |

### Frame Format

```
MAGIC ("VX", 2 bytes)
TYPE (1 byte)
LENGTH (u32, 4 bytes, little-endian)
PAYLOAD (LENGTH bytes)
```

### Per-frame epoch enforcement

The heartbeat payload carries `epoch`. Followers reject heartbeats where `epoch < current_epoch`. Higher-epoch heartbeats cause the follower to advance its epoch and (if it was a leader) self-demote.

The `repl_ack` payload carries the follower's `applied_seq` + the epoch it was applied under. Leaders use this to compute true lag in seq units (`leader.mutation_seq - last_ack_seq`) and to detect followers stuck at a stale epoch.

## Non-blocking broadcast

Until v0.8, `broadcastMutation` held a single leader-wide mutex while calling blocking `writeFrame` to each follower fd. One slow follower would stall the broadcast for all others.

Current implementation:

- Each follower has a `FollowerState` with a bounded outbox queue (1024 frames cap), its own mutex, its own condition variable, and a **dedicated drain thread**.
- `broadcastMutation` snapshots the followers list under the leader mutex, releases the lock, then enqueues each frame to each follower's outbox under that follower's outbox mutex. Returns immediately.
- A drain thread per follower pops items and calls `writeFrame` directly. Blocks only on its own socket — one slow follower never starves the others.
- Outbox-full triggers follower disconnection: the broadcast logs a warning and marks the follower for reaping; reap removes it from the followers list and joins the drain thread.

INFO surfaces `slave_N:outbox=N` when there's pending data (visible in `INFO Replication` per-follower lines).

## Per-follower lag tracking

Each `FollowerState` records:
- `last_ack_seq` — most recent applied_seq from this follower's repl_ack
- `last_ack_ts_ms` — wall-clock timestamp when we received that ack
- `last_ack_epoch` — the epoch the follower had when it sent that ack

The leader exposes per-follower lines in `INFO Replication`:

```
slave0:addr=10.0.0.2:6380,offset=50231,applied=50230,lag_seq=1,lag_sec=0
slave1:addr=10.0.0.3:6380,offset=50231,applied=49998,lag_seq=233,lag_sec=4
```

`lag_seq = master.mutation_seq - applied`. `lag_sec = now - last_ack_ts`.

## Follower full-sync atomicity

When a follower receives `full_sync_data` from the leader, the sequence is ordered so that a crash at any point leaves vex consistent on restart:

1. **Truncate local AOF first.** If we crash between truncate and snapshot install, restart sees an empty AOF on top of the old `vex.zdb`. We lose the brief delta but no stale records replay over fresh state.
2. **Atomic-write `vex.zdb`** via `atomic_io.atomicWrite` (the tmp+fsync+rename+dir-fsync pattern). After this point `vex.zdb` is durably the new content.
3. **Load the new snapshot** into the in-memory KV + Graph.

If the process dies between step 2 and 3, restart reads the new `vex.zdb` correctly. Combined with the truncated AOF, the follower comes back with exactly the leader's state at the moment of full-sync.

---

## Consistency Model

| Property | Guarantee |
|----------|-----------|
| Read consistency on leader | Strong (read-your-own-writes) |
| Read consistency on followers | Eventual (replication lag visible in `lag_seq`) |
| Write ordering | Total order (leader serializes all writes) |
| Replication lag | Typically < 1 heartbeat interval (5s); precisely measured via repl_ack |
| Split-brain protection | Epoch monotonicity. Rejoining old leaders self-demote on seeing higher epoch. Quorum decisions deferred to vex-sentinel. |
| Crash recovery | Full-sync is atomic with AOF truncate + atomic snapshot install. No partial state across restart. |

---

## Monitoring

`INFO Replication` and `VEX.STATUS` are the operator surface. See [Observability](observability.md).

```
127.0.0.1:6380> INFO | grep -A 6 "# Replication"
# Replication
role:master
connected_slaves:2
master_repl_offset:50231
cluster_epoch:7
slave0:addr=10.0.0.2:6380,offset=50231,applied=50230,lag_seq=1,lag_sec=0
slave1:addr=10.0.0.3:6380,offset=50231,applied=49998,lag_seq=233,lag_sec=4
```

For Prometheus, point `redis_exporter` at the cluster nodes — the field names match Redis's and most metrics will scrape. Vex-specific fields (`cluster_epoch`, per-follower `applied`/`lag_seq`) need a custom scrape until the native `/metrics` ships.

---

## Limitations

- **No sharding**: all data lives on all nodes (full replication)
- **No synchronous writes**: writes succeed on leader alone, then replicate async (`WAIT` returns follower count but doesn't enforce a quorum)
- **Single leader at a time**: epoch enforcement prevents two leaders from accepting concurrent writes, but the cluster can have at most one active leader by design
- **Eventual consistency on followers**: lag is now precisely measurable via `lag_seq`; tune your application accordingly
- **Auto-promotion is unsafe under partition**: until vex-sentinel ships, use `VEX.PROMOTE <epoch>` manually for production failover
