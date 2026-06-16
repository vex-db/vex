# GraphRAG

[Back to README](../README.md) | [LLM Ecosystem](llm-ecosystem.md) | [Vector Search](vector-search.md) | [Agent Memory](agent-memory.md)

---

> **Partial implementation — read carefully.** The core `GRAPH.RAG`,
> `GRAPH.VECSEARCH`, `GRAPH.SETVEC`, and `GRAPH.GETVEC` commands are real, but
> the accurate signatures live in [Vector Search](vector-search.md): the query
> vector is passed as **raw f32 bytes**, `K` / `DEPTH` / `DIR` / `EDGETYPE` are
> keyword flags (not positional), and the reply is always the flat
> `[key, score, props, neighbors]` array. `GRAPH.COOCCUR` **is implemented**
> (see its section below — it matches this design). **Not implemented** (design
> proposal only): the `FORMAT subgraph` reply mode. Note: HNSW indices **do**
> persist to disk (`.vhi` files) and reload on startup — any "not yet
> implemented" note about HNSW persistence below is outdated.

---

## Overview

GraphRAG combines vector similarity search with knowledge graph traversal to produce richer context for LLMs than pure vector RAG. Instead of returning a flat list of similar chunks, GraphRAG returns a **connected subgraph** -- the chunks plus their relationships, entity co-occurrences, and causal chains.

Vex implements GraphRAG as database-level primitives. No external graph database, no multi-hop API calls, no glue code.

**Pipeline comparison:**

| Step | Traditional Stack | Vex |
|------|------------------|-----|
| 1. Embed query | OpenAI / Ollama | OpenAI / Ollama (same) |
| 2. ANN search | Pinecone (~50ms) | `GRAPH.VECSEARCH` (~0.1ms) |
| 3. Fetch metadata | Redis GET x K (~5ms) | Zero-copy (same process) |
| 4. Graph expansion | Neo4j Cypher (~20ms) | CSR BFS (~0.05ms) |
| 5. Return subgraph | Assemble in app code | `GRAPH.RAG` returns it |
| **Total** | **~75ms + app logic** | **~0.2ms, 1 command** |

---

## Commands

| Command | Type | Description |
|---------|------|-------------|
| `GRAPH.RAG field K depth query_vec [opts]` | Read | Vector search + graph expansion, returns subgraph |
| `GRAPH.COOCCUR tag_property [opts]` | Write | Auto-create edges between co-occurring entities |
| `GRAPH.VECSEARCH field K query_vec` | Read | Pure ANN search (existing, see [Vector Search](vector-search.md)) |
| `GRAPH.INGEST json` | Write | Bulk ingest nodes + edges from JSON (existing) |

### GRAPH.RAG (v2 -- Subgraph Returns)

Vector search + graph expansion in one call. Returns a full subgraph with nodes, edges, properties, and similarity scores.

```
GRAPH.RAG <field> <K> <depth> <query_f32...>
    [DIR OUT|IN|BOTH]
    [EDGETYPE <type>]
    [NODETYPE <type>]
    [FORMAT flat|subgraph]
```

**Parameters:**
- `field`: vector field name to search (e.g., `embedding`)
- `K`: number of top results from vector search
- `depth`: BFS expansion depth from each result (0 = no expansion)
- `query_f32...`: query vector as space-separated floats
- `DIR`: expansion direction (default `OUT`)
- `EDGETYPE`: filter expansion to edges of this type only
- `NODETYPE`: filter expansion to nodes of this type only
- `FORMAT`: response format (default `flat` for backward compatibility)

**Response format (FORMAT subgraph):**

```
1) "nodes"
2) 1) 1) "id"
      2) "doc:transformer"
      3) "type"
      4) "document"
      5) "score"
      6) "0.9523"
      7) "props"
      8) 1) "title"
         2) "Attention Is All You Need"
         3) "year"
         4) "2017"
   2) 1) "id"
      2) "author:vaswani"
      3) "type"
      4) "person"
      5) "score"
      6) "-1"
      7) "props"
      8) 1) "name"
         2) "Ashish Vaswani"
3) "edges"
4) 1) 1) "from"
      2) "doc:transformer"
      3) "to"
      4) "author:vaswani"
      5) "type"
      6) "authored_by"
      7) "weight"
      8) "1.0"
```

Nodes from vector search have a `score` (0.0-1.0 cosine similarity). Nodes discovered via graph expansion have `score` = `-1` (not directly matched).

**Response format (FORMAT flat -- default, backward compatible):**

```
1) 1) "doc:transformer"
   2) "0.9523"
   3) 1) "title"  2) "Attention Is All You Need"
   4) 1) "author:vaswani"  2) "topic:attention"
```

