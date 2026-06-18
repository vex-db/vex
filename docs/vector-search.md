# Vector Search & GRAPH.RAG

[Back to README](../README.md) | [Commands](commands.md) | [Architecture](architecture.md)

---

In Vex, vectors aren't a separate store — they live **on graph nodes**. Each
node can carry several named vector fields (a text embedding *and* an image
embedding, say), each backed by its own HNSW index for sub-millisecond ANN
search, persisted as f16 on disk and paged in by the OS. Because the vectors and
the graph share one engine, "find similar, then expand to what's related" is a
single in-process call — that's [`GRAPH.RAG`](graphrag.md).

This page covers the **vector layer**: storing vectors, searching them, and how
the HNSW + f16 storage works. For the search-plus-graph-expansion RAG story, see
[GraphRAG](graphrag.md).

Vectors are **raw little-endian f32 bytes** on the wire (dimension inferred from
length) — from Python, `model.encode(text).astype('<f4').tobytes()`.

---

## Commands

| Command | Type | Description |
|---------|------|-------------|
| `GRAPH.SETVEC node_key field <f32_bytes>` | Write | Store a vector embedding on a graph node |
| `GRAPH.GETVEC node_key field` | Read | Retrieve a node's vector as raw bytes |
| `GRAPH.VECSEARCH field <query_bytes> K n` | Read | ANN vector similarity search |
| `GRAPH.RAG field <query_bytes> K n [DEPTH d] [DIR d] [EDGETYPE t] [NODETYPE t]` | Read | Vector search + graph expand in one shot |

### GRAPH.SETVEC

Store a vector embedding on an existing graph node.

```
GRAPH.SETVEC <node_key> <field_name> <raw_f32_bytes>
```

- `node_key`: the graph node (must exist via `GRAPH.ADDNODE`)
- `field_name`: embedding field name (e.g., "text_embedding", "image_vec")
- `raw_f32_bytes`: vector as raw little-endian f32 bytes (length must be multiple of 4)

A node can have multiple vector fields (text embedding + image embedding, etc.). Each field maintains its own HNSW index. The first vector inserted for a field establishes its dimension — all subsequent vectors for that field must match.

Vectors are automatically **normalized to unit length** on insert (cosine similarity = dot product for unit vectors).

### GRAPH.GETVEC

Retrieve a stored vector as raw f32 bytes.

```
GRAPH.GETVEC <node_key> <field_name>
```

Returns the normalized vector as a bulk string of raw bytes, or `nil` if not set.

### GRAPH.VECSEARCH

Pure ANN vector search — find the K nearest nodes by cosine similarity.

```
GRAPH.VECSEARCH <field> <query_f32_bytes> K <n>
```

Returns an array of `[key, score, key, score, ...]` pairs, sorted by similarity (highest first).

```
127.0.0.1:6380> GRAPH.VECSEARCH embedding <query_bytes> K 5
 1) "doc:42"
 2) "0.9523"
 3) "doc:17"
 4) "0.9201"
 5) "doc:8"
 6) "0.8876"
```

### GRAPH.RAG — search + expand

`GRAPH.RAG` runs `VECSEARCH` and then a BFS graph expansion from each hit in one
call, returning a flat `[key, score, props, neighbors]` array per result
(graph-expanded nodes carry `score = -1`). Full signature, reply shape, and a
complete RAG pipeline live in [GraphRAG](graphrag.md).

---

## Storing and searching (Python)

```python
import redis
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')   # 384-dim
r = redis.Redis(port=6380)
def emb(t): return model.encode(t).astype('<f4').tobytes()   # raw f32 bytes

# store a vector on a node (node must exist via GRAPH.ADDNODE)
r.execute_command('GRAPH.ADDNODE', 'doc:transformer', 'document')
r.execute_command('GRAPH.SETVEC', 'doc:transformer', 'embedding',
                  emb("Attention Is All You Need"))

# K nearest neighbours by cosine similarity → [key, score, key, score, ...]
hits = r.execute_command('GRAPH.VECSEARCH', 'embedding',
                         emb("how does attention work?"), 'K', '5')
```

---

## Architecture

### Vector Storage

Vectors are stored in `VectorStore`, which follows the same composite-key pattern as `PropertyStore`:

```
Key: (node_id:u32 << 16) | field_id:u16
Value: []f32 (owned, normalized to unit length)
```

- Field names are interned via `StringIntern` (u16 IDs, max 64 fields)
- Dimension per field is enforced (set on first insert, validated after)
- Vectors are pre-normalized on insert: cosine similarity = dot product

### HNSW Index

One HNSW index per vector field, implementing the standard algorithm (Malkov & Yashunin, 2018):

| Parameter | Value | Description |
|-----------|-------|-------------|
| M | 16 | Max connections per node per layer |
| M_max0 | 32 | Max connections at layer 0 |
| ef_construction | 200 | Search width during index build |
| ef_search | 50 | Search width during query |
| Distance | Cosine | 1 - dot_product (pre-normalized vectors) |
| Level gen | Geometric | floor(-ln(rand) / ln(M)) |

**Insert**: O(log N) — greedy descent from top layer, then beam search + connect at each layer

**Search**: O(log N) — greedy descent to layer 0, then ef-wide beam search, filter by `node_alive` bits

**Delete**: Lazy — dead nodes are skipped during search via the graph's `node_alive` DynamicBitSet. No neighbor rewiring needed.

### GRAPH.RAG Execution Pipeline

```
GRAPH.RAG embedding <query> K 5 DEPTH 1
         │
         ▼
  ┌─── Normalize query vector ───┐
  │    (copy + unit normalize)   │
  └──────────┬───────────────────┘
             ▼
  ┌─── HNSW Search ─────────────┐
  │    ef=50 beam search         │
  │    Filter by node_alive      │
  │    → 5 NodeIds + distances   │
  └──────────┬───────────────────┘
             ▼
  ┌─── Graph Expand (per result) ┐
  │    BFS traverse, DEPTH=1     │
  │    Reuses query.traverse()   │
  │    → neighbor NodeIds        │
  └──────────┬───────────────────┘
             ▼
  ┌─── Collect Properties ───────┐
  │    node_props.collectAll()   │
  │    Zero-copy from PropStore  │
  └──────────┬───────────────────┘
             ▼
       RESP response
```

### Storage: Dual-Tier with f16 Quantization

Vectors live in **two tiers**: a heap **f32 write buffer** for everything inserted since the last save, and an **f16 mmap** tier (`.vvf` files) for everything that's been persisted. `SAVE`/`BGSAVE` merges the buffer into the mmap tier and drops the f32 copies. After a save, the OS manages hot/cold paging on the f16 tier — searched vectors stay in RAM, untouched ones cost ~0 resident memory.

**This is the key thing to understand about memory:** a vector is f32-in-RAM until you save, then f16-on-disk. So resident memory is high right after a bulk insert (full f32 buffer) and drops sharply after the first `SAVE`. See the table below — it lists both numbers.

```
Write path:  GRAPH.SETVEC → normalize → heap f32 (write buffer)
Save path:   SAVE/BGSAVE → merge write buffer + mmap → sorted f16 .vvf → atomic rename
Read path:   getById() → check write buffer (f32) → binary search mmap (f16→f32 conversion)
```

**On-disk format (.vvf):**
```
Header (20 bytes): magic("VXVF") + version + dtype(f16) + dim + count
Data (sorted by node_id): [node_id:u32 + vector:[dim]f16]*
```

### Memory Usage

Two resident-memory numbers matter, and they're very different:

- **Before SAVE** — every inserted vector is a heap f32 in the write buffer. This is the high-water mark you'll see right after a bulk load.
- **After SAVE, idle** — vectors are f16 in mmap'd `.vvf` files; cold pages get evicted, so resident memory falls to roughly the HNSW index (which always stays in RAM). Active search pages a hot subset back in, so steady-state-under-load sits between this and the on-disk size depending on your working set.

| Vectors | Dims | Disk (f16 `.vvf`) | RSS before SAVE (f32 buffer) | RSS after SAVE, idle | HNSW (always resident) |
|---------|------|-------------------|------------------------------|----------------------|------------------------|
| 10K  | 384 | 7.5 MB  | ~15 MB  | ~3 MB   | ~3 MB   |
| 10K  | 768 | 15 MB   | ~31 MB  | ~3 MB   | ~3 MB   |
| 100K | 384 | 75 MB   | ~154 MB | ~30 MB  | ~30 MB  |
| 100K | 768 | 147 MB  | ~300 MB | ~30 MB  | ~30 MB  |
| 1M   | 768 | 1.47 GB | ~3 GB   | ~300 MB | ~300 MB |

