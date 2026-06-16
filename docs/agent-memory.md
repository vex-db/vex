# Agent Memory

[Back to README](../README.md) | [LLM Ecosystem](llm-ecosystem.md) | [GraphRAG](graphrag.md) | [Semantic Cache](semantic-cache.md)

---

> **Status: design proposal — NOT yet implemented.** The `MEMORY.*` agent
> commands (`MEMORY.STORE` / `RECALL` / `RELATE` / `CONTEXT` / `DECAY` /
> `LIST` / `GET` / `DEL`) described below do not exist. (Vex does implement
> Redis `MEMORY USAGE|STATS|HELP` — a different, unrelated command.) The
> pattern can be built today on the real `GRAPH.*` + vector primitives. Track
> status in [LLM Ecosystem](llm-ecosystem.md).

---

## Overview

LLM agents need persistent memory that survives across sessions. Today, most agent frameworks dump text into a vector database and do similarity search. This loses all **relational structure** -- contradictions, temporal ordering, causal links, and importance weighting are gone.

Vex provides purpose-built memory primitives that combine three storage paradigms:

| Paradigm | What It Stores | Vex Primitive |
|----------|---------------|---------------|
| **Vector** | Semantic meaning for fuzzy recall | HNSW index per agent |
| **Graph** | Relationships between memories | CSR edges with types and weights |
| **KV** | Fast lookup by known ID | Direct key access with TTL |

### Memory Types

| Type | Description | Example |
|------|-------------|---------|
| `episodic` | Events, conversations, observations | "User deployed to staging on May 11" |
| `semantic` | Facts, preferences, knowledge | "User prefers Zig over Rust for systems work" |
| `procedural` | Action patterns, workflows | "When user says 'deploy', run tests first then push" |

### Why Not Just Use a Vector DB?

```
Vector DB approach:
  "User likes Python"     → embedding → store
  "User prefers Rust"     → embedding → store
  "User deployed v2.1"    → embedding → store
  Recall: similarity search returns all three. No structure.
  Can't tell: do these contradict? What came first? What's more important?

Vex approach:
  "User likes Python"     → MEMORY.STORE (semantic, importance: 0.6)
  "User prefers Rust"     → MEMORY.STORE (semantic, importance: 0.8)
  MEMORY.RELATE: "prefers Rust" --[contradicts]--> "likes Python"
  "User deployed v2.1"    → MEMORY.STORE (episodic, auto-timestamped)
  Recall: ranked by (similarity * recency * importance), contradictions flagged.
```

---

## Commands

| Command | Type | Description |
|---------|------|-------------|
| `MEMORY.STORE agent text [opts]` | Write | Store a memory with metadata and optional embedding |
| `MEMORY.RECALL agent dim vec... [opts]` | Read | Recall memories by semantic similarity, ranked by composite score |
| `MEMORY.RELATE agent mem_a mem_b rel [opts]` | Write | Create a typed relationship between two memories |
| `MEMORY.CONTEXT agent memory_id [opts]` | Read | Get a memory + its related memories (subgraph) |
| `MEMORY.DECAY agent [opts]` | Write | Apply temporal decay, optionally prune low-score memories |
| `MEMORY.LIST agent [opts]` | Read | List memories with filters |
| `MEMORY.GET agent memory_id` | Read | Get a single memory by ID |
| `MEMORY.DEL agent memory_id` | Write | Delete a memory and its relationships |

### MEMORY.STORE

Store a memory with metadata, optional embedding, and scoring parameters.

```
MEMORY.STORE <agent_id> <text>
    [ID <memory_id>]
    [TYPE episodic|semantic|procedural]
    [IMPORTANCE <0.0-1.0>]
    [DIM <dim> VEC <v1> <v2> ... <vN>]
    [SOURCE <source_id>]
    [TTL <seconds>]
    [HALFLIFE <seconds>]
    [TAG <tag>]
```

