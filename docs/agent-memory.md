# Agent Memory

[Back to README](../README.md) · [Semantic Cache](semantic-cache.md) · [GraphRAG](graphrag.md) · [Commands](commands.md)

---

An agent that remembers needs more than a pile of embeddings. It needs to know
that "user switched to light mode" **contradicts** "user prefers dark mode," that
the switch came **after**, and that a stated preference matters more than an
offhand remark. Dump everything into a vector DB and you lose all of that —
similarity search returns three vectors with no notion of order, weight, or how
they relate.

Vex's `MEMORY.*` commands give an agent **persistent, ranked, related** memory in
one engine: vectors for fuzzy recall, graph edges for relationships, KV for fast
lookup — and a recall score that blends *similarity · recency · importance ·
frequency* so the memory that actually matters surfaces first.

```
> MEMORY.STORE agent:alice "User prefers dark mode" TYPE semantic IMPORTANCE 0.7 VEC <f32_bytes>
"mem:agent:alice:1"
> MEMORY.RECALL agent:alice <query_f32_bytes> LIMIT 5 THRESHOLD 0.5
1) 1) "mem:agent:alice:1"  2) "0.87"  3) "User prefers dark mode"  ...
```

Embeddings are passed as **raw little-endian f32 bytes** (`VEC <bytes>` /
`RECALL agent <bytes>`) — the same wire format as the rest of vex's vector
commands. You embed the text; vex stores, indexes, ranks, and relates.

## Why not just a vector DB

```
Vector DB:                            Vex:
  "likes Python"   → store              MEMORY.STORE (semantic, importance 0.6)
  "prefers Rust"   → store              MEMORY.STORE (semantic, importance 0.8)
  "deployed v2.1"  → store              MEMORY.RELATE prefers-Rust --contradicts--> likes-Python
  recall: 3 vectors, no structure       MEMORY.STORE (episodic, auto-timestamped)
  can't tell: contradiction? order?     recall: ranked by similarity·recency·importance,
            what's important?                    with the contradiction one hop away
```

Three storage paradigms, one engine, no glue:

| Paradigm | Stores | In vex |
|---|---|---|
| **Vector** | meaning, for fuzzy recall | shared HNSW index, filtered per agent |
| **Graph** | relationships between memories | typed, weighted CSR edges |
| **KV** | fast lookup by known ID | direct key access + TTL |

