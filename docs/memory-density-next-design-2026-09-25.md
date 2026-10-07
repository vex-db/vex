# Next memory-density candidate — 2026-09-25

The smallest credible next experiment is a **32-byte direct Entry with optional metadata for TTL, integer and eviction state**. Keep the current striped `StringHashMap`, native allocator and reusable heap buffers. This is a design for a bounded prototype, not an accepted implementation or a throughput claim. The expiry/FLUSHDB fixes release dead storage; they do not reduce the loaded representation.

## Evidence and byte budget

Inputs are the local native ARM64 Docker baseline in `bench/loadtest/results/memory-lifecycle-2026-09-25.json` and the native allocator diagnostic in `bench/loadtest/runs/2026-09-25-memory-lifecycle/native-memory-curve.jsonl`. Both naturally grow 16-byte keys and 256-byte values. The diagnostic measures allocation capacity; server measurements include sockets, workers and allocator retention. These are memory-only observations, not the earlier AWS throughput measurements.

Current Entry is 64 bytes: 32-byte inline/heap union, three 8-byte fields (`expires_at`, `last_access`, `int_value`), then inline length, flags and padding. The table also reserves a 16-byte key slice and one control byte per slot. At these capacities its exact requested backing allocation is `81 * capacity + 24 * 256` bytes. A 32-byte Entry with unchanged alignment makes that `49 * capacity + 24 * 256`: **32 bytes saved per reserved slot**, without reducing the number of live values or their capacity.

| Keys | Reserved slots | Vex RSS MiB | Redis | Dragonfly | Entry saving MiB | Vex minus saving |
|---:|---:|---:|---:|---:|---:|---:|
| 600,000 | 1,048,576 | 262.22 | 236.64 | 184.06 | 32 | 230.22 |
| 750,000 | 1,048,576 | 306.07 | 283.53 | 238.87 | 32 | 274.07 |
| 800,000 | 1,048,576 | 321.02 | 301.05 | 249.61 | 32 | 289.02 |
| 900,000 | 2,097,152 | 430.39 | 336.38 | 271.14 | 64 | 366.39 |
| 1,000,000 | 2,097,152 | 459.58 | 371.66 | 292.64 | 64 | 395.58 |
| 1,200,000 | 2,097,152 | 517.91 | 457.34 | 339.38 | 64 | 453.91 |
| 1,500,000 | 2,097,152 | 605.42 | 561.41 | 448.50 | 64 | 541.42 |
| 1,600,000 | 2,097,152 | 634.61 | 588.36 | 470.06 | 64 | 570.61 |

The final column is arithmetic, **not predicted or measured RSS**. Real savings depend on residency, allocation rounding and process overhead. The native diagnostic reports almost all large-map backing pages resident at these flat stages, supporting a real table saving. The 1.2M point has only 3.43 MiB of theoretical margin below Redis, so it is a particularly useful falsification point. The 850k–1M growth-boundary region remains above Redis even with this change. Do not describe success on the two flat ranges as success over the entire curve.

A 40-byte Entry saves only 24/48 MiB and misses Redis at 600k by 1.58 MiB and at 1.2M by 12.57 MiB. A smaller field cleanup therefore does not meet the specified full ranges. Removing integer caching or narrowing timestamps/lengths solely to hit a size target would change behavior and is unnecessary here.

## Bounded representation

Use a 24-byte storage union (existing `pointer + usize length + usize capacity`, or 24 inline bytes) plus one 8-byte state word. Preserve full-width lengths, capacity, timestamps and integer values. Enforce the expected 32-byte size/alignment at compile time.

The state word has three forms, hidden behind Entry helpers: ordinary heap, inline length, or an aligned pointer to a 32-byte metadata object. Two low tag bits suffice; only strip those bits when converting a metadata pointer back. Metadata retains the current three i64 fields plus length/flags. Use the allocator's typed/aligned allocation guarantee and a small round-trip test; do not rely on unused high virtual-address bits or invent a new allocation arena.

Ordinary non-TTL strings under noeviction allocate **no metadata**. Their 256-byte value still uses exactly the existing heap pointer and overwrite capacity; a successful GET needs no extra pointer chase. Values up to 24 bytes remain inline. TTL, native integer caching, or configured LRU allocate metadata only for the affected entries. Preserve integer string-buffer ownership and existing integer formatting behavior rather than combining this experiment with a numeric representation rewrite. LRU must retain its timestamp whenever the policy is enabled, including the current behavior with an unlimited budget.

Allocate replacement metadata before changing a live entry, so OOM preserves its old state. Reuse it on repeated TTL/integer writes; free it when a plain SET removes its last need. DEL, expiry, eviction, import cleanup, FLUSHDB and destruction must release both payload and metadata exactly once. Extend the existing detached-expiry result to carry metadata ownership until after stripe unlock. The cursor must continue inspecting the actual current Entry under the stripe lock; do not add a separate expiry index.

This changes representation and Entry access helpers in CKV and the few worker paths reading Entry fields. It does not change probing, load factor, hashing, locks, allocator choice or reactor scheduling. Keep it separate from the release fixes so each binary and result has unambiguous provenance.

## Costs that can reject it

For `T` entries requiring metadata, requested-byte saving is `32 * capacity - 32 * T`; allocator chunk overhead further reduces RSS saving. A native glibc 32-byte metadata request is expected to occupy roughly a 48-byte chunk, to be verified by the existing allocation diagnostic. All-TTL/all-LRU density is therefore a different case, not covered by the common-case projection.

Values of 25–32 bytes lose the current inline representation and need heap storage. At 800k keys, 32-byte values can add roughly 36.62 MiB of native chunks against 32 MiB of table savings. This is an explicit likely regression and must be measured, not hidden. The extra state branch and metadata transitions can also cost speed. If preserving the 32-byte workload is a hard requirement, reject this candidate at that gate; do not silently grow it into a custom table or global allocator change.

This differs from the rejected per-entry pointer experiment: ordinary strings remain directly in table slots and allocate no entry object. It differs from the rejected dense-map screen: capacity/probing remain unchanged. It does not repeat the global SMP allocator experiment. Those prior results remain rejection evidence, not candidates to rerun unchanged.

## Minimal decision sequence

1. Extend the existing native accounting diagnostic with only this representation. Reuse the complete saved growth sequence through 1.6M, including 825k/850k/900k/1M. Require exact allocation reconciliation, unchanged keys/values, no allocations on equal-size 256-byte overwrite, and zero live allocations after destruction. Check 24/25/32/33-byte values, TTL refresh/removal, INCR transitions, LRU and allocation-failure ownership in focused correctness cases.
2. If accounting holds, run the same socket memory lifecycle against the candidate. Report current RSS separately from requested live bytes, reserved capacity and retained reusable pages. Reuse the idle-expiry/WATCH test and shrink/DEL/refill/FLUSH checks; requested-byte reduction alone does not establish RSS release.
3. Only after the memory gate, use the existing matched server/client throughput protocol at 600k, 800k, 1.2M and 1.6M, with the existing three rotated rounds and strict client/clock/identity gates. Preserve the Vex speed lead over both engines; compare against matched current Vex controls to expose a regression hidden by its existing lead. Keep the existing variation-above-5% inconclusive rule and do not rerun for favorable outcomes. Include the 32-byte value guard before promotion; no additional broad matrix is needed.

Acceptance wording should name the measured value size, TTL/eviction mode and key-count ranges. Until both RSS and throughput gates pass, the result is a byte-budget hypothesis only.