**Parameters:**
- `agent_id`: namespace for this agent's memories (e.g., `agent:code-assistant`)
- `text`: the memory content (stored as a property on the graph node)
- `ID`: explicit memory ID. If omitted, auto-generated as `mem:<agent_id>:<counter>`
- `TYPE`: memory classification (default: `episodic`)
- `IMPORTANCE`: how important this memory is (default: 0.5). Higher = decays slower in ranking.
- `DIM` + `VEC`: pre-computed embedding. If provided, stored in HNSW for semantic recall. If omitted, the memory is only retrievable by ID, type, or graph traversal.
- `SOURCE`: link to origin (conversation ID, file path, etc.). Stored as a graph edge `SOURCED_FROM`.
- `TTL`: hard expiry in seconds. Memory is deleted after this time.
- `HALFLIFE`: soft decay in seconds. Memory's recency score halves every `HALFLIFE` seconds. Default: 604800 (7 days).
- `TAG`: optional tag for batch operations.

**Example:**
```
MEMORY.STORE agent:alice "User prefers dark mode for all editors"
    TYPE semantic IMPORTANCE 0.7
    DIM 4 VEC 0.1 0.3 0.8 0.2
    HALFLIFE 2592000 TAG preferences
"mem:agent:alice:1"
```

**Internal behavior:**
1. Create a graph node: `GRAPH.ADDNODE mem:agent:alice:1 memory`
2. Set properties: `text`, `type`, `importance`, `created_at`, `last_accessed`, `access_count`, `halflife`, `tag`
3. If embedding provided: `GRAPH.SETVEC mem:agent:alice:1 __memory_agent:alice__ <vec>`
4. If source provided: `GRAPH.ADDEDGE mem:agent:alice:1 <source_id> SOURCED_FROM`

### MEMORY.RECALL

Recall memories by semantic similarity, ranked by a composite score.

```
MEMORY.RECALL <agent_id> <dim> <v1> <v2> ... <vN>
    [LIMIT <n>]
    [THRESHOLD <0.0-1.0>]
    [TYPE episodic|semantic|procedural]
    [AFTER <unix_timestamp>]
    [BEFORE <unix_timestamp>]
    [TAG <tag>]
    [BOOST recency|importance|frequency]
```

**Parameters:**
- `agent_id`: which agent's memories to search
- `dim` + `vec`: query embedding
- `LIMIT`: max results (default: 10)
- `THRESHOLD`: minimum similarity (default: 0.5)
- `TYPE`: filter by memory type
- `AFTER/BEFORE`: time range filter (unix timestamps)
- `TAG`: filter by tag
- `BOOST`: emphasize a scoring component (default: balanced)

**Scoring formula:**

```
score = similarity * recency_decay * importance * frequency_boost

where:
  similarity    = cosine_similarity(query_vec, memory_vec)        [0.0 - 1.0]
  recency_decay = 0.5 ^ (age_seconds / halflife)                  [0.0 - 1.0]
  importance    = memory.importance                                [0.0 - 1.0]
  frequency_boost = min(1.0, log2(access_count + 1) / 10)         [0.0 - 1.0]
```

With `BOOST`:
- `recency`: recency_decay weight doubled
- `importance`: importance weight doubled
- `frequency`: frequency_boost weight doubled

**Response format:**
```
1) 1) "mem:agent:alice:3"
   2) "0.8724"                           # composite score
   3) "User prefers Zig for systems work"  # text
   4) "semantic"                          # type
   5) "0.9312"                           # similarity
   6) "0.9100"                           # recency_decay
   7) "0.8"                              # importance
   8) "1715400000"                       # created_at (unix)
2) 1) "mem:agent:alice:1"
   2) "0.7891"
   3) "User prefers dark mode for all editors"
   ...
```

### MEMORY.RELATE

Create a typed, weighted relationship between two memories.

```
MEMORY.RELATE <agent_id> <memory_a> <memory_b> <relation_type>
    [WEIGHT <0.0-1.0>]
```

**Relation types:**
- `supports`: memory_b supports/confirms memory_a
- `contradicts`: memory_b contradicts memory_a
- `updates`: memory_b is a newer version of memory_a
- `causes`: memory_a caused memory_b
- `extends`: memory_b adds detail to memory_a
- Custom types: any string is accepted

**Example:**
```
MEMORY.RELATE agent:alice mem:agent:alice:5 mem:agent:alice:1 contradicts WEIGHT 0.9
OK

# "User switched to light mode" contradicts "User prefers dark mode"
```

**Internal behavior:** Creates a graph edge from memory_a to memory_b with type and weight.

### MEMORY.CONTEXT

Get a memory and all its related memories as a subgraph. Useful for building context around a specific memory.