**Memory types** (`TYPE`, default `episodic`): `episodic` (events — "deployed to
staging on May 11"), `semantic` (facts/preferences — "prefers Zig over Rust"),
`procedural` (workflows — "on 'deploy', run tests first").

## Commands

| Command | Description |
|---|---|
| `MEMORY.STORE agent text [opts]` | Store a memory (+ optional embedding); returns its id |
| `MEMORY.RECALL agent <f32_bytes> [opts]` | Recall by similarity, ranked by composite score |
| `MEMORY.RELATE agent a b reltype [WEIGHT w]` | Typed, weighted edge between two memories |
| `MEMORY.CONTEXT agent id [DEPTH n] [TYPES…]` | A memory + its related subgraph |
| `MEMORY.DECAY agent [PRUNE thr] [DRY_RUN]` | Age out / prune low-score memories |
| `MEMORY.LIST / GET / DEL` | List with filters · fetch one · delete (+ edges) |

Canonical signatures: [Commands](commands.md).

### MEMORY.STORE

```
MEMORY.STORE <agent> <text>
    [ID <id>]            # default mem:<agent>:<counter>
    [TYPE episodic|semantic|procedural]
    [IMPORTANCE 0.0-1.0]  # default 0.5; higher decays slower in ranking
    [VEC <f32_bytes>]     # raw little-endian f32; omit → recall by id/type/graph only
    [SOURCE <id>]         # provenance; stored as a SOURCED_FROM edge
    [TTL <secs>]          # hard expiry
    [HALFLIFE <secs>]     # recency half-life; default 604800 (7d)
    [TAG <tag>]
```

Under the hood: a `memory` graph node with the text + metadata properties; if a
vector is given it goes into the shared `__memory__` HNSW field tagged with the
agent, so recall can filter by agent without a per-agent index.

### MEMORY.RECALL

```
MEMORY.RECALL <agent> <f32_bytes>
    [LIMIT n]            # default 10
    [THRESHOLD 0.0-1.0]  # min similarity, default 0.5
    [TYPE …] [TAG …] [AFTER <ts>] [BEFORE <ts>]
    [BOOST recency|importance|frequency]   # square the selected factor
```

**Composite score** combines semantic similarity with memory metadata:

```
score = similarity · recency · importance · frequency
  similarity = cosine(query, memory)
  recency    = 0.5 ^ (age / halflife)
  importance = the stored weight
  frequency  = 0.5 + 0.5·min(1, log2(access_count+1)/10)
```

The `frequency` factor has a **0.5 floor** so a never-accessed-yet memory doesn't
score zero (the bare `log2` form would zero every fresh memory). Reply per hit:
`[id, score, text, type, similarity, recency, importance, created_at]`.

`BOOST` squares the selected factor; it does not multiply it by two. For
example, `BOOST importance` uses `similarity · recency · importance² · frequency`.
For factors between zero and one, this lowers the numerical score while giving
that factor more influence on the ranking. Evaluate ranking quality for your
workload; composite scoring is not universally better than similarity alone.

### MEMORY.RELATE / CONTEXT

`RELATE` draws a typed, weighted edge between two memories —
`supports`, `contradicts`, `updates`, `causes`, `extends`, or any custom string.
`CONTEXT` returns a memory plus everything within `DEPTH` hops (optionally
filtered to certain relation `TYPES`) — the agent sees not just a fact but its
history and conflicts.

```
> MEMORY.RELATE agent:alice mem:agent:alice:5 mem:agent:alice:1 contradicts WEIGHT 0.9
OK    # "switched to light mode" contradicts "prefers dark mode"
```

### MEMORY.DECAY

Old memories should fade without a cron job. `DECAY` recomputes the
non-similarity score (`importance · recency · frequency`) and optionally prunes
anything below a threshold; `DRY_RUN` previews first.

```
> MEMORY.DECAY agent:alice DRY_RUN PRUNE 0.1
1) "would_prune" 2) (integer) 12  3) "would_keep" 4) (integer) 847 ...
```

## End to end (Python)

```python
import redis, numpy as np
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')
r = redis.Redis(port=6380)
agent = 'agent:code-assistant'

def emb(text):  # raw little-endian f32 bytes — vex's vector wire format
    return model.encode(text).astype('<f4').tobytes()

def remember(text, t='episodic', importance=0.5):
    return r.execute_command('MEMORY.STORE', agent, text,
                             'TYPE', t, 'IMPORTANCE', str(importance), 'VEC', emb(text))

def recall(query, limit=5):
    return r.execute_command('MEMORY.RECALL', agent, emb(query),
                             'LIMIT', str(limit), 'THRESHOLD', '0.5')

# Session 1 — learn preferences
m1 = remember("User's project uses Zig and targets Linux", 'semantic', 0.9)
m2 = remember("User prefers no comments unless logic is complex", 'semantic', 0.8)

# Session 2 — something changes; record the relationship
m4 = remember("User now also targets macOS", 'semantic', 0.9)
r.execute_command('MEMORY.RELATE', agent, m4, m1, 'updates')

# Later — recall surfaces the recent, important, similar memory + its history
recall("What platform does the user target?")   # m4 first, m1 reachable via 'updates'

# Maintenance — let the stale fade
r.execute_command('MEMORY.DECAY', agent, 'PRUNE', '0.05')
```

**Half-life picks the forgetting curve** (`HALFLIFE`, seconds): `3600` chat-session
context, `86400` daily tasks, `604800` (default) weekly patterns, `2592000`
preferences, `31536000` core identity.

### Use with Mem0 / Zep

[Mem0](https://github.com/mem0ai/mem0) and [Zep](https://github.com/getzep/zep)
decide *what* to remember and detect contradictions (the LLM-side reasoning);
point them at vex for storage + retrieval via a thin adapter. Vex stores the
`contradicts` edge — it doesn't decide that two memories conflict.

## Capabilities and boundaries

- **Reasoning belongs to the application.** Vex stores explicitly supplied
  `contradicts`/`updates` relationships and ranks memories. Detecting conflicts,
  deciding which fact supersedes another, and consolidating episodic memories
  into semantic facts require application logic or an LLM.
- **Semantic recall needs an embedding.** Your application generates and supplies
  vectors. Memories stored without `VEC` remain accessible through ID lookup,
  listing, and graph traversal, but do not participate in vector recall.
- **Ranking uses a fixed formula:** similarity · recency · importance · frequency.
  `BOOST` squares one selected factor, as described above. Custom ranking models
  require client-side reranking of the returned candidates.
- **Multiple agents can use an application-managed shared namespace.** The
  `<agent>` argument identifies the memory namespace; it need not represent one
  agent process. For example, sales, security, and onboarding agents can use
  `workspace:acme-launch` to contribute to and recall the same memory pool.
  Separate namespaces are not automatically searched together. Namespace labels
  are not an authorization boundary or a built-in team permission model; the
  application must control access.
- **Relationships and duplicates are application-managed.** Relation types are
  free strings, so a typo creates a distinct type. Storing the same text with
  automatically generated IDs creates separate memories; deduplicate before
  storing when needed.
- **Recall and graph context are separate operations.** `MEMORY.RECALL` searches
  the vector index and ranks candidates; `MEMORY.CONTEXT` traverses stored
  relationships. Both run inside Vex, but client requests still incur network
  and serialization costs.
- **Capacity and latency depend on the workload.** This page does not establish
  a measured one-million-memory capacity or a sub-millisecond recall guarantee.
  Memory use depends on vector dimensions and representation, text, metadata,
  graph edges, and index overhead. Performance claims need reproducible
  `MEMORY.*` benchmarks reporting hardware, dataset size, concurrency, and
  latency percentiles. GET/SET benchmarks do not establish memory-recall
  performance.
