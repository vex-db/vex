# HNSW workspace: first local result

Scratch reuse removes three allocation calls per steady query, but the first
10,000-vector screen does not meet the speed acceptance threshold. The median
observed QPS change is +1.46%; paired changes are +0.54% and +2.39%, both below
5%. The prototype remains an explicit research API with no server caller.
No Vex command, concurrency behavior or persistence format was changed.

## What changed

`SearchWorkspace` retains a visited bitset, sorted candidate buffer and pending
heap for one serialized caller. `searchWithWorkspace` uses the same beam loop
as existing `search`; the latter remains the control. Visitation is cleared
on every query. Returned results have independent ownership. There is no
change to distance computation, neighbor order, ef, graph construction,
vector encoding or insertion scratch ownership.

Astra specified the contract, reviewed the implementation and added difficult
retained-buffer growth/recovery tests. Luna implemented the bounded prototype
and diagnostic. The parent corrected and checked measurement details, ran the
native Docker experiments, and applied the acceptance gates.

## Observations

One immutable deterministic fixture per fresh process: 10,000 vectors, 64
float dimensions, k=10, ef_search=50, 100 fixed normalized queries. Each timed
process repeats that query set 1,000 times after warmup. All timing uses raw
libc allocation; separate accounting processes use counters. There were no
concurrent task-owned builds or benchmarks.

| Timed sequence | QPS | CPU/query, µs | p50, µs | p99, µs |
|---|---:|---:|---:|---:|
| Control 1 | 12,925 | 77.212 | 75.208 | 120.166 |
| Workspace 1 | 12,995 | 76.679 | 74.791 | 115.541 |
| Workspace 2 | 13,235 | 75.419 | 74.209 | 105.167 |
| Control 2 | 12,925 | 77.220 | 75.625 | 111.292 |

Timing repetition stayed within the 5% guard. CPU/query moved in the same
direction and paired p99 observations did not regress, but neither QPS pair
reached the 5% minimum. The small observed improvement is not an accepted
production speedup. No larger population or saved-data timing expansion was
started after this result.

The separate accounting runs used the same live population with contiguous
IDs and IDs spaced 64 apart:

| ID stride | Control allocation calls/query | Workspace allocation calls/query | Retained workspace bytes |
|---:|---:|---:|---:|
| 1 | 4 | 1 | 3,736 |
| 64 | 4 | 1 | 82,480 |

Workspace steady scratch allocation/free counts are zero after warming the
fixed query set; the independently owned output still allocates. Counts are
successful raw allocation calls, not resize/remap operation counts. Retained
bytes reconcile exactly to the bitset allocation (including its header) plus
actual candidate and heap capacities. The sparse case illustrates why reuse
is not itself a memory saving: visited storage follows the maximum node ID.
Every accounting process returned tracked live bytes to zero on destruction.

Control and workspace produced identical ordered IDs and f32 distance bits.
Exact recall@10 was 0.844 for this synthetic fixture, identical for both
variants and both ID strides. This experiment preserves baseline recall; it
does not establish production retrieval quality or improve ANN accuracy.

## Validation and limits

All 263 ReleaseSafe unit tests passed, including existing graph/vector and
persistence tests. New checks cover retained result ownership, empty/nonempty
indexes, k and ef changes, filtering, duplicate ties, sparse growth, reuse
across nonempty sizes, independently owned workspaces and deterministic OOM
recovery for visited/candidate/heap/output allocations. Both diagnostic build
modes compiled and eight sparse fixture smoke cells passed.

The runner independently validates binary identity, configuration, observable
checksums, identical recall, timing plausibility, memory accounting, teardown,
resource events and repeatability. Negative checks reject corrupt result
records, leaks, hidden instrumentation and unstable timings.

RSS values are snapshots. The external cgroup peak also includes the Python
runner, so neither is presented as the standalone engine's peak RSS. The
`cold_elapsed_ns` record means cold workspace after the correctness pass has
warmed the graph; it is not a cold-process/cache query. The Docker VM shares
host resources with unrelated workloads.

Only write-buffer vector storage was measured. Fresh mapped `.vvf`/`.vhi`
queries, update/tombstone batches, 100K populations, 384-dimensional vectors,
and server integration remain separate work. The mapped insertion
scratch-lifetime concern in the handoff remains unresolved by this prototype.

Portable evidence: [vector-workspace-2026-09-23.json](../bench/loadtest/results/vector-workspace-2026-09-23.json).
Raw artifacts: `bench/loadtest/runs/2026-09-23-table-screen/vector-matrix/`.

## Next bounded assignment

Astra should use a local baseline profile to choose among vector lookup,
distance evaluation and candidate maintenance; source inspection alone has
not established which dominates. Luna can extend the standalone diagnostic
with the exact chosen instrumentation and one isolated implementation after
that attribution. Preserve this validated control and result/recall checks.
Do not widen the algorithm, alter ef or build a custom vector index to obtain
a faster number. For KV, grouped probing or reduced repeated hashing remains
separate from vector search and from the rejected universal dense-map swap.