### GRAPH.COOCCUR

Auto-create edges between nodes that share a property value. This is the core link-building step in GraphRAG -- connecting entities that appear in the same document chunk.

```
GRAPH.COOCCUR <tag_property>
    [TYPE <edge_type>]
    [WINDOW <max_group_size>]
    [WEIGHT <default_weight>]
    [INCR]
```

**Parameters:**
- `tag_property`: the property to group by (e.g., `chunk_id`, `source_doc`)
- `TYPE`: label for created edges (default `CO_OCCURS`)
- `WINDOW`: skip groups larger than this (prevents O(n^2) cliques, default 50)
- `WEIGHT`: default weight for new edges (default 1.0)
- `INCR`: if edge already exists, increment its weight instead of skipping

**Example:**

After ingesting a document where entities "OpenAI", "GPT-4", and "Transformer" all appear in chunk_17:

```
# Entities already ingested with chunk_id property
GRAPH.ADDNODE entity:openai organization
GRAPH.SETPROP entity:openai chunk_id "chunk_17"
GRAPH.ADDNODE entity:gpt4 model
GRAPH.SETPROP entity:gpt4 chunk_id "chunk_17"
GRAPH.ADDNODE entity:transformer architecture
GRAPH.SETPROP entity:transformer chunk_id "chunk_17"

# Auto-link co-occurring entities
GRAPH.COOCCUR chunk_id TYPE CO_OCCURS INCR
(integer) 3
# Created: openai<->gpt4, openai<->transformer, gpt4<->transformer

# If another chunk also mentions openai + transformer:
GRAPH.SETPROP entity:openai chunk_id "chunk_42"
GRAPH.SETPROP entity:transformer chunk_id "chunk_42"
GRAPH.COOCCUR chunk_id TYPE CO_OCCURS INCR
# openai<->transformer weight incremented to 2.0 (stronger relationship)
```

**Returns:** count of edges created or updated.

---

## Full GraphRAG Pipeline

### Step 1: Ingest Documents

Split documents into chunks and create nodes:

```python
import redis
import numpy as np
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')  # 384-dim
r = redis.Redis(port=6380)

# Create chunk nodes with embeddings
for chunk in document_chunks:
    r.execute_command('GRAPH.ADDNODE', f'chunk:{chunk.id}', 'chunk')
    r.execute_command('GRAPH.SETPROP', f'chunk:{chunk.id}', 'text', chunk.text)
    r.execute_command('GRAPH.SETPROP', f'chunk:{chunk.id}', 'source', chunk.doc_id)

    embedding = model.encode(chunk.text).astype(np.float32)
    r.execute_command('GRAPH.SETVEC', f'chunk:{chunk.id}', 'embedding',
                      *[str(x) for x in embedding])
```

### Step 2: Extract Entities (Use Microsoft GraphRAG or LlamaIndex)

This is the AI-side work. Use an existing tool:

**Option A: Microsoft GraphRAG** (full pipeline, LLM-powered extraction)
```python
# GraphRAG extracts entities + relationships using an LLM.
# Write a Vex output adapter (~200 lines) to redirect writes:
#   Entity → GRAPH.ADDNODE entity:{name} {type}
#   Relationship → GRAPH.ADDEDGE entity:{from} entity:{to} {rel_type}
#   Community summary → GRAPH.SETPROP community:{id} summary {text}
```

**Option B: LlamaIndex PropertyGraphIndex** (lighter weight)
```python
from llama_index.core import PropertyGraphIndex

# With a Vex graph store adapter:
# index = PropertyGraphIndex.from_documents(
#     documents,
#     graph_store=VexPropertyGraphStore(redis_client=r),
# )
```

**Option C: Manual extraction** (simplest, no LLM needed)
```python
import spacy
nlp = spacy.load("en_core_web_sm")

for chunk in document_chunks:
    doc = nlp(chunk.text)
    for ent in doc.ents:
        node_key = f'entity:{ent.text.lower().replace(" ", "_")}'
        r.execute_command('GRAPH.UPSERT_NODE', node_key, ent.label_)
        r.execute_command('GRAPH.SETPROP', node_key, 'chunk_id', chunk.id)
        # Link entity to its source chunk
        r.execute_command('GRAPH.ADDEDGE', node_key, f'chunk:{chunk.id}', 'MENTIONED_IN')
```

### Step 3: Build Co-occurrence Links

```
# Auto-link entities that appear in the same chunk
GRAPH.COOCCUR chunk_id TYPE CO_OCCURS WINDOW 20 INCR
```

