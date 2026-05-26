#!/usr/bin/env bash
# fill-disk.sh — validates S4 (disk-full STOP-WRITE mode).
#
# Setup: mount a small tmpfs (e.g. 32MB) at /tmp/vex-chaos-fs and point
# vex's data dir at it. Then:
#
#   1. Drive writes until free space drops below ~1MB.
#   2. Continue writing → expect `-MISCONF Errors writing to the AOF file`
#      from the next write.
#   3. Verify reads still succeed.
#   4. Free space (delete a large dummy file inside the tmpfs).
#   5. `redis-cli CONFIG SET appendfsync no` → STOP-WRITE clears.
#   6. Confirm writes succeed again.

set -euo pipefail

# TODO: implement.
echo "fill-disk.sh: TODO — scaffold only, see README.md"
exit 0
