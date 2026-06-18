# Two ways to run Vex

Vex is one binary with no dependencies, so it fits two very different shapes
without any code change — just how you connect to it.

- **Local** — Vex runs *next to* your app on one machine, talking over a Unix
  socket. Best for a personal project, a single-machine agent, a desktop app, or
  a prototype. Fastest path, nothing leaves the box, zero ops.
- **Networked (TCP)** — Vex runs as a *shared service* many apps connect to over
  the network, with replication, TLS, and metrics. Best for an enterprise
  deployment.

Same commands, same data model. Start local; graduate to networked when you
actually need to.

---

## Local — Vex beside your app

Your agent and its memory live on the same machine. Run the binary, connect over
a **Unix domain socket (UDS)**, and you skip the entire network stack — UDS is
**2–6× faster than TCP** for same-machine traffic ([benchmarks](benchmarks.md)),
and the socket never leaves the box, so there's nothing to secure.

```bash
# run it (Docker, or `zig build run -- ...`)
docker run -v /tmp/vex:/sock ghcr.io/pratyush-sngh/vex:latest \
  --reactor --unixsocket /sock/vex.sock --no-persistence
```

```python
# connect over the socket — same client, just a path instead of host:port
import redis
r = redis.Redis(unix_socket_path="/tmp/vex/vex.sock")
r.execute_command("MEMORY.STORE", "agent:me", "prefers dark mode", "VEC", emb(...))
```

A minimal `vex.conf` for a local agent:

```conf
reactor
unixsocket /tmp/vex/vex.sock
workers 4                 # min(cores, 8) by default — fine for a laptop
maxmemory 512mb
maxmemory-policy allkeys-lru
# persistence: pick one
#   no-persistence        → pure cache, nothing on disk (fast, ephemeral)
#   save + appendonly     → durable; survives restarts
```

**Use local when:** it's a personal project, a single-process agent, a CLI/desktop
app, CI, or anything where one machine holds both the app and its memory. You get
the lowest latency and the simplest operations (there's nothing to operate).

> Prefer plain `localhost:6380` TCP if you don't want to manage a socket path —
> it's slightly slower than UDS but works identically and is the easiest start.

---

## Networked (TCP) — Vex as a shared service

Now multiple app instances connect to one Vex over the network, so the concerns
shift to **availability, security, and scale**.

```bash
docker run -p 6380:6380 ghcr.io/pratyush-sngh/vex:latest \
  --reactor --workers 16 \
  --requirepass "$VEX_PASSWORD" \
  --tls-cert /certs/vex.crt --tls-key /certs/vex.key \
  --appendonly yes --appendfsync everysec
```

What you turn on for production:

- **Security** — `--requirepass` (constant-time auth) and TLS (`--tls-cert` /
  `--tls-key`, OpenSSL via dlopen, no build dependency). See [Security](security.md).
- **High availability** — leader + followers with epoch-based failover; read
  replicas scale reads. `VEX.PROMOTE` / `VEX.STATUS` drive failover (a
  `vex-sentinel` orchestrator is planned). See [Clustering](clustering.md).
- **Durability** — snapshot + AOF with `appendfsync everysec` (bounded loss) and
  STOP-WRITE on disk-full. See [Persistence](persistence.md).
- **Scale + tuning** — on a many-core box, set `--workers` to match cores, pick a
  **network-optimized instance** (more NIC RSS queues = higher unpipelined
  ceiling), and enable **RFS**. This is the difference between "fine" and "beats
  Dragonfly." See [Tuning](tuning.md).
- **Observability** — `INFO` / `SLOWLOG` / `LATENCY` / `CLIENT LIST` mirror
  Redis 7, so `redis_exporter` → Prometheus works unmodified. See
  [Observability](observability.md).

It stays Redis-shaped to operate: same client libraries, same horizontal
scaling, small pods. See [Deployment](deployment.md) for the production
checklist, systemd units, and Docker/k8s details.

**Use networked when:** several services share one dataset, you need failover or
read replicas, the data must be encrypted in transit, or you're running on a big
many-core box and want the throughput.

---

## At a glance

| | Local (personal) | Networked (enterprise) |
|---|---|---|
| Connect via | **Unix socket** (or localhost TCP) | **TCP** (bind + port) |
| Latency | lowest (no network stack) | network RTT |
| Security | none needed (on-box) | TLS + auth |
| Availability | single instance | leader/follower + failover |
| Scale | one machine | replicas + many-core tuning ([Tuning](tuning.md)) |
| Ops | run the binary | persistence, metrics, certs, cluster |
| Best for | agents, prototypes, desktop, CI | shared service, HA, big boxes |

The engine is identical in both — `MEMORY.*`, `CACHE.SEM*`, `GRAPH.RAG`, and KV
behave the same whether they're a function-call-distance away over a socket or a
hop away over TCP.
