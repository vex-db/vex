# Memtier 2.5.1 elapsed-clock repair

This patch changes the benchmark client only. It is a measurement repair, not a Vex optimization or evidence of improved Vex performance.

The original client derives request latency, run duration, deadline progress and client CPU utilization denominators from `gettimeofday`. A negative latency narrowed to `unsigned int` becomes approximately 4,294,967 milliseconds. Our raw artifacts contain that signature; the source mechanism supports a wall-clock reversal, but does not prove the VM clock's underlying cause.

The patch replaces all 25 application `gettimeofday` calls with one explicit shared helper. It uses `CLOCK_MONOTONIC` plus a fixed startup epoch offset, initialized before workers. Request, replay, cluster-response, duration and deadline paths therefore retain one timestamp domain. CPU-time clocks, libevent and vendored dependencies are unchanged. Clock failures abort without a realtime fallback. The new header is included in Automake's source list.

One stderr line begins `MEMTIER_CLOCK ` and contains JSON fields `clock_source`, `epoch_offset_us`, `source_tag`, and `source_commit`. It appears even for `--version`. Consumers must require one valid line for this patched client. Conversion is:

```
synthetic_epoch_us = Linux_VM_CLOCK_MONOTONIC_us + epoch_offset_us
```

Runtime timestamps remain epoch-shaped but are **not contemporaneous wall time after a wall-clock adjustment**. Align RSS samples using their Linux VM monotonic timestamp and this exact offset; do not use the macOS host monotonic clock. The realtime-then-monotonic startup reads introduce a small calibration lag; both client timestamps and converted sampler timestamps share the same offset. The helper does not change a host or container clock.

## Source and build

The adjacent JSON records the verified official tag commit, archive hash, patch hash and temporary source locations. The existing image reports version 2.5.1 with unavailable source SHA (`00000000`); that version label alone does not establish byte-for-byte equivalence to a rebuilt client. Pin the rebuilt image digest and use it for every cell of a new comparison.

Apply from a clean extracted source directory:

```sh
git apply /absolute/path/to/memtier-monotonic-clock.patch
python3 /absolute/path/to/check_memtier_monotonic_clock.py "$PWD"
autoreconf -ivf
./configure
make -j2
```

Use the official Ubuntu 22.04 build recipe: `build-essential autoconf automake libevent-dev pkg-config zlib1g-dev libssl-dev libboost-all-dev cmake flex`. Pin the base image digest and record resolved package versions for build provenance. The official recipe does not pin package versions. For the runtime image preserve the existing native ARM64 runtime libraries, or inspect the rebuilt binary's dynamic dependencies and include matching libevent, OpenSSL, zlib and C++ runtime libraries. No new library is required for the helper on Ubuntu 22.04.

Validation completed locally: clean-archive patch application; exactly the five intended original files changed plus the new header; all 25 replacements; no dependency edits; a deterministic C++11 test compiled across two translation units. The test simulates realtime moving backward 219 microseconds while monotonic advances 219 microseconds, checks timeval normalization and the single shared offset/metadata line, and verifies failure at each clock-read stage aborts. The native ARM64 client subsequently built successfully in the pinned Ubuntu-based image, with no OOM or memory-limit events. Its immutable image ID, binary hash and resolved package manifest hash are in the adjacent JSON; raw build logs and dynamic-library provenance are retained under `bench/loadtest/runs/2026-09-22-memtier-clock`. Keep the existing latency and resource guards enabled.
