# Bounded vector speed experiment: reusable HNSW search workspace

Status: the opt-in prototype and first 10K write-buffer screen are complete.
The observed median QPS change (+1.46%) did not pass the 5% paired threshold;
no server integration is enabled. See [results](vector-workspace-results-2026-09-23.md).
The broader matrix below remains a plan, not completed validation. The shared
graph architecture and synchronization are unchanged.

## Decision and source evidence

Try **caller-owned reusable HNSW search workspace** before an ID-to-offset
index. The first experiment changes allocation reuse only, not visitation,
distance computation, HNSW parameters, vector encoding or persistence.

`HnswIndex.searchLayer` in `src/engine/vector/hnsw.zig` allocates and zeroes a
`DynamicBitSet` sized to `capacity`, then creates `SortedCandidates` and
`MinHeap` buffers on every call. `capacity` is the greatest NodeId plus one,
not the number of live vectors. `search` performs one such layer-zero beam
search after greedy descent; insertion uses the same routine at multiple
layers. The candidate initially targets **queries only**. Preserve insertion
and its owned candidate-result contract.

An ID-to-row index is credible later: every distance evaluation calls
`VectorStore.getById`; mapped data performs binary search before decoding.
However, it helps only the mapped tier, adds per-field memory and load work,
and must preserve tombstone → write-buffer → mapped-data precedence. A dense
u32 row array costs `4 * (max_id + 1)` bytes per field even for sparse IDs; a
sparse map has different overhead. The saved row offsets must belong to one
mapping generation. `saveAllFields` currently writes replacement files but
does not itself replace the active mapping or clear the write buffer; do not
assume save changes the active lookup tier. Workspace reuse avoids those
additional persistence and indexing contracts. Neither opportunity has a
measured speedup yet.

## Smaller-model implementation assignment

The implementer owns `src/engine/vector/hnsw.zig`, focused additions to
`tests/unit/engine/hnsw_test.zig`, and one new standalone diagnostic under
`bench/loadtest/diagnostics/` with its run notes. They are not alone in this
checkout: preserve all other agents' changes. Do not edit the table candidate,
VectorStore, graph storage, command handler, locks, allocator selection or
on-disk formats. Keep existing public `search` behavior as the control; expose
an explicit `searchWithWorkspace` alternative used only by tests/diagnostic
until the screen passes. No server-wide build flag or default switch yet.

1. Introduce an explicitly owned `SearchWorkspace` containing a reusable
   visited bitset, sorted candidates and working heap. Give it allocator,
   initialization/reset/grow/destruction rules. It belongs to a serialized
   query caller, not a global singleton and not a shallow-copied HnswIndex.
   Separate callers/index-build threads must not share it concurrently.
2. Factor the existing beam loop into one private routine that fills provided
   scratch structures; preserve neighbor iteration order, comparisons,
   tie insertion, candidate trimming and heap ordering exactly. Avoid two
   independently maintained search algorithms. The legacy wrapper owns and
   frees temporary storage; the workspace path retains scratch allocations.
   Never return a by-value owning candidate object that aliases the workspace
   and is then freed by the insertion caller's existing defer.
3. Initially clear the complete reusable bitset for each search. This removes
   allocation churn but retains O(max_id / word_bits) clearing. Do not bundle
   generation arrays, touched-word tracking or a different visited algorithm.
   Reset logical lengths even on early return or error. Preserve the actual
   bitset capacity/growth invariant when an index grows or a workspace is
   reused with another index.
4. Retain candidate/heap capacity across calls. The sorted candidate set needs
   a temporary `ef + 1` entry before trimming; the pending working heap can
   exceed `ef`. Never cap the heap to ef or discard queued candidates. A new
   workload may legitimately grow the workspace; report that growth.
5. Return an independently owned result slice with the same allocation/free
   contract as `search`. A result must remain valid after the next workspace
   search. Keep output allocation behavior comparable and report it separately
   from scratch allocations. Do not claim an entirely allocation-free query.
6. Make OOM cleanup/reuse safe: failed scratch growth must not leak, double
   free, leave a stale candidates slice, or contaminate the next search.
   Destruction must release every retained buffer.

## Lifetime and concurrency constraints

The normal reactor path takes the graph rwlock and then the shared `kv_mutex`
(`worker.zig:executeCommand`), so current command execution serializes these
searches. Do not present the two decoding buffers as an established concurrent
reactor race or remove the mutex as part of this experiment. Explicit
workspace ownership nevertheless keeps the library API safe for separate
callers. It does not make concurrent same-field mapped-vector decoding safe;
that storage still mutates shared scratch state.

