# Direct versus dense table screen

This is a diagnostic design, not an engine change or a claim of improvement.
Keep the real 64-byte `ConcurrentKV.Entry`, its 32-byte inline storage, libc
allocation, 16-byte owned keys and 256 independently hashed stripes. The
rejected separately allocated Entry prototype is not a candidate in this screen.

## Pinned API and allocation facts

Sources are the copied compiler files under
`bench/loadtest/runs/2026-09-23-table-screen/`, and `hash_map.zig` under
`bench/loadtest/runs/2026-09-22-memory-density/`.

* `array_hash_map.String(Entry)` is allocator-passing: initialize `.empty`;
  call `put(gpa, key, value)`, `getOrPut(gpa, key)`, `getPtr(key)`,
  `fetchSwapRemove(key)` and `deinit(gpa)`. Verify the exported `std` alias in
  the pinned compiler rather than assuming an older managed API.
* `entries` is a public `MultiArrayList(Data)` whose fields are a `u32` hash,
  a 16-byte key slice and a 64-byte value. The structure-of-arrays allocation
  is exactly `DataList.capacityInBytes(entries.capacity)`: 84 bytes per
  reserved dense position here. Do not multiply by `@sizeOf(Data)`; its
  struct padding is not allocated for each position. See ArrayMap lines
  124–134 and MultiArrayList lines 664–667.
* The index is a separate allocation. Its four-byte header is followed by
  `2^bit_index` records, each containing an entry index and a probe distance.
  Both fields are `u8`, `u16` or `u32` for bit indexes at most 8, at most 16,
  or larger: record widths are 2, 4 or 8 bytes. Normal index capacity is
  floor(index length × 3/5). See ArrayMap lines 1551–1566, 1583–1631 and
  1643–1694. At about 3,906 live keys per stripe the index has 8,192 records
  of four bytes, approximately 8 MiB across 256 stripes, plus headers.
* Dense array growth allocates a new array, copies live fields and frees the
  old array. Its capacity is `minimum + minimum/2 + init_capacity`, where
  `init_capacity=max(1, std.atomic.cache_line/64)` for this Data. Print the
  target's actual cache-line constant. The nearby sequential-insertion array
  capacities are approximately 2,770 then 4,158. Index and dense array growth
  happen separately; account for allocation peaks. See MultiArrayList
  lines 525–575. `map.capacity()` is the smaller usable capacity of the two
  structures, not either allocation length (ArrayMap lines 456–460).
* Lookup uses Robin Hood linear probing in the index, followed by access to
  the stored hash/key and dense value. `fetchSwapRemove` copies out the
  removed key/value, moves the last dense element into its position, repairs
  that element's index and backward-shifts the affected probe cluster. It
  does not free caller-owned keys or value buffers. Use swap removal, not
  order-preserving removal. See ArrayMap lines 677–697 and 1160–1254.
* The string context stores the low 32 bits of seed-zero Wyhash. The direct
  map uses full 64-bit seed-zero Wyhash and a seven-bit fingerprint. Preserve
  seed-one stripe selection for both. This compares complete library table
  designs; hash width, caching, probing and index load differ as well as
  storage layout. Do not describe a difference as an isolated layout effect.
* `getOrPut` attempts capacity for `len+1` before checking for an existing
  key (ArrayMap line 319). Overwrites must use `getPtr` and mutate the owned
  Entry in place, as Vex does; otherwise the diagnostic creates artificial
  overwrite allocations. Do not pre-reserve either implementation.

## Bounded matrix

Use 16 fresh processes: two key counts (800,000 and 1,000,000), two fixed
value sizes (32 and 256 bytes), and direct/dense/dense/direct for each case.
Generate the same natural prefix key sequence and misses before timing.
Route every operation through the same seed-one stripe calculation.

800,000 keys average 3,125 per stripe, below the direct table's 3,276-live-key
limit at capacity 4,096. One million average 3,906, above that limit. These
counts bracket the direct growth boundary while most dense arrays remain
at the same capacity. Report actual per-stripe count/capacity histograms;
hash distribution means individual stripes cross the boundary separately.
Do not assume exact balance or change counts after seeing which layout wins.
The dense layout can lose memory below the direct growth boundary; that is
a result this screen is intended to expose.

Each process exercises the same precomputed bounded traces: random hits,
guaranteed misses, equal-size overwrites, a skewed hot set, and 10% delete / 10% same-key reinsert / 80% read churn. Each delete is
followed by reinsertion and eight reads of that recently reinserted key. Record loaded capacity and RSS separately. Keep operations and seeds identical, print observable
checksums, and use explicit failures that remain active in ReleaseFast.
Key generation, reference-model work, sorting and reporting stay outside
timed regions. The execution protocol uses 10 million operations per phase, cycling a
one-million-element trace; accounting uses twice the live key count.

Run native-libc timing and RSS measurements first in each fresh process,
without allocator counters. Report loaded, post-churn and post-flush RSS.
Then a separate fresh-store pass may use the counted allocator to obtain
requested bytes, allocation counts and peaks. Do not report second-pass RSS
as fresh-process density or second-pass timing as native table performance.
Alternatively, run the two passes in separate fresh processes and record
that the 16 logical cells have 32 process invocations.

The reference checks cover every trace, binary keys, hit/miss results,
content after overwrites, lookup of the element moved by swap removal,
counts after churn, and zero live tracked requests after full destruction.
For removed Entries, free the owned key and only the heap value allocation;
inline bytes live inside the copied Entry. Neither implementation may keep
a pointer into an Entry across a mutation. Equal-size overwrites must make
zero new allocations. Allocation-failure checks are separate from timing.

## Interpretation

This is a single-thread table screen. It does not test reactor concurrency,
TCP throughput or p99 request latency. Dense contiguous storage avoids one
malloc per Entry but still adds an index lookup; that cost must be measured.
Report capacities, hash behavior and retention alongside time medians and
both paired comparisons. Include unfavorable growth and churn results.

The parent execution protocol owns numerical acceptance and stability gates.
No result here promotes an engine default. Only a repeatable candidate that
passes density, ownership, churn and allocation-failure checks should proceed
to a separately preregistered server experiment.