### Step 4: Compact the Graph

```
# Rebuild CSR for optimal traversal performance
GRAPH.COMPACT
```

### Step 5: Query

```python
query = "How do transformer attention mechanisms work?"
query_vec = model.encode(query).astype(np.float32)

results = r.execute_command(
    'GRAPH.RAG', 'embedding', '5', '2',
    *[str(x) for x in query_vec],
    'DIR', 'BOTH', 'FORMAT', 'subgraph'
)

# results contains:
# - Top 5 chunks by semantic similarity (with scores)
# - All entities linked to those chunks (depth 1)
# - Co-occurring entities (depth 2)
# - All edges between them (type, weight)

# Feed the subgraph to the LLM
context = format_subgraph_for_llm(results)
response = llm.chat(f"Based on this context:\n{context}\n\nAnswer: {query}")
```

---

## Integration with Microsoft GraphRAG

Microsoft GraphRAG is the most complete open-source GraphRAG pipeline. It handles:
- Document chunking
- LLM-powered entity/relationship extraction
- Community detection (Leiden algorithm)
- Community summarization
- Local + global search strategies

**Vex replaces Neo4j as the graph store.** Everything else stays the same.

```
┌─────────────────────────────────────────┐
│          Microsoft GraphRAG             │
│                                         │
│  Documents → Chunking → LLM Extraction  │
│      → Community Detection → Summaries  │
│                    │                     │
│              Output Adapter              │
│          (Neo4j → Vex, ~200 lines)       │
└────────────────────┬────────────────────┘
                     │ RESP protocol
                     ▼
┌─────────────────────────────────────────┐
│              Vex Engine                 │
│                                         │
│  GRAPH.INGEST (bulk load entities)      │
│  GRAPH.COOCCUR (auto-link)              │
│  GRAPH.SETVEC (store embeddings)        │
│  GRAPH.RAG (query time)                 │
└─────────────────────────────────────────┘
```

**Why Vex over Neo4j for this:**
- 22x faster shortest path queries
- No Cypher query language to learn (RESP commands)
- Vector search + graph in same process (Neo4j needs a separate vector DB)
- Single binary deployment vs Neo4j JVM + plugins

---

## Pros

- **Single-command RAG**: `GRAPH.RAG` does vector search + graph expansion in ~0.2ms. No multi-service orchestration.
- **Subgraph returns**: LLMs get structural context (who relates to whom, through what), not just a flat chunk list.
- **Co-occurrence auto-linking**: `GRAPH.COOCCUR` eliminates manual edge creation. Run it after each ingestion batch.
- **Incremental**: Add documents and entities incrementally. No need to rebuild the entire graph.
- **Framework-compatible**: Works with MS GraphRAG and LlamaIndex via thin output adapters.
- **22x faster graph queries than Memgraph**: CSR adjacency + bidirectional BFS.

## Cons

- **No built-in entity extraction**: Vex stores and queries the graph. Extracting entities from text requires an external tool (MS GraphRAG, LlamaIndex, spaCy). This is by design -- entity extraction is an AI problem, not a database problem.
- **HNSW rebuild on cold start**: Until HNSW persistence is implemented (Phase 1), restarting Vex rebuilds the HNSW index from vectors. ~2-5s per 100K vectors.
- **Single-node graph limit**: Current graph engine runs on one machine. For graphs exceeding available RAM (hundreds of millions of nodes), partitioned graph (v0.7 roadmap) is needed.
- **No Cypher/SPARQL**: Graph queries use Vex's command-based API, not a declarative query language. Simpler for programmatic use, less flexible for ad-hoc exploration.
- **COOCCUR is O(n*k^2)**: Where n = number of groups and k = group size. The `WINDOW` parameter caps k, but very large datasets need batched runs.

## Limitations

- **Subgraph size**: `GRAPH.RAG` with high K and deep DEPTH can return large subgraphs. The response is bounded by K * (avg_degree ^ DEPTH) nodes. Use DEPTH 1-2 for most workloads.
- **Vector dimension**: All vectors in a field must have the same dimension. You cannot mix 384-dim and 768-dim embeddings in the same field.
- **f16 quantization**: On-disk vectors use f16 (half precision). Cosine distance error ~1e-3. Acceptable for ANN retrieval, not for exact distance computation.
- **Edge directionality**: `GRAPH.COOCCUR` creates bidirectional relationships (two directed edges). This doubles edge count compared to undirected graphs.
- **No community detection**: MS GraphRAG uses Leiden clustering for community summarization. Vex doesn't have a built-in community detection algorithm. Use GraphRAG's pipeline for this step and ingest the results.
