#!/usr/bin/env bash
# kill-and-restart.sh — validates S1+S2+S3 (atomic snapshot/AOF + appendfsync).
#
# Repeatedly:
#   1. Start vex with appendfsync=everysec, redis-benchmark in the background.
#   2. After a random delay (50ms..2s), kill -9 the vex process.
#   3. Restart vex.
#   4. Verify: startup succeeds, snapshot/AOF load without corruption,
#      lost writes are bounded by the appendfsync window (<= ~1s).
#
# Repeat 20 iterations. Any iteration that fails fails the script.

set -euo pipefail

# TODO: implement.
echo "kill-and-restart.sh: TODO — scaffold only, see README.md"
exit 0
