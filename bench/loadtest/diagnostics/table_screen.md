# Table screen diagnostic

`table_screen.zig` compares the production direct map (`std.StringHashMap`) with
the pinned `std.array_hash_map.String(Entry)` implementation. Both variants use 256 maps,
Wyhash seed 1 for stripe selection, seed 0 inside each map, real
`ConcurrentKV.Entry`, separately allocated 16-byte binary query keys plus
separately owned map keys, and the same precomputed trace.

Build it with a temporary root shim; production sources and `build.zig` stay
unchanged:

```zig
pub const ConcurrentKV = @import("src/engine/kv/concurrent_kv.zig").ConcurrentKV;
pub const main = @import("bench/loadtest/diagnostics/table_screen.zig").main;
```

Run one fresh process per matrix cell:

```text
table-screen direct|dense COUNT VALUE_BYTES timed|accounting|selftest [OPS]
```

`timed` reports wall and process CPU monotonic nanoseconds and an observable
checksum for
random hits, misses, a 1,024-key hot set, equal-size overwrites, an 80/20
lookup/overwrite mix, and 10% delete / 10% same-key reinsert / 80% read
churn. Each delete is followed immediately by reinsertion; the eight reads
then access that recently reinserted key. The trace is generated before
timing; RSS reads, validation, setup, warmup, and JSON formatting are outside
the timed loop. The optional operation count can be up to 10 million while the
precomputed trace is capped at one million entries and cycled. `accounting`
wraps libc allocation and reports requested live and peak bytes, allocation
counts, RSS, map capacities, and exact pinned dense index headers/slots plus
nominal data bytes. The runner reconciles every accounted live byte against
fixtures, owned keys, heap values, and both map allocations. Peak requested
bytes also include temporary growth and an untimed value-validation oracle. `selftest` performs randomized binary-key
put/get/overwrite/delete/reinsert checks, moved-entry checks, equal-size
overwrite no-allocation checks, and allocator-balance checks.

The diagnostic is single-threaded and does not model `ConcurrentKV` locks,
expiry, eviction, persistence, or TCP request handling. Timed GET checks consume
value length, boundary bytes, and inline metadata; full value validation runs
after each timed phase. `post_flush` records RSS with fixture allocations still
live after the table is destroyed, while `completion` is emitted after all
fixtures are freed and the counted allocator must be balanced at zero.
