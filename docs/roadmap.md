# Vision & Roadmap

[Back to README](../README.md)

---

## The goal

**Vex is the in-process database for LLM applications.** One binary where vector
search, knowledge-graph traversal, and key-value state share memory and the
Redis protocol — so the **vector → graph → KV** path behind GraphRAG and agent
memory runs in a single process with zero network hops, instead of stitching
together Redis + a vector DB + a graph DB + glue code.

```
GRAPH.VECSEARCH  →  GRAPH.TRAVERSE  →  GET
   (semantic)        (relationships)    (state)
   ── one fused path, shared memory, no network round-trips ──
```

The moat is exactly that fusion: no incumbent has in-process vector + graph + KV
behind the Redis protocol. Redis has no graph/vector; Pinecone/Qdrant have no
graph or KV; Neo4j has no vector + KV + speed.

---

## Design principles

1. **Primitives, not a framework.** Vex ships store / search / traverse building
   blocks. The *reasoning* — which embedding model to use, detecting
   contradictions, consolidating memories — stays in clients and frameworks
   (Mem0, Zep, LangChain). The one sanctioned edge exception is the optional
   [`vex-embed`](embedding.md) sidecar.
2. **Substrate as credibility, not as the pitch.** KV-beats-Redis and
   graph-beats-Memgraph ([Benchmarks](benchmarks.md)) are the proof the engine
   is real — the headline is what the fused path *enables*.
3. **Keep the core lean and microsecond-clean.** Blocking or heavyweight work
   (embedding inference, HTTP) stays off the event loop — hence `vex-embed` is a
   separate process, not in-core.
4. **Operationally Redis-shaped.** Drop-in RESP protocol, same horizontal
   scaling and deployment model. Fewer instances, and fewer *other* datastores.

---

## Status

Legend: **shipped** · _experimental_ · `roadmap`

### Shipped
- **KV** — Redis-compatible, multi-reactor, lock-free reads
- **Graph** — `TRAVERSE` / `PATH` / `WPATH` (CH-accelerated) / `NEIGHBORS`, typed hits, `GRAPH.COOCCUR`
- **Vector** — HNSW ANN per graph node, f16 mmap storage, persistent indexes
- **GraphRAG** — `GRAPH.RAG` (vector search + graph expansion, one call)
- **Semantic cache** — `CACHE.SEM*` (similarity hit, TTL, tag invalidation, stats)
- **Agent memory** — `MEMORY.*` (typed relations, composite-ranked recall, decay)
- **Pub/sub** — `SUBSCRIBE` / `PSUBSCRIBE` / `PUBLISH`, cross-worker delivery
- **Ops** — TLS, MULTI/EXEC, WATCH, BGSAVE/AOF, LRU eviction, leader/follower clustering

### Experimental
- **`vex-embed`** — embedding proxy: transparent RESP passthrough + an `EMBED`
  command today; per-command text→vector auto-rewrite is WIP, and it's
  plain-HTTP only so far.

### Roadmap (priority order)
1. **Finish embeddings** — `vex-embed` per-command auto-rewrite (so clients can
   pass text to `CACHE`/`MEMORY`/`SETVEC`) + a live integration test against a
   local embedder. Prerequisite for MCP.
2. **MCP server** — the flagship LLM-native surface: Claude / Cursor / agents use
   vex's primitives as tools directly (tools map 1:1 to RESP commands, so it
   stays primitives-only).
3. **Deepen the fused path** — `GRAPH.RAG FORMAT subgraph` (rich nodes+edges
   returns) and **hybrid filtered `VECSEARCH`** ("nearest vectors among nodes of
   type X reachable from Y") — the single most differentiated primitive.
4. **Pub/sub for LLM events** — complete it (`PUBSUB CHANNELS`/`NUMSUB`) and add
   the LLM-native primitive: publish change / invalidation events on
   `MEMORY` / `CACHE` / graph mutations (keyspace-notification style), so agents
   can react to memory updates and cache invalidation can fan out.

---

## Consciously deferred (hardening)

We're going breadth-first, so these known items are scheduled, not forgotten:

- **Internal-node isolation** — `CACHE`/`MEMORY` nodes currently live under the
  user's graph namespace, so they're counted in `GRAPH.STATS` and baked into
  snapshots. Fix: a reserved `gsys:` namespace (new features already adopt it).
- **Cache persistence policy** — should a semantic cache survive `BGSAVE`/restart,
  or come back cold? Undecided.
- **Typed memory fields** — numeric recall fields are parsed string→number on the
  hot path; a typed sidecar would avoid it at scale.
- **`vex-embed` hardening** — live integration test + HTTPS support.

---

_The canonical "where this is going" doc. If code and this page disagree, the
code wins — open an issue._
