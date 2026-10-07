# HNSW query workspace screen

`vector_workspace.zig` compares the existing allocating `control` search with
the explicit caller-owned `workspace` search on one deterministic write-buffer
fixture. It checks ordered node IDs and exact f32 distances before timing,
computes exact top-10 recall outside the timed region, and emits a checksum so
the timed work cannot be optimized away.

The intended first screen is:

```text
COUNT=10000 DIM=64 QUERIES=100
vector-workspace control COUNT DIM UNIQUE_QUERIES timed
vector-workspace workspace COUNT DIM UNIQUE_QUERIES timed
# Sparse-ID control and repeated steady trace (STRIDE then TOTAL_QUERIES):
vector-workspace workspace COUNT DIM UNIQUE_QUERIES timed 64 20000
```

The optional `STRIDE` (default `1`) spaces live node IDs; `TOTAL_QUERIES`
(default `UNIQUE_QUERIES`) repeats the fixed query set. `timed` emits cold-query time,
steady p50/p99, QPS, process CPU/query, RSS before/after build, allocation
deltas as `null` (timed mode uses libc directly), retained scratch bytes as
`null`, and checksums. `accounting` runs the same phases with counted libc
wrappers and reports per-phase allocation deltas, retained/peak bytes, and a
zero-live teardown record.

Use a temporary root shim exporting `HnswIndex`, `SearchWorkspace`, and
`VectorStore` when compiling the diagnostic. The fixture stays write-buffer
only; saved `.vvf`/`.vhi` handling is a separate experiment.

`workspace_capacity` reports visited bits, candidate/heap backing capacities,
reconciled expected scratch bytes and RSS after queries. RSS fields are
snapshots; cold timing means cold workspace after graph correctness warmup.
The pinned bitset owns one header word plus rounded-up mask words. Separate
accounting rejects retained-capacity mismatch, steady live growth and leaks.

Run the frozen first screen with `scripts/run_vector_workspace.py --binary PATH`
from `bench/loadtest/`; the script records fresh-process resource guards and
stops unstable results. See `docs/vector-workspace-results-2026-09-23.md` for
the first completed decision.
