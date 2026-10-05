# Next data-structure experiments

Status: the direct/dense diagnostic has completed memory accounting; timing
stopped at the repeatability guard. See [measured results](table-screen-results-2026-09-23.md).
The shared 256-stripe KV architecture is unchanged. The opt-in vector workspace
[screen](vector-workspace-results-2026-09-23.md) passed correctness but did not
reach its speed threshold; hypotheses below are not accepted speedups.

Priority correction, 2026-09-24: the user rejected the practical value of the
dense-table outcome. This candidate is closed as a general replacement; the
remaining table ideas below are deferred hypotheses, not the active queue.
Follow the server-throughput priority in
[the execution plan](performance-execution-plan.md#priority-correction--2026-09-24).

## What the evidence supports

The current table is `std.StringHashMap(ConcurrentKV.Entry)`. Each reserved
slot has a 16-byte key slice, a 64-byte Entry and one metadata byte. In the
measured million-key workload, the 256 tables reserve 2,097,152 slots and
request 162.01 MiB for their backing allocations. Large empty value slots
are a concrete memory target. Key allocations and large value buffers are
additional costs.

The rejected pointer-per-entry prototype saved about 35 MiB but failed the
local server throughput and latency gates. That rejects that implementation;
it does not establish that every indexed or indirect layout is slower.
Contiguous backing storage has different locality and allocation costs from
a separately allocated object for every key. See the
[completed experiment](memory-io-followup-2026-09-22.md).

The pinned standard table already has seven-bit fingerprints. Its lookup
loop checks one slot at a time. Grouped probing would change how those
fingerprints are searched, not introduce fingerprints for the first time.
[Abseil's design](https://abseil.io/about/design/swisstables) describes
filtering multiple candidate slots together. This is a design reference,
not evidence of a Vex speedup or a reason to add a C++ dependency.

## Order of work

1. **Establish a representative table benchmark.** Compare the current direct
   table with a standard-library dense indexed map, after verifying the
   latter's API and allocation layout in the pinned compiler. Use real
   64-byte Entries, owned keys and reusable value buffers. This establishes
   whether a library solution is sufficient before building a custom table.

2. **Try grouped probing with direct entries.** This is the first custom
   table candidate for speed: inspect a group of metadata bytes together,
   then compare only keys with matching fingerprints. Keep the allocator,
   stripe count, value representation, load threshold and growth policy
   fixed initially. That isolates the lookup algorithm. It will not, by
   itself, remove the current empty-slot memory cost. Verify actual generated
   code on ARM64; a vector expression alone is not proof of faster probing.

3. **Evaluate dense storage independently.** Store live Entries in a compact
   backing array with small table indexes instead of allocating each Entry
   separately. Measure spare array capacity and index overhead, not only
   live-entry bytes. Deletion must repair any moved entry's index, and growth
   must preserve ownership under the stripe lock. This is primarily a memory
   candidate until real request throughput also improves.
   [F14's layout discussion](https://engineering.fb.com/2019/04/25/developer-tools/f14/)
   explains the distinction between direct, node and dense indexed storage;
   its results do not predict Vex's results.

4. **Reduce repeated hashing as a separate small experiment.** A GET currently
   computes a seed-1 hash for stripe selection and a seed-0 hash for the map.
   One well-distributed hash could supply disjoint stripe, bucket and
   fingerprint bits, passed through the adapted lookup API. Keep the current
   two-hash implementation as the control. Never reuse the same low bits for
   stripe and bucket selection: that recreates the earlier clustering issue.
   Keep this independent of a map replacement so any benefit is attributable.

An Entry smaller than 64 bytes is another candidate, but removing the integer
field is not a harmless packing change. The cached integer currently coexists
with the original string allocation, whose length and ownership are used by
other paths. A representation change must first define string/integer
conversion, TTL, accounting, COPY and allocation-failure behavior. Saving eight
bytes per reserved slot would remove 16 MiB of requested backing storage at
the measured capacity; this is arithmetic, not measured RSS or speedup. A
56-byte stride can cross cache-line boundaries, so smaller is not necessarily
faster. Integer access must also preserve its required atomic alignment.

Do not bundle a new allocator, short-key format, deferred resizing or worker
ownership into the first table prototype. Each adds a separate hypothesis.
Consider bounded/incremental growth only if measurements show resize pauses
are material. Likewise, raising the load threshold does not automatically
cross the million-key workload's power-of-two capacity boundary.

## Bounded implementation handoff

The next implementation owns a new table diagnostic under
`bench/loadtest/diagnostics/` and its build/run notes. Reuse the existing
counted allocator and local Docker tooling where practical. Do not change
`src/`, introduce dependencies, or enable an experimental engine default.

The existing `bench/hashmap_bench.zig` is not an acceptance benchmark:
`vendor/verztable` is missing in this checkout; its payload is a slice rather
than a real Entry; lookup results are discarded or checked only by assertions;
and `bench-hashmap` disables libc linkage, unlike the actual release server.
Unused lookup results may be optimized away in ReleaseFast. Use an observable
checksum and explicit failures that remain active in optimized builds.

An independent read-only review found a cached `verztable-0.1.0` archive,
although the required vendor directory remains absent. That version uses
displacement-chain lookup, not grouped SIMD lookup. Its string-key bucket
stores a full 64-bit hash alongside the key and value. With a 64-byte Entry,
the nominal bucket plus metadata is about 90 bytes per slot versus the
standard table's 81: roughly 18 MiB more at the measured capacity, before
headers/alignment. Its 87.5% load threshold also does not avoid the 8,192-slot
capacity for about 3,906 live keys per stripe. It is an optional speed/churn
comparator, not the leading density candidate. Verify revision and provenance
before use; the cached version is not established as the missing vendor
revision. The pinned dense-map source/layout still needs inspection.

First screen the current table against the pinned standard-library dense map.
Use identical precomputed operation traces and keys, native ReleaseFast,
libc allocation, monotonic time and fresh processes. Generate keys outside
timed regions. Separate uninstrumented timing from allocation accounting.
Report actual entry/key/index capacities and growth peaks for both layouts.

Cover random hits, misses, overwrites, new-key insertion, deletion/reinsertion
and a skewed hot set. Test occupancies immediately before and after a growth
boundary; one favorable key count is insufficient. Verify every operation
against a simple reference model, including binary keys and moved entries.
Keep equal-size overwrite allocation counts at zero and live requested bytes
at zero after destruction. Record loaded RSS, churn RSS and post-flush RSS.

Start with 32-byte and 256-byte values and fixed 16-byte keys. Only a promising
candidate expands to 128 B, 1 KiB and 4 KiB, longer keys, and the 33/257/4097
allocation boundaries. A lookup microbenchmark win does not authorize server
integration or a public performance claim.

Before server integration, declare the candidate's goal and acceptance gates.
For a speed candidate, require a repeatable table improvement and then at
least 5% mixed-P1 server throughput improvement exceeding observed variation,
with both paired comparisons positive. For a memory candidate, require at
least 10% loaded-RSS reduction in the million-key small-value case and no
material throughput loss. Both must preserve correctness, zero-allocation
overwrites and allocation-failure safety. Server guards retain throughput
within -2%, CPU/op within +2%, p99 within +5% and RSS within +2% on other
checked workloads; retained-memory regressions require explicit rejection
or a revised design, not omission from the report.

Use the same clock-repaired client on both sides, fresh control/candidate/
candidate/control processes, fixed resources and existing validity checks.
Stop for more than 5% repeat variation, errors, clock anomalies or resource
pressure. Only a passing two-worker pilot expands to GET/SET/mixed and
one/two/four-worker scaling. CPU sets in Docker Desktop do not establish
exclusive physical cores.

## Separate I/O follow-up

The existing default-off SQ-head cache had a +5.8% local pilot, but its
confirmation was inconclusive and remains unpromoted. The repaired client
makes a fresh, complete confirmation reasonable once local repeatability is
established. Preserve the original confirmation gates and do not combine
that experiment with a new table.

The available local profile contains substantial kernel work. A table
improvement may increase memory capacity or isolated storage speed while
producing only a small TCP throughput gain. Claim the benefit actually
measured; do not promise a multi-fold request speedup from a map replacement.

## Extending the work to graph and vector

The same table algorithm can be evaluated at existing dictionary call sites
across Vex. Each store should retain its own key/value representation and
locking. In particular, do not impose the 64-byte KV Entry on a graph's
four-byte NodeId. The first implementation remains the bounded KV/table
diagnostic above; this extension identifies later consumers and their gates,
not an engine-wide migration or a new storage framework.

Current candidates, verified in source:

| Consumer | Existing representation | Role of a candidate table |
| --- | --- | --- |
| Graph node lookup | `StringHashMap(NodeId)` in `graph.zig` | Resolve external keys to stable node IDs |
| Graph properties | Composite `u64` to bytes, plus entity-to-property-ID lists in `property_store.zig` | Point lookup, mutation and property retrieval |
| Vector write buffer | Composite node/field `u64` to `[]f32` in `vector_store.zig` | Find newly inserted/updated vectors |
| Vector deletion metadata | Composite IDs in a hash set | Preserve deletion/overwrite precedence over mapped files |
| HNSW upper layers | Node-ID maps to neighbour slices | Optional later target, only if profiles justify it |

Graph traversal already uses CSR adjacency arrays with appended delta edges.
HNSW layer zero already uses direct node-ID indexing for neighbour slices.
Keep these representations as controls. A general hash table provides exact
key lookup; vector nearest-neighbour search still needs its search index.
[HNSW's paper](https://arxiv.org/abs/1603.09320) describes the graph search,
and [Faiss's implementation notes](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes#--indexhnsw-variants)
show HNSW search and its underlying vector storage as separate components.

The more specific vector hypothesis is faster access to vector payloads.
`HnswIndex.dist` calls `VectorStore.getById` for each evaluated candidate.
The current path checks tombstones and the write-buffer map; mapped vectors
then use binary search by NodeId and decode into one of two shared scratch
buffers. Evaluate a per-field NodeId-to-row/offset index, with direct arrays
for sufficiently dense IDs and a compact map for sparse IDs. Account for the
additional index memory. Preserve update/deletion precedence and rebuild or
invalidate offsets after save/remap. Treat decoding and distance computation
as a separate experiment; the current dot-product source is a scalar loop,
but inspect generated code before assuming the compiler does not vectorize it.

Another bounded vector candidate is reusing search workspaces. Each
`searchLayer` currently allocates/clears a capacity-sized visited bitset and
creates temporary candidate/working lists. Measure reuse without changing
search order or recall. Any concurrent design needs query/worker-owned
workspace and decoding buffers; retain current synchronization until their
lifetime and ownership are safe. The normal reactor command path currently
acquires a shared `kv_mutex` after the graph lock, so a faster table alone
does not establish concurrent graph/vector scaling.

After a table candidate passes its initial screen, test graph node resolution,
property reads/updates, and traversal with both compacted and pending edges.
Measure vector search separately for write-buffer and saved/reloaded data,
including sparse IDs and updates/deletions. Hold dataset, dimensions, HNSW
parameters and queries fixed; report recall@K against an exact reference as
well as p50/p99, QPS, CPU/query, build time and loaded/peak/retained memory.
An apparent speedup from reduced recall does not pass.

Use `GRAPH.RAG` as the final end-to-end workload: vector search, node/property
retrieval and graph expansion. Stable node IDs must survive table rehashes,
array relocation and persistence reloads. Neither the existing design-only
vector benchmark document nor its speculative speedup estimates constitute
measured results. No graph/vector code or benchmark runs changed in this
investigation.
