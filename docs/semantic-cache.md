# Semantic Cache

[Back to README](../README.md) | [LLM Ecosystem](llm-ecosystem.md) | [Vector Search](vector-search.md) | [Commands](commands.md)

---

> **Status: design proposal — NOT yet implemented.** The `CACHE.SEM*` commands
> (`CACHE.SEMSET` / `SEMGET` / `SEMINVAL` / `SEMCLEAR` / `SEMSTATS`) described
> below do not exist in the codebase. This page describes the intended design;
> the pattern can be approximated today with the real `GRAPH.SETVEC` /
> `GRAPH.VECSEARCH` primitives (see [Vector Search](vector-search.md)). Track
> status in [LLM Ecosystem](llm-ecosystem.md).

---

## Overview

Semantic caching stores LLM responses keyed by query **meaning**, not exact string match. When a new query arrives, Vex checks if a semantically similar query has already been answered. If the similarity exceeds a threshold, the cached response is returned -- skipping the LLM API call entirely.

```
"What's the weather in NYC?"     →  cache miss  → call LLM → cache response
"NYC weather today"              →  cache HIT   → return cached (similarity: 0.96)
"Weather forecast New York City" →  cache HIT   → return cached (similarity: 0.94)
"What's the weather in London?"  →  cache miss  → call LLM → cache response
```

**Cost impact:** LLM API calls cost $0.01-0.10+ each. On workloads with repetitive queries (customer support, FAQ bots, search assistants), semantic caching cuts LLM costs by 30-60%.

**Why not just use Redis?** Redis caches by exact key match. "What's the weather in NYC?" and "NYC weather today" are completely different keys. Semantic caching requires vector similarity search -- which Vex has natively via HNSW.

---

## Commands

| Command | Type | Description |
|---------|------|-------------|
| `CACHE.SEMSET key response dim vec... [opts]` | Write | Cache a response with its query embedding |
| `CACHE.SEMGET dim vec... [opts]` | Read | Check cache by query similarity |
| `CACHE.SEMINVAL key` | Write | Invalidate a specific cache entry |
| `CACHE.SEMCLEAR` | Write | Flush entire semantic cache |
| `CACHE.SEMSTATS` | Read | Cache hit/miss statistics |

### CACHE.SEMSET

Store a response with its query embedding for semantic retrieval.

```
CACHE.SEMSET <key> <response> <dim> <v1> <v2> ... <vN>
    [EX <seconds>]
    [PX <milliseconds>]
    [THRESHOLD <0.0-1.0>]
    [TAG <tag>]
```

**Parameters:**
- `key`: unique identifier for this cache entry (e.g., `cache:q:12345`)
- `response`: the LLM response to cache (bulk string)
- `dim`: embedding dimension (e.g., 384)
- `v1...vN`: query embedding as space-separated floats (must be `dim` values)
- `EX`: TTL in seconds (default: no expiry)
- `PX`: TTL in milliseconds
- `THRESHOLD`: per-entry similarity threshold (default: 0.95). A query must exceed this similarity to match this entry.
- `TAG`: optional tag for grouped invalidation (e.g., `weather`, `pricing`)

**Example:**
```
CACHE.SEMSET cache:q:1 "The weather in NYC is 72F and sunny." 4 0.1 0.8 0.3 0.5 EX 3600 TAG weather
OK
```

**Internal behavior:**
1. Store `key → response` in KV store (with TTL if specified)
2. Store the query embedding in HNSW index (field: `__semcache__`)
3. Link the HNSW entry to the KV key
4. Store threshold as metadata on the entry

### CACHE.SEMGET

Check if a semantically similar query has a cached response.

```
CACHE.SEMGET <dim> <v1> <v2> ... <vN>
    [THRESHOLD <0.0-1.0>]
    [COUNT <n>]
```

