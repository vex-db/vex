# Direct versus dense map: first local screen

The dense standard-library map is not ready to replace Vex's direct map. It
saves requested storage above the direct map's growth boundary but costs more
below it. Timing stopped at the preregistered repeatability guard, so this
experiment establishes no accepted speed improvement.

## Memory result

These are **requested bytes for the table, owned keys and values**, excluding
benchmark fixtures. MiB means 1,048,576 bytes. Both variants use the real
64-byte Vex Entry, 16-byte owned keys, 256 stripes, libc allocation and natural
incremental growth. The comparison changes the complete map implementation,
including probing, hash storage, load factor and dense-array growth.

| Keys | Value bytes | Direct MiB | Dense MiB | Dense change |
|---:|---:|---:|---:|---:|
| 800,000 | 32 | 93.53 | 105.46 | +12.8% |
| 1,000,000 | 32 | 177.26 | 108.51 | −38.8% |
| 800,000 | 256 | 288.84 | 300.77 | +4.1% |
| 1,000,000 | 256 | 421.41 | 352.65 | −16.3% |

At one million keys the direct maps reserve 2,097,152 slots; the dense arrays
reserve 1,064,192 positions plus their separate probe indexes. At 800,000 keys
the direct maps reserve only 1,052,672 slots, while dense capacity is unchanged.
The observed difference is −68.75 MiB at one million and +11.93 MiB at 800,000.
This is why one favorable key count cannot establish a universal density win.

All 16 accounting processes completed. Every phase reconciled tracked live
bytes to fixture storage + owned keys + heap values + actual map backing
allocations. Equal-size overwrite/mixed phases made no new allocations,
checksums matched, and tracked live bytes returned to zero after destruction.
The recorded RSS includes fixture allocations and libc retention; it is not
Vex server RSS. Loaded/post-flush/post-destruction RSS and allocation peaks are
preserved in the result artifact, including unfavorable cases.

## Speed result and stop

The first timing case was 800,000 keys with 32-byte values, using ten million
operations per phase over a precomputed one-million-element trace. Dense
random-hit repetitions were 240.10 and 277.84 ns/op, a 14.57% variation, above
the 5% stop limit. The single direct observation was 163.73 ns/op. The ABBA
sequence stopped after its third process; these incomplete observations do
not establish a repeatable speed ratio. No timing retry was performed.

Remaining memory-only cells were completed separately after the stop. No
further speed cells or server integration were attempted. Churn in this
screen is 10% deletion, 10% immediate same-key reinsertion, and 80% reads of
the recently reinserted key; it does not model arbitrary replacement keys.
GET timing consumes boundary bytes and metadata, not a complete network
response copy. There are no stripe locks or concurrent clients in this test.

## Validation and provenance

- Native ARM64 Docker, pinned Zig `0.17.0-dev.314+eae06cf5c`, libc linkage.
- Both ReleaseSafe and ReleaseFast compiled. Four randomized selftests and
  eight heap-value smoke processes passed, including capacity and byte checks.
- Python runner selftests reject malformed counts, missing post-flush data,
  incorrect allocation totals, checksum changes and resource/variance errors.
- Each measurement had a fresh process, immutable binary hash, one CPU quota,
  CPU 0 affinity and 1.5 GiB memory/swap limit. Other local workloads remained
  running; CPU affinity does not isolate the host. No accepted accounting cell
  hit the throttling, swap, low-memory or memory-event guard.
- A preliminary invocation exposed a histogram-reporting bug before any cell
  was accepted. Explicit outer braces fixed Zig's `if`/`for`/`else` binding;
  the corrected binary was smoke-tested before the retained matrix.

Astra owned strategy and independent validation; Luna implemented the bounded
diagnostic; the parent ran Docker, corrected measurement defects and applied
stop gates. No production KV source was changed.

Portable evidence: [table-screen-2026-09-23.json](../bench/loadtest/results/table-screen-2026-09-23.json).
Raw local artifacts: `bench/loadtest/runs/2026-09-23-table-screen/`.

The next KV candidate remains grouped probing with direct entries or a
separately measured reduction in repeated hashing. Dense storage remains a
memory hypothesis, with an explicit growth-boundary tradeoff, rather than an
accepted speed optimization.