The HNSW column is a conservative upper bound (~300 bytes/node: a per-node neighbor-slice header + up to 32 layer-0 neighbors ×4 B + sparse higher layers; the real figure is typically lower). HNSW neighbor lists are heap-resident and **not** mmap'd, so they don't page out — they set the floor for "after SAVE, idle."

### Lazy Initialization

Vector infrastructure is **null by default** — zero memory overhead when vectors are not used. Initialized on first `GRAPH.SETVEC` call. If no `.vvf` files exist on startup, stays null.

---

## Persistence

Vectors are persisted in separate `.vvf` files (not in the main `.zdb` snapshot):

```
data/
├── vex.zdb              # KV + graph snapshot (unchanged)
├── vex.aof              # append-only log (unchanged)
└── vectors/
    ├── embedding.vvf    # mmap'd vector file for "embedding" field
    ├── embedding.vhi    # serialized HNSW index for "embedding" field
    ├── image_vec.vvf    # separate file per vector field
    └── image_vec.vhi    # separate HNSW index per vector field
```

- **SAVE/BGSAVE**: writes .vvf files (merge write buffer + existing mmap → sorted f16 → atomic rename) and .vhi files (serialized HNSW indices)
- **Startup**: mmap .vvf files → load HNSW from `.vhi` (instant), fall back to rebuild from `.vvf` if missing. Write-buffer vectors from AOF replay are re-inserted into the deserialized index
- **Crash safety**: .vvf.tmp written first, atomic rename. Crash during save leaves old .vvf intact
- **Bounds validation**: on load, `.vvf` file size is validated against the header entry count to detect truncation or corruption
- **Backward compatible**: no changes to .zdb format. Servers without vectors load fine

HNSW indices are **serialized to `.vhi` files** during SAVE and deserialized on startup. The `.vhi` format: 40-byte header (magic, version, M, node count, entry point), followed by layer 0 neighbors, node levels, then higher-layer neighbor lists. If `.vhi` is missing or corrupt, falls back to rebuild from `.vvf` vectors (~2-5s for 100K vectors).

---

## Concurrency

- `GRAPH.SETVEC` acquires the **graph write lock** (exclusive). Other graph reads/writes block.
- `GRAPH.GETVEC`, `GRAPH.VECSEARCH`, `GRAPH.RAG` acquire the **graph read lock** (shared). Multiple searches run in parallel.
- HNSW index is modified only during `SETVEC` (under write lock). Reads are lock-free.
- f16→f32 conversion uses **double scratch buffers** (alternated per access) for safe concurrent reference handling.

---

## Limitations

- **HNSW always in memory**: neighbor lists (~300 bytes/node) are not mmap'd. Fine for <1M vectors.
- **No HNSW parameter tuning at runtime**: M, ef_construction, ef_search are fixed. Compile-time config.
- **Lazy deletion only**: deleted nodes waste HNSW connections. Periodic rebuild planned.
- **Max 64 vector fields**: limited by StringIntern's u64 bitmask. Sufficient for practical use.
- **f16 precision**: ~3 decimal digits. Cosine distance error ~1e-3. Acceptable for ANN, not for exact match.

---

## Comparison with Other Vector Databases

| Feature | Redis (RediSearch) | Qdrant | Weaviate | Vex |
|---------|-------------------|--------|----------|-----|
| Vector search | HNSW or FLAT | HNSW | HNSW | HNSW |
| Graph traversal | No (RedisGraph discontinued) | No | No | Native CSR |
| Combined search+traverse | No | No | No | `GRAPH.RAG` |
| f16 quantization | No (f32 only) | Scalar/Product | No | f16 on disk |
| mmap vectors | No | Yes | No | Yes (.vvf files) |
| Protocol | RESP + FT.* | REST/gRPC | REST/gRPC | RESP (redis-cli) |
| Deployment | Server + module | Server/Cloud | Server/Cloud | Single binary |
| KV + Vectors + Graph | 3 separate systems | Vectors only | Vectors only | All in one process |