**Parameters:**
- `dim`: embedding dimension
- `v1...vN`: query embedding (same model that produced the stored embeddings)
- `THRESHOLD`: minimum similarity to consider a hit (default: 0.95, overrides per-entry threshold)
- `COUNT`: max entries to check (default: 1). Higher values increase hit probability but add latency.

**Returns:**
- On **hit**: `[key, response, similarity_score]`
- On **miss**: `nil`

**Example:**
```
# Original query cached above
CACHE.SEMGET 4 0.12 0.79 0.31 0.48
1) "cache:q:1"
2) "The weather in NYC is 72F and sunny."
3) "0.9847"

# Different enough query -- miss
CACHE.SEMGET 4 0.9 0.1 0.2 0.3
(nil)
```

**Internal behavior:**
1. ANN search on `__semcache__` HNSW index with K=COUNT
2. For each result, check if similarity >= threshold (query-level or per-entry)
3. If match found, fetch response from KV store
4. If KV entry expired (TTL), return nil and clean up the HNSW entry

### CACHE.SEMINVAL

Invalidate a specific cache entry.

```
CACHE.SEMINVAL <key>
CACHE.SEMINVAL TAG <tag>
```

- By key: removes the specific entry from both KV and HNSW index
- By tag: removes all entries with the given tag

```
CACHE.SEMINVAL cache:q:1
(integer) 1

CACHE.SEMINVAL TAG weather
(integer) 47
```

### CACHE.SEMCLEAR

Flush the entire semantic cache (KV entries + HNSW index).

```
CACHE.SEMCLEAR
OK
```

### CACHE.SEMSTATS

Return cache performance statistics.

```
CACHE.SEMSTATS
1) "hits"
2) (integer) 1247
3) "misses"
4) (integer) 3891
5) "hit_rate"
6) "0.2427"
7) "entries"
8) (integer) 892
9) "avg_similarity"
10) "0.9712"
11) "evictions"
12) (integer) 34
```

---

## Usage Example: Python

```python
import redis
import numpy as np
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')  # 384-dim
r = redis.Redis(port=6380)

def ask_llm_with_cache(query: str, threshold: float = 0.95) -> str:
    # 1. Embed the query
    query_vec = model.encode(query).astype(np.float32)
    floats = [str(x) for x in query_vec]

    # 2. Check semantic cache
    result = r.execute_command('CACHE.SEMGET', '384', *floats,
                               'THRESHOLD', str(threshold))

    if result is not None:
        key, response, score = result
        print(f"Cache HIT (similarity: {score})")
        return response.decode()

    # 3. Cache miss -- call LLM
    response = call_llm(query)

    # 4. Cache the response
    cache_key = f"cache:q:{hash(query)}"
    r.execute_command('CACHE.SEMSET', cache_key, response, '384', *floats,
                      'EX', '3600', 'TAG', 'general')

    return response
```

### With LangChain

```python
from langchain_vex import VexSemanticCache
from langchain.globals import set_llm_cache

# Drop-in LangChain cache backend
set_llm_cache(VexSemanticCache(
    redis_url="redis://localhost:6380",
    embedding_model=SentenceTransformer('all-MiniLM-L6-v2'),
    threshold=0.95,
    ttl=3600,
))

# All LangChain LLM calls now use Vex semantic cache automatically
```

---

## Architecture

```
CACHE.SEMGET <query_embedding>
       │
       ▼
┌─── HNSW Search ─────────────┐
│  Field: __semcache__          │
│  ef=50, K=COUNT               │
│  → candidate NodeIds + scores │
└──────────┬────────────────────┘
           │
           ▼
┌─── Threshold Filter ─────────┐
│  similarity >= threshold?     │
│  Check per-entry threshold    │
│  → best match or nil          │
└──────────┬────────────────────┘
           │
           ▼ (hit)
┌─── KV Lookup ────────────────┐
│  GET <matched_key>            │
│  Check TTL                    │
│  → response string or nil     │
└──────────┬────────────────────┘
           │
           ▼
      Return [key, response, score]
      or nil (miss)
```

