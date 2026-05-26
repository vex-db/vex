# LLM Ecosystem

[Back to README](../README.md) | [GraphRAG](graphrag.md) | [Semantic Cache](semantic-cache.md) | [Agent Memory](agent-memory.md) | [MCP Server](mcp-server.md)

---

## The Problem

A typical LLM application today requires 3-4 separate databases:

| Need | Typical Solution | Latency | What Breaks |
|------|-----------------|---------|-------------|
| Session & state | Redis | ~0.1ms | Nothing -- but it can't search or traverse |
| Vector search (RAG) | Pinecone / Qdrant / Weaviate | 10-75ms | Network hop, separate billing, cold starts |
| Knowledge graph | Neo4j / Memgraph | 5-50ms | Another hop, another query language, another cluster |
| Conversation history | Postgres / Mongo | 2-10ms | Schema migrations, connection pooling |

Every hop between services adds network latency, serialization overhead, and operational complexity. A single RAG query touches 3 systems, 3 wire protocols, and 3 failure domains.

## How Vex Solves This

Vex collapses three of these into **one process, one protocol, one binary**:

```
┌──────────────────────────────────────────────────────────────┐
│                        Vex Engine                            │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │    KV     │  │  Graph   │  │  Vector  │  │ Pub/Sub  │    │
│  │ Sessions  │  │ Knowledge│  │  HNSW    │  │ Events   │    │
│  │ Cache     │  │ Relations│  │  ANN     │  │ Coord    │    │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └─────┬────┘    │
│        │              │              │              │         │
│        └──────────────┴──────────────┴──────────────┘         │
│                           │                                   │
│              ┌────────────┴────────────┐                     │
│              │    Composite Commands    │                     │
│              │  GRAPH.RAG  CACHE.SEM*  │                     │
│              │  MEMORY.*   GRAPH.COOCCUR│                     │
│              └─────────────────────────┘                     │
│                           │                                   │
│                      RESP Protocol                           │
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
    redis-cli            Python/TS             MCP Agent
    redis-py             any Redis client       Claude/GPT
```

| | Pinecone + Redis + Neo4j | Vex |
|---|---|---|
| Vector search | API call (~50ms) | In-process HNSW (~0.1ms) |
| Fetch metadata | Redis GET x K (~5ms) | Same memory, zero-copy |
| Graph expand | Neo4j query (~20ms) | CSR traverse (~0.05ms) |
| **Total** | **~75ms, 3 network hops** | **~0.2ms, 1 command** |

---

## Features for LLMs

### 1. GraphRAG (Enhanced Vector Search + Graph Traversal)

**Status**: Core exists (`GRAPH.RAG`), enhancements planned

Single-command vector search + graph expansion. Find semantically similar documents, then traverse the knowledge graph to pull in related entities, causal chains, and co-references -- all in <1ms.

**New commands**:
- `GRAPH.RAG` v2 -- returns full subgraph (nodes + edges + scores), not just a flat list
- `GRAPH.COOCCUR` -- auto-create edges between entities that appear in the same context

See [GraphRAG](graphrag.md) for full documentation.

### 2. Semantic Cache

**Status**: Planned

Cache LLM responses by query meaning, not exact string match. "What's the weather in NYC?" and "NYC weather today" hit the same cache entry. Cuts LLM API costs by 30-60% on repetitive workloads.

**Commands**: `CACHE.SEMSET`, `CACHE.SEMGET`, `CACHE.SEMINVAL`, `CACHE.SEMCLEAR`, `CACHE.SEMSTATS`

See [Semantic Cache](semantic-cache.md) for full documentation.

### 3. Agent Memory

**Status**: Planned

Purpose-built primitives for LLM agent memory -- episodic events, semantic facts, procedural patterns -- with temporal decay, relationship tracking, and composite scoring. Replaces ad-hoc "dump everything in a vector DB" approaches with structured, graph-native memory.

**Commands**: `MEMORY.STORE`, `MEMORY.RECALL`, `MEMORY.RELATE`, `MEMORY.CONTEXT`, `MEMORY.DECAY`

See [Agent Memory](agent-memory.md) for full documentation.

### 4. MCP Server (Model Context Protocol)

**Status**: Planned

Native MCP support lets LLMs (Claude, GPT, Cursor, etc.) use Vex directly as a tool -- store memories, query knowledge graphs, cache responses -- through a standardized protocol. No client code required.

**Usage**: `vex --mcp` or `vex --mcp --mcp-transport sse --mcp-port 3001`

See [MCP Server](mcp-server.md) for full documentation.

---

## Ecosystem: What Vex Does vs. What Existing Tools Do

Vex is a database engine. It stores, indexes, and queries. It does not run neural networks, tokenize text, or call LLM APIs. The ecosystem splits cleanly:

### Vex Handles (Database Primitives)

| Capability | How |
|-----------|-----|
| Store embeddings | `GRAPH.SETVEC` -- f32 write buffer, f16 mmap persistence |
| ANN vector search | `GRAPH.VECSEARCH` -- HNSW index, sub-millisecond |
| Search + traverse | `GRAPH.RAG` -- vector ANN + graph BFS in one call |
| Knowledge graph | `GRAPH.ADDNODE/ADDEDGE/TRAVERSE/PATH` -- CSR adjacency |
| Entity co-occurrence | `GRAPH.COOCCUR` -- auto-link entities sharing context |
| Semantic caching | `CACHE.SEM*` -- similarity-based response cache |
| Agent memory | `MEMORY.*` -- structured storage with decay and relations |
| Event coordination | `SUBSCRIBE/PUBLISH` -- cross-agent pub/sub |
| Session state | `SET/GET/EXPIRE` -- Redis-compatible KV |
| MCP interface | `--mcp` flag -- LLMs talk to Vex directly |

### Existing Tools Handle (AI/ML Layer)

| Need | Use This | How It Connects to Vex |
|------|----------|----------------------|
| **Generate embeddings** | [Ollama](https://ollama.ai) (local) / [LiteLLM](https://github.com/BerriAI/litellm) (proxy) / [HF TEI](https://github.com/huggingface/text-embeddings-inference) (production) | App calls embedding API, stores result via `GRAPH.SETVEC` |
| **Extract entities from documents** | [Microsoft GraphRAG](https://github.com/microsoft/graphrag) / [LlamaIndex PropertyGraphIndex](https://docs.llamaindex.ai/) | Pipeline extracts entities + relationships, writes via `GRAPH.INGEST` |
| **Memory consolidation & contradiction detection** | [Mem0](https://github.com/mem0ai/mem0) / [Zep](https://github.com/getzep/zep) | Agent reads/writes via `MEMORY.*` commands, AI logic stays in Mem0/Zep |
| **Token counting & context packing** | [tiktoken](https://github.com/openai/tiktoken) / LlamaIndex node parsers | Application-layer concern, not a database problem |
| **Threshold tuning for semantic cache** | [Optuna](https://github.com/optuna/optuna) / simple script | Offline hyperparameter sweep against query sample |
| **Graph visualization** | [RedisInsight](https://redis.io/insight/) (KV) + custom UI (graph) | RedisInsight works for KV/hash/set via RESP; graph needs dedicated UI |

### Adapters to Build (Thin, ~200-400 Lines Each)

| Adapter | Purpose | Why It Can't Be Avoided |
|---------|---------|------------------------|
| [langchain-vex](https://github.com/pratyush-sngh/langchain-vex) | `VexVectorStore`, `VexGraphStore`, `VexSemanticCache`, `VexMemory` | LangChain defines the interfaces; someone must implement them for Vex |
| [llama-index-vex](https://github.com/pratyush-sngh/llama-index-vex) | `VexVectorStore`, `VexPropertyGraphStore` | Same -- LlamaIndex interface adapters |
| MS GraphRAG output adapter | Redirect entity/relationship writes to `GRAPH.INGEST` | GraphRAG assumes Neo4j; needs ~200 lines to target Vex instead |
| Mem0 storage adapter | Custom `VectorStore` implementation pointing at Vex | Mem0 supports pluggable stores; adapter maps to `MEMORY.*` commands |

### What NOT to Build

| Tempting Idea | Why Skip It |
|---------------|-------------|
| vex-embed (embedding service) | Ollama/LiteLLM/TEI already do this perfectly. Don't rebuild. |
| vex-extract (entity extraction) | MS GraphRAG is purpose-built for this. Write an output adapter, not a competitor. |
| vex-memory-agent (consolidation) | Mem0 does this. Point it at Vex via adapter. |
| vex-context (token packing) | Application-layer problem. tiktoken + LlamaIndex solve it. |
| vex-tune (threshold tuning) | 50 lines of Python with Optuna. Not a project. |

---

## Recommended Build Order

```
Phase 1: HNSW Persistence           ██████░░░░░░░░░░░░░░░░░░
Phase 2: Semantic Cache  ─┐
         RAG v2 Subgraph  ├─ parallel  ████░░░░░░░░░░░░░░░░
         GRAPH.COOCCUR   ─┘
Phase 3: MEMORY.* Primitives                ████████░░░░░░░░
Phase 4: MCP Server                                 ████░░░░
Phase 5: Python Adapters                                ████
```

**Phase 1** unblocks everything (no more cold-start HNSW rebuild). **Phase 2** features are independent and parallel. **Phase 3** builds on all of Phase 2. **Phase 4-5** are the distribution layer.

See each feature's dedicated doc page for implementation details, command reference, and usage examples.