```
MEMORY.CONTEXT <agent_id> <memory_id>
    [DEPTH <n>]
    [TYPES <rel_type> [rel_type ...]]
```

**Parameters:**
- `DEPTH`: how many relationship hops to follow (default: 2)
- `TYPES`: filter to specific relationship types

**Response format:**
```
1) "center"
2) 1) "mem:agent:alice:5"
   2) "User switched to light mode"
   3) "semantic"
   4) "0.8"
3) "related"
4) 1) 1) "mem:agent:alice:1"
      2) "User prefers dark mode for all editors"
      3) "semantic"
      4) "contradicts"
      5) "0.9"
   2) 1) "mem:agent:alice:3"
      2) "User mentioned eye strain issues"
      3) "episodic"
      4) "causes"
      5) "0.7"
```

### MEMORY.DECAY

Apply temporal decay scoring and optionally prune low-score memories.

```
MEMORY.DECAY <agent_id>
    [PRUNE <threshold>]
    [DRY_RUN]
```

**Parameters:**
- `PRUNE`: delete memories with composite score below this threshold (no vector similarity component -- uses `importance * recency_decay * frequency_boost`)
- `DRY_RUN`: show what would be pruned without actually deleting

**Example:**
```
MEMORY.DECAY agent:alice DRY_RUN PRUNE 0.1
1) "would_prune"
2) (integer) 12
3) "would_keep"
4) (integer) 847
5) "lowest"
6) 1) "mem:agent:alice:42"
   2) "0.0312"
   3) "Discussed weather on Jan 3"

MEMORY.DECAY agent:alice PRUNE 0.1
1) "pruned"
2) (integer) 12
3) "remaining"
4) (integer) 847
```

### MEMORY.LIST

List memories with filters.

```
MEMORY.LIST <agent_id>
    [TYPE <type>]
    [TAG <tag>]
    [LIMIT <n>]
    [SORT score|recency|importance]
```

### MEMORY.GET

Get a single memory by ID. Updates `last_accessed` and `access_count`.

```
MEMORY.GET <agent_id> <memory_id>
```

### MEMORY.DEL

Delete a memory, its embedding, and all relationships.

```
MEMORY.DEL <agent_id> <memory_id>
(integer) 1
```

---

## Usage Example: Agent with Persistent Memory

```python
import redis
import numpy as np
import time
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')
r = redis.Redis(port=6380)
agent_id = 'agent:code-assistant'

def remember(text: str, mem_type: str = 'episodic', importance: float = 0.5):
    """Store a new memory."""
    vec = model.encode(text).astype(np.float32)
    floats = [str(x) for x in vec]
    return r.execute_command(
        'MEMORY.STORE', agent_id, text,
        'TYPE', mem_type,
        'IMPORTANCE', str(importance),
        'DIM', '384', 'VEC', *floats,
    )

def recall(query: str, limit: int = 5) -> list:
    """Recall relevant memories."""
    vec = model.encode(query).astype(np.float32)
    floats = [str(x) for x in vec]
    return r.execute_command(
        'MEMORY.RECALL', agent_id, '384', *floats,
        'LIMIT', str(limit),
        'THRESHOLD', '0.5',
    )

def relate(mem_a: str, mem_b: str, relation: str, weight: float = 1.0):
    """Create a relationship between memories."""
    return r.execute_command(
        'MEMORY.RELATE', agent_id, mem_a, mem_b, relation,
        'WEIGHT', str(weight),
    )

# --- Usage ---

# Session 1: User tells us their preferences
m1 = remember("User's project uses Zig and targets Linux", "semantic", 0.9)
m2 = remember("User prefers no comments in code unless logic is complex", "semantic", 0.8)
m3 = remember("Discussed refactoring the graph engine today", "episodic", 0.4)

# Session 2: User mentions something contradictory
m4 = remember("User now also targets macOS for their project", "semantic", 0.9)
relate(m4, m1, "updates")  # m4 updates m1

# Session 3: Agent needs context for a new request
memories = recall("What platform does the user target?")
# Returns m4 (highest: recent + important + similar)
# Also returns m1 (related via 'updates' -- agent sees the history)

context = recall("What are the user's code style preferences?")
# Returns m2 (high similarity + importance)

# Periodic maintenance: prune old, unimportant memories
r.execute_command('MEMORY.DECAY', agent_id, 'PRUNE', '0.05')
```

### Integration with Mem0

