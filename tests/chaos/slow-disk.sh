#!/usr/bin/env bash
# slow-disk.sh — validates S2 (appendfsync) under a slow underlying disk.
#
# Approach: use a FUSE shim or LD_PRELOAD to add 100ms latency to fsync().
# Then run a write workload under each appendfsync mode:
#
#   - everysec: hot-path latency should be unaffected (background thread
#     absorbs the cost). Throughput should remain within ~5% of baseline.
#   - always:   hot-path latency rises to ~100ms+ per write. Throughput
#     drops. No writes are silently dropped.
#   - no:       no fsync issued, throughput identical to baseline.

set -euo pipefail

# TODO: implement.
echo "slow-disk.sh: TODO — scaffold only, see README.md"
exit 0
