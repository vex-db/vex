# Vex

**The memory database for AI agents.** Vector search, a knowledge graph, and
key-value state in one Redis-compatible binary — so an agent's long-term memory,
semantic cache, and session state live in *one* place instead of a vector DB +
a graph DB + Redis and the glue between them.

One process. One protocol (RESP — works with every Redis client). And on
identical hardware it's faster than both Redis *and* Dragonfly.

```bash
docker run -p 6380:6380 ghcr.io/pratyush-sngh/vex:latest --reactor
```

```
redis-cli -p 6380

# Give an agent a memory, then recall it by meaning — not exact text
> MEMORY.STORE agent:7 "user prefers dark mode and terse replies" IMPORTANCE 0.8
> MEMORY.RECALL agent:7 "what are the UI preferences?" K 3
1) "user prefers dark mode and terse replies"   # ranked by similarity·recency·importance

# Cache an LLM answer by what the question MEANS
> CACHE.SEMSET "how do I reset my password?" "Settings → Security → Reset."
> CACHE.SEMGET "i forgot my password, help" THRESHOLD 0.85
"Settings → Security → Reset."
```

The things an agent needs to remember are **first-class commands**, not a stack
you assemble.

---

## Why agents need this

An agent that remembers — past conversations, user preferences, a knowledge
base — needs three capabilities working *together*:

- **Semantic recall** — "what do I know that's *relevant* to this?" (vectors)
- **Relationships** — "what's connected to this?" (a knowledge graph)
- **State** — sessions, counters, flags (key-value)

The usual answer is a vector DB *and* Neo4j *and* Redis, kept in sync by code on
every request. Vex collapses that into one engine where they share memory:
`GRAPH.RAG` does a vector search **and** graph expansion in a single call, with
no network hops between steps.

| Your agent needs | Typical stack | With Vex |
|---|---|---|
| Long-term memory | vector DB + ranking code | `MEMORY.*` — ranked recall built in |
| Semantic cache | Redis + a vector DB | `CACHE.SEM*` |
| Knowledge / GraphRAG | vector DB + Neo4j + glue | `GRAPH.RAG` — search + traverse, one call |
| Session / KV state | Redis | KV (Redis-compatible) |

## The agent primitives

- **Agent memory** — `MEMORY.STORE / RECALL / RELATE / CONTEXT / DECAY`.
  Persistent memories with typed relationships and **composite ranking**
  (similarity · recency · importance · frequency), so recall surfaces what
  actually matters, and `DECAY` ages out the stale. → [Agent Memory](docs/agent-memory.md)
- **Semantic cache** — `CACHE.SEMSET / SEMGET`. Cache LLM responses by query
  *meaning* to cut tokens and latency on near-duplicate questions. Per-entry TTL
  + similarity threshold + tag invalidation. → [Semantic Cache](docs/semantic-cache.md)
- **GraphRAG** — `GRAPH.RAG`: vector ANN search + graph BFS expansion in one
  command; `GRAPH.COOCCUR` auto-links entities that co-occur. → [GraphRAG](docs/graphrag.md)
- **Vectors** — HNSW nearest-neighbor search per graph node, f16 mmap storage,
  persistent indexes. → [Vector Search](docs/vector-search.md)

Embeddings can run in-process via the optional `vex-embed` sidecar (text →
vectors via Ollama/OpenAI). An **MCP server** — so agents call vex's primitives
as tools directly — is on the [roadmap](docs/roadmap.md).

## It's also just a fast Redis

Vex speaks RESP, so `redis-cli`, redis-py, ioredis, go-redis — anything — just
works. And the substrate is genuinely fast:

- **Beats Dragonfly on identical hardware** (c6gn.16xlarge, 256B values,
  saturated): SET **+46%** unpipelined / **4.8×** pipelined; GET **+35%** / **6.2×**.
- **20–40% faster than Redis** pipelined; wins unpipelined past ~12 connections.
- **22× faster shortest-path than Memgraph.**

> Honest framing: unpipelined small-op throughput is *kernel-network-bound*, so
> vex and Dragonfly are close there (vex ahead) — the daylight is in pipelined
> and per-engine efficiency. We don't quote the "25× vs single-threaded Redis"
> multiple (any multi-core engine can). Full numbers + methodology:
> [Benchmarks](docs/benchmarks.md). Squeezing many-core boxes: [Tuning](docs/tuning.md).

---

