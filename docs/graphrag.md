# GraphRAG

[Back to README](../README.md) · [Vector Search](vector-search.md) · [Agent Memory](agent-memory.md) · [Commands](commands.md)

---

Plain vector RAG returns a flat list of similar chunks. **GraphRAG** returns the
chunks *plus what they're connected to* — the entities they mention, the entities
those co-occur with, the edges between them — so the LLM gets *structure*, not
just a pile of text. The catch is that the usual way to build it is a vector DB
**and** a graph DB **and** glue code that fans out across both on every query.

Vex does it in **one command**. `GRAPH.RAG` runs the vector search and the graph
expansion in the same process, over shared memory:

| Step | Typical stack | Vex |
|---|---|---|
| Embed query | OpenAI / Ollama | same |
| ANN search | Pinecone (~50 ms) | `GRAPH.VECSEARCH` (~0.1 ms) |
| Fetch metadata | Redis `GET` × K (~5 ms) | zero-copy, same process |
| Graph expansion | Neo4j Cypher (~20 ms) | CSR BFS (~0.05 ms) |
| Assemble | your app code | `GRAPH.RAG` returns it |
| **Total** | **~75 ms + glue** | **~0.2 ms, one call** |

## The two commands that matter

### `GRAPH.RAG` — search + expand, one call

```
GRAPH.RAG <field> <f32_bytes> K <n>
    [DEPTH <d>]                 # BFS hops from each hit (0 = no expansion)
    [DIR IN|OUT|BOTH]           # expansion direction (default OUT)
    [EDGETYPE <type>]           # only expand along edges of this type
    [NODETYPE <type>]           # only include nodes of this type
```

The query vector is **raw little-endian f32 bytes** (dimension inferred from
length), same as `GRAPH.SETVEC`. The reply is a **flat array**, one entry per
result node — `[key, score, props, neighbors]`:

```
> GRAPH.RAG embedding "<query_bytes>" K 5 DEPTH 1 DIR OUT
1) 1) "doc:transformer"                         # key
   2) "0.9523"                                  # cosine score (graph-expanded nodes: -1)
   3) 1) "title" 2) "Attention Is All You Need" # props
   4) 1) "author:vaswani" 2) "topic:attention"  # 1-hop neighbors
```

### `GRAPH.COOCCUR` — auto-build the edges

The graph is only useful once entities are linked. `COOCCUR` connects every node
that shares a property value — the standard "these entities appeared in the same
chunk" link — so you don't hand-write edges:

```
GRAPH.COOCCUR <prop> [TYPE <edge_type>] [WINDOW <max_group>] [WEIGHT <w>] [INCR]
```

```
# entities tagged with the chunk they appeared in...
> GRAPH.COOCCUR chunk_id TYPE CO_OCCURS WINDOW 20 INCR
(integer) 3      # openai<->gpt4, openai<->transformer, gpt4<->transformer
```

`WINDOW` caps group size (skip huge cliques — it's O(n·k²) in group size `k`),
`INCR` strengthens an existing edge instead of skipping it, so an entity pair
seen across many chunks ends up with a higher weight. Returns edges created/updated.

## A full pipeline

```python
import redis
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')   # 384-dim
r = redis.Redis(port=6380)
def emb(t): return model.encode(t).astype('<f4').tobytes()   # f32 bytes

# 1. chunks → nodes with embeddings
for c in chunks:
    r.execute_command('GRAPH.ADDNODE', f'chunk:{c.id}', 'chunk')
    r.execute_command('GRAPH.SETPROP', f'chunk:{c.id}', 'text', c.text)
    r.execute_command('GRAPH.SETVEC', f'chunk:{c.id}', 'embedding', emb(c.text))

# 2. entities (extraction is the LLM's job — use spaCy / LlamaIndex / MS GraphRAG)
for c in chunks:
    for ent in extract_entities(c.text):
        k = f'entity:{ent.id}'
        r.execute_command('GRAPH.UPSERT_NODE', k, ent.type)
        r.execute_command('GRAPH.SETPROP', k, 'chunk_id', c.id)
        r.execute_command('GRAPH.ADDEDGE', k, f'chunk:{c.id}', 'MENTIONED_IN')

# 3. link co-occurring entities, then compact the CSR for fast traversal
r.execute_command('GRAPH.COOCCUR', 'chunk_id', 'TYPE', 'CO_OCCURS', 'WINDOW', '20', 'INCR')
r.execute_command('GRAPH.COMPACT')

# 4. query: top-5 chunks + their 1-hop graph context, one call
hits = r.execute_command('GRAPH.RAG', 'embedding', emb("how does attention work?"),
                         'K', '5', 'DEPTH', '1', 'DIR', 'BOTH')
context = format_for_llm(hits)      # chunks + linked entities + scores
answer = llm.chat(f"Context:\n{context}\n\nAnswer: {query}")
```

`GRAPH.INGEST <json>` bulk-loads nodes + edges in one call if you already have
the graph extracted.

## Use with Microsoft GraphRAG / LlamaIndex

Entity and relationship extraction — and community summarization — is LLM work;
vex is the store and the query engine. Point an existing pipeline at vex via a
thin output adapter: **MS GraphRAG** (Neo4j → vex) keeps its LLM extraction +
Leiden community detection and just writes to vex over RESP; **LlamaIndex
PropertyGraphIndex** works the same via a graph-store adapter. Why vex instead of
Neo4j + a vector DB: **22× faster shortest path**, vector search and graph in one
process, one binary, no Cypher to learn.

## Honest scope

- **Extraction isn't built in** — by design. Pulling entities/relationships from
  text is an AI problem (MS GraphRAG, LlamaIndex, spaCy); vex stores and queries
  the result.
- **Flat reply, not a subgraph object.** `GRAPH.RAG` returns the
  `[key, score, props, neighbors]` array above — graph-expanded nodes carry
  `score = -1` (not directly matched). There's no separate "subgraph" reply mode.
- **Bound the fan-out.** Result size is ~`K · avg_degree^DEPTH`; keep `DEPTH` at
  1–2. `COOCCUR` makes bidirectional links (two directed edges), doubling edge count.
- **Single-machine graph.** Fits in one box's RAM; partitioned graph is on the
  [roadmap](roadmap.md). On-disk vectors are f16 (cosine error ~1e-3 — fine for
  ANN), and HNSW indexes **persist** (`.vhi`) and reload on startup (no cold rebuild).
- **No community detection** — use the upstream pipeline's clustering and ingest
  the summaries as nodes.