There is a distinct **single-thread lifetime concern** in the baseline:
`HnswIndex.insert` holds `vec = getById(...)` across repeated distance calls,
and `addConnection` similarly retains `from_vec`. For mapped vectors,
`MmapField.getByNodeId` alternates two scratch buffers, so two subsequent
successful reads of that field reuse the original buffer. Retained query
slices can therefore be overwritten during construction/pruning. The graph's
missing-`.vhi` load fallback builds via `insert` on mapped vectors. This is a
source-derived correctness risk, not a measured recall regression; reproduce
and fix it separately before claiming correctness or build speed for that
path. A workspace must not hide it by coincidentally changing buffer usage.

For the first saved-data query fixture, build the HNSW graph while vectors are
stable write-buffer allocations, persist `.vvf` and `.vhi`, then fresh-load
both. Verify `.vhi` was actually loaded rather than silently rebuilt. Use an
owned, normalized query buffer independent of VectorStore scratch. The search
loop consumes each candidate decode immediately; it does not retain that
candidate slice across later distances. Do not insert/rebuild during the
read-only timed query phase.

## Required correctness and recall checks

- Compare control and candidate on the **same immutable index and query
  sequence**: exact ordered NodeIds, result counts and f32 distances. Preserve
  tie order with duplicate/equidistant vectors. Distance evaluation sequence
  or count may be recorded in an untimed test if needed to diagnose divergence.
- Cover empty/single-node indexes, k greater than live count, ef changes,
  repeated queries, different queries after reset, dead-node filtering,
  deleted entry points and sparse IDs with max ID much larger than live count.
  Treat k=0 as a baseline-compatibility check: do not silently change its
  current behavior in the optimization. API semantic fixes are separate work.
- Grow an index between queries, reuse one workspace sequentially across two
  differently sized indexes/fields, and use two independent workspaces.
  Confirm the prior returned result survives subsequent searches.
- Run existing HNSW, vector-store, graph/RAG and persistence tests. Add
  deterministic allocation-failure tests for visited growth, candidate/heap
  growth and result allocation, followed by a successful query and complete
  destruction using the leak-checking allocator.
- Test write-buffer and fresh `.vvf`/`.vhi` reload queries separately. Apply
  write-buffer overrides and vector tombstones between untimed query batches;
  compare both search implementations on the same resulting graph. Do not
  interpret an update-only change to payloads as a rebuilt ANN topology.
- Compute exact top-K with the same normalized queries and the actual tier's
  decoded stored vectors, respecting deletion/alive filtering. Report
  recall@K for each storage tier. Saved f16 quantization can differ from the
  original f32 dataset: preserve that distinction. Candidate recall must be
  identical to control, not merely within a loose tolerance. Poor baseline
  recall limits product claims even if query execution is faster.

## Measurement and stop gates after independent review

Use native ReleaseFast, libc and CLOCK_MONOTONIC with identical seeded data,
query order, dimensions, k, ef, graph bytes and compiler flags. Precompute
queries and exact results outside timed regions; consume an observable result
checksum. Separate uninstrumented timing from allocation accounting. Include
both cold-workspace growth and repeated steady-state batches after warming
with the full fixed query set; do not hide retained high-water capacity.

Start with 10k and 100k vectors at 64 dimensions, k=10, ef_search=50, fixed
construction order/seed and existing construction parameters. Include a
sparse-ID control with the same live population. A promising result expands
to 384 dimensions, k/ef variation and the saved-data fixture above. Report
p50/p99, QPS, CPU/query, scratch/output allocations, workspace capacity,
loaded/peak/post-destruction RSS and recall. Do not time competing benchmarks
or builds concurrently.

For a speed screen require fresh-process control/candidate/candidate/control
batches, both paired QPS gains at least 5%, median gain above within-mode
variation, CPU/query moving in the same direction, and no p99 regression over
5%. Stop as inconclusive above 5% repeat variation or on resource pressure.
Correctness divergence, recall loss, changed graph/search parameters, leaks,
or unbounded retained workspace growth rejects the candidate. Workspace
requested bytes should reconcile to bitset + actual heap/candidate backing
capacity, return to zero on destruction, and stop growing when the fixed
query trace is repeated. Report retained memory instead of calling reuse a
memory saving.

A passing isolated screen authorizes a separate integration review, not a
public server speedup claim. That review chooses the long-lived workspace
owner, wires GRAPH.VECSEARCH/related vector commands and GRAPH.RAG consistently,
and retains existing synchronization. End-to-end latency may still be governed
by vector decoding/distance, graph expansion or the global command mutex.
