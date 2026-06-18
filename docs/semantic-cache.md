# Semantic Cache

[Back to README](../README.md) · [Agent Memory](agent-memory.md) · [Vector Search](vector-search.md) · [Commands](commands.md)

---

"What's the weather in NYC?" and "NYC weather today" mean the same thing — but to
a normal cache they're different keys, so you call (and pay for) the LLM twice.
A **semantic** cache keys responses by *meaning*: if a new query is similar
enough to one you've already answered, you return the cached answer and skip the
model call entirely.

```
"What's the weather in NYC?"      → miss → call LLM → cache it
"NYC weather today"               → HIT  (similarity 0.96) → no LLM call
"Weather forecast New York City"  → HIT  (similarity 0.94) → no LLM call
"What's the weather in London?"   → miss → call LLM → cache it
```

On repetitive workloads (support, FAQ bots, search assistants) that typically
cuts LLM spend **30–60%**. Redis can't do this — it matches exact keys; semantic
matching needs vector similarity search, which vex has natively (HNSW). The query
embedding is passed as **raw little-endian f32 bytes**, same as the rest of vex's
vector commands.

## Commands

| Command | Description |
|---|---|
| `CACHE.SEMSET key response <f32_bytes> [EX s] [PX ms] [THRESHOLD t] [TAG tag]` | Cache a response with its query embedding |
| `CACHE.SEMGET <f32_bytes> [THRESHOLD t] [COUNT n]` | `[key, response, score]` if a similar query is cached, else nil |
| `CACHE.SEMINVAL key` · `CACHE.SEMINVAL TAG tag` | Invalidate by key or tag; returns count removed |
| `CACHE.SEMCLEAR` | Flush the whole semantic cache |
| `CACHE.SEMSTATS` | `[hits, misses, entries]` |

- **`THRESHOLD`** (default 0.95) — minimum cosine similarity for a hit.
  `SEMSET`'s threshold is the entry's floor; `SEMGET`'s overrides at query time.
- **`COUNT`** (default 1) — how many nearest candidates to check; higher = more
  hits, slightly more latency.
- **`EX`/`PX`** — TTL; expired entries return nil and are cleaned up on access.
- **`TAG`** — group entries for bulk invalidation (e.g. drop all `weather`
  entries when the underlying data changes).

Each entry is a graph node (response + TTL + tag as node props) with its query
embedding in the shared `__semcache__` HNSW field. Canonical signatures:
[Commands](commands.md).

## End to end (Python)

```python
import redis
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')   # 384-dim
r = redis.Redis(port=6380)

def emb(text):                       # raw little-endian f32 bytes
    return model.encode(text).astype('<f4').tobytes()

def ask(query, threshold=0.95):
    hit = r.execute_command('CACHE.SEMGET', emb(query), 'THRESHOLD', str(threshold))
    if hit:                          # [key, response, score]
        return hit[1].decode()       # cache hit — no LLM call
    answer = call_llm(query)
    r.execute_command('CACHE.SEMSET', f'cache:{hash(query)}', answer, emb(query),
                      'EX', '3600', 'TAG', 'general')
    return answer
```

LangChain users get a drop-in backend via `langchain_vex.VexSemanticCache`
(`set_llm_cache(...)`), so every LLM call routes through the cache automatically.

## Tuning the threshold

The threshold is the one parameter that matters — too low serves *wrong* answers
(a silent bug, worse than a crash); too high and the cache rarely helps.

| Threshold | Behavior | Use |
|---|---|---|
| 0.98–0.99 | near-exact only | medical, legal, safety-critical |
| **0.95** | catches rephrasing/typos | **good default** — assistants, FAQ |
| 0.90 | broader matches | high-volume, cost-sensitive |
| < 0.85 | unrelated queries can match | don't |

To set it deliberately: take 100–500 real query pairs, label "same intent" vs
"different," and pick the threshold that maximizes same-intent hits while keeping
cross-intent false matches near zero (an Optuna sweep that penalizes false hits
~10× works well).

## Honest scope

- **You embed; vex caches.** Vex doesn't run embedding models — the client embeds
  the query first (~5–50 ms, local Ollama or remote API). The optional
  [`vex-embed`](../README.md) sidecar can do this off your hot path.
- **One model per cache.** All `__semcache__` entries must share an embedding
  dimension; cosine similarities aren't comparable across models, so switching
  models means flushing and rebuilding.
- **Stateless queries only.** "Capital of France?" caches; "based on our earlier
  discussion…" does not — the answer depends on context not in the query vector.
- **Cold start + footprint.** Hit rate climbs from 0% as queries accumulate; each
  entry is ~2–3 KB (embedding + HNSW overhead), so ~100K entries ≈ 250 MB — use
  TTLs for large caches. No streaming: responses are cached as complete strings.
- **Sub-millisecond** on a hit (HNSW search ~0.1 ms), all in-process — and
  composable: cache a full `GRAPH.RAG` result, not just the raw LLM text.