## Setup & use

Two shapes, same binary: run Vex **local** (beside your app, over a Unix socket —
best for a personal project or single-machine agent) or **networked over TCP**
(a shared service with replication, TLS, and metrics — best for enterprise). See
**[Two ways to run Vex](docs/usage-patterns.md)**.

### Run

```bash
# Docker
docker run -p 6380:6380 ghcr.io/pratyush-sngh/vex:latest --reactor

# Or build from source (Zig 0.17)
zig build run -- --reactor
redis-cli -p 6380
```

### Configure

```bash
cat > vex.conf <<'EOF'
port 6380
reactor
workers 4               # min(cores, 8) by default; see Tuning for many-core boxes
maxmemory 256mb
maxmemory-policy allkeys-lru
EOF
zig build run
```

Precedence: `--flags` > `vex.conf` (or `VEX_CONFIG`) > defaults.
Full list: [Configuration](docs/configuration.md).

### A tiny RAG, end to end

```
# index a document node with an embedding + a topic edge
> GRAPH.ADDNODE doc:1 document
> GRAPH.SETVEC doc:1 embedding <f32_bytes>
> GRAPH.ADDNODE topic:ai topic
> GRAPH.ADDEDGE doc:1 topic:ai about

# one call: nearest docs to the query vector + 1-hop graph expansion
> GRAPH.RAG embedding <query_bytes> K 5 DEPTH 1 DIR OUT
1) 1) "doc:1"  2) "0.9523"  3) ("title" "Attention Is All You Need")  4) ("topic:ai")
```

Python pipelines and full walkthroughs: [Vector Search](docs/vector-search.md),
[Agent Memory](docs/agent-memory.md), [Semantic Cache](docs/semantic-cache.md).

### Compatibility

All five Redis data types (strings, lists, hashes, sets, sorted sets) + graph +
vector + the agent primitives. MULTI/EXEC, WATCH, pub/sub, 16 logical DBs.
Full command reference: [Commands](docs/commands.md).

---

## Production

Redis-shaped to operate: same client tooling, same horizontal scaling, small
pods. Plus TLS, snapshot + AOF persistence (group commit, `appendfsync`,
STOP-WRITE on disk-full), LRU eviction, and leader/follower clustering with
epoch-based failover. The operator surface (`INFO`, `SLOWLOG`, `LATENCY`,
`CLIENT LIST`) mirrors Redis 7, so `redis_exporter` works unmodified.
→ [Deployment](docs/deployment.md) · [Observability](docs/observability.md) · [Clustering](docs/clustering.md)

## Documentation

| | |
|---|---|
| **[Vision & Roadmap](docs/roadmap.md)** | The agent-first north star; shipped vs experimental vs roadmap |
| **[Commands](docs/commands.md)** | Full reference: KV, graph, vector, semantic cache, agent memory, pub/sub |
| **[Agent Memory](docs/agent-memory.md)** · **[Semantic Cache](docs/semantic-cache.md)** · **[GraphRAG](docs/graphrag.md)** | The agent primitives, in depth |
| **[Vector Search & GRAPH.RAG](docs/vector-search.md)** | HNSW search, f16 storage, RAG pipeline examples |
| **[Architecture](docs/architecture.md)** | System design, event loop, why it's fast, source layout |
| **[Benchmarks](docs/benchmarks.md)** · **[Tuning](docs/tuning.md)** | vs Dragonfly & Redis (methodology); every tuning knob, measured |
| **[Two ways to run Vex](docs/usage-patterns.md)** | Local (Unix socket, personal) vs networked (TCP, enterprise) |
| **[Configuration](docs/configuration.md)** · **[Deployment](docs/deployment.md)** | Flags, config file, env; production checklist |
| **[Persistence](docs/persistence.md)** · **[Security](docs/security.md)** · **[Clustering](docs/clustering.md)** | Snapshot/AOF; TLS/auth; replication & failover |
| **[Transactions](docs/transactions.md)** · **[Pub/Sub](docs/pubsub.md)** · **[Observability](docs/observability.md)** | MULTI/EXEC/WATCH; channels; INFO/SLOWLOG/LATENCY |
| **[Kernel-bypass (AF_XDP)](docs/af-xdp-design.md)** | Design sketch: going around the kernel net stack |
| **[Changelog](CHANGELOG.md)** · **[Testing](docs/testing.md)** | Release history; test suite & coverage |

## License

MIT