[Mem0](https://github.com/mem0ai/mem0) handles the AI-side of memory -- deciding **what** to remember, detecting contradictions, consolidating episodic memories into semantic ones. Vex handles storage and retrieval.

```python
# Mem0 with Vex storage adapter (adapter: ~200 lines)
from mem0 import Memory

config = {
    "vector_store": {
        "provider": "vex",
        "config": {
            "host": "localhost",
            "port": 6380,
        }
    }
}

m = Memory.from_config(config)
m.add("User prefers dark mode", user_id="alice")
results = m.search("What theme does the user like?", user_id="alice")
```

### Integration with Zep

[Zep](https://github.com/getzep/zep) provides long-term memory with temporal awareness, entity extraction from conversations, and memory synthesis. Point it at Vex for storage.

---

## Scoring Model

The composite score determines memory ranking during recall:

```
composite = similarity * recency * importance * frequency

similarity    = cosine(query_embedding, memory_embedding)
recency       = 0.5 ^ (age / halflife)
importance    = user-assigned weight [0.0 - 1.0]
frequency     = min(1.0, log2(access_count + 1) / 10)
```

### Recency Decay Visualization

With default halflife = 7 days:

```
Age           Recency Score
─────────────────────────────
0 (now)       1.000
1 day         0.906
3 days        0.740
7 days        0.500  ← halflife
14 days       0.250
30 days       0.049
90 days       0.000  ← effectively gone
```

Different halflife values for different use cases:

| Halflife | Use Case |
|----------|----------|
| 3600 (1 hour) | Chat session context. Forget fast. |
| 86400 (1 day) | Daily task context. Yesterday's work fades. |
| 604800 (7 days) | Default. Weekly work patterns. |
| 2592000 (30 days) | Preferences, long-term facts. |
| 31536000 (1 year) | Core identity, permanent preferences. |

---

## Pros

- **Graph-native relationships**: Memories aren't isolated blobs. "Contradicts", "updates", "causes" relationships give agents structural reasoning tools that pure vector search cannot.
- **Composite scoring**: Ranking by similarity * recency * importance * frequency is strictly better than ranking by similarity alone. Recent, important, frequently-accessed memories surface first.
- **Temporal decay**: Old memories naturally fade without manual cleanup. Configurable per-memory via `HALFLIFE`.
- **Agent namespacing**: Each agent has isolated memory (`agent_id` namespace). Multi-agent systems don't collide.
- **Sub-millisecond recall**: HNSW search + scoring + graph traversal all in-process. No network hops.
- **Works with existing memory frameworks**: Mem0 and Zep can use Vex as their storage backend via thin adapters.
- **Incremental**: Memories are added and related one at a time. No batch reprocessing required.

## Cons

- **Contradiction detection is NOT built in**: Vex stores the `contradicts` relationship, but **you** (or Mem0/Zep) must detect the contradiction using an LLM. Vex is a database, not a reasoning engine.
- **Memory consolidation is NOT built in**: Merging 50 episodic memories ("visited file X") into one semantic memory ("user frequently works on auth module") requires LLM reasoning. Vex stores the result; an external agent does the thinking.
- **Embedding required for semantic recall**: Memories without embeddings are only retrievable by ID, type, or graph traversal. For full recall capability, client must embed each memory at store time.
- **Scoring model is fixed**: The composite formula (similarity * recency * importance * frequency) is hardcoded. If you need a different ranking model, you must post-process results client-side.
- **No cross-agent memory sharing by default**: Each agent's memories are namespaced. Sharing requires explicit cross-namespace reads (not yet supported).

## Limitations

- **Memory count per agent**: Practical limit depends on vector dimensionality. At 384-dim with HNSW overhead, ~1M memories per agent ≈ 700 MB. For most agents, this is more than enough.
- **Halflife minimum resolution**: Recency decay is computed at query time, not stored. Very short halflife values (< 60s) may not behave precisely due to clock granularity.
- **Relation types are strings**: No enforcement of valid relation types. Typos in relation names (`contardicts` vs `contradicts`) silently create new types.
- **No automatic deduplication**: Storing the same text twice creates two memories. Deduplication must be handled by the client (or by checking similarity before storing).
- **No memory versioning**: `MEMORY.STORE` with the same ID overwrites. There's no history of previous values. Use `updates` relations for explicit version chains.