**Storage footprint per cached entry:**
- KV: key + response bytes + entry overhead (~33 bytes)
- HNSW: embedding (dim * 4 bytes f32) + neighbor list (~300 bytes)
- Example: 384-dim embedding, 500-byte response = ~2.4 KB per entry

---

## Threshold Tuning

The similarity threshold is the critical parameter. Too low = false hits (wrong cached responses served). Too high = low hit rate (cache rarely helps).

| Threshold | Behavior | Use Case |
|-----------|----------|----------|
| 0.98-0.99 | Very strict, near-exact matches only | Medical, legal, safety-critical |
| 0.95 | Good default. Catches rephrasing, typos | General assistants, FAQ bots |
| 0.90 | Aggressive. Broader matches | High-volume, cost-sensitive, tolerant of approximate answers |
| < 0.85 | Dangerous. Unrelated queries may match | Not recommended |

**How to tune:**

1. Collect a sample of real queries (100-500)
2. For each pair, compute embedding similarity
3. Manually label: "same intent" vs "different intent"
4. Find the threshold that maximizes same-intent hits while minimizing cross-intent false matches
5. Or use Optuna:

```python
import optuna

def objective(trial):
    threshold = trial.suggest_float('threshold', 0.85, 0.99)
    hits, false_hits = evaluate_cache(query_pairs, threshold)
    return hits - (false_hits * 10)  # heavily penalize false hits

study = optuna.create_study(direction='maximize')
study.optimize(objective, n_trials=50)
print(f"Best threshold: {study.best_params['threshold']}")
```

---

## Pros

- **Immediate cost savings**: 30-60% reduction in LLM API calls on repetitive workloads. ROI measurable within hours of deployment.
- **Drop-in**: Works with any LLM, any embedding model. No changes to prompt engineering or LLM configuration.
- **Sub-millisecond**: HNSW search is ~0.1ms. Cache check adds negligible latency to the request path.
- **TTL support**: Cached responses auto-expire. Stale data is cleaned up without manual intervention.
- **Tag-based invalidation**: Invalidate all `weather`-tagged entries when weather data updates, without touching other cache entries.
- **No external dependencies**: Unlike GPTCache (Python library), this is a database primitive. Works from any language, any Redis client.
- **Composable**: Combine with `GRAPH.RAG` -- cache the full RAG response, not just the LLM output.

## Cons

- **Embedding generation required client-side**: Vex doesn't run embedding models. The client must embed the query before calling `CACHE.SEMGET`. This adds ~5-50ms depending on the embedding model and whether it's local (Ollama) or remote (OpenAI API).
- **Threshold tuning**: The default (0.95) works for most cases, but domain-specific workloads may need tuning. A bad threshold causes silent wrong answers -- harder to debug than a crash.
- **Embedding model lock-in**: If you switch embedding models, the entire cache must be flushed and rebuilt. Cosine similarities are not comparable across different models.
- **No streaming support**: LLM responses are cached as complete strings. Streaming APIs (which most chatbots use) must buffer the full response before caching.

## Limitations

- **Stateless queries only**: "What's the capital of France?" is cacheable. "Based on our earlier discussion, what do you think?" is not -- the answer depends on conversation context that isn't captured in the query embedding.
- **Single embedding model per cache**: All entries in `__semcache__` must use the same embedding dimension. You can't mix models.
- **No partial matching**: Either the full response is returned or nothing. There's no way to cache parts of a response or compose cached fragments.
- **Cold cache**: A fresh Vex instance has no cached entries. Hit rate starts at 0% and climbs as queries accumulate. Plan for a warm-up period.
- **Memory proportional to cache size**: Each entry costs ~2-3 KB (embedding + HNSW overhead). 100K cached entries ≈ 250 MB. For large caches, consider aggressive TTLs.
