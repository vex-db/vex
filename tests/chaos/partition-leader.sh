#!/usr/bin/env bash
# partition-leader.sh — validates S10 (epoch mechanism) + S12 (ack frames).
#
# Setup: 3-node cluster (1 leader + 2 followers) + 1 sentinel.
# Steps:
#   1. Drive writes against the leader.
#   2. Use `pfctl` (macOS) or `iptables` (linux) to drop traffic from the
#      leader to both followers — leader is partitioned.
#   3. Sentinel detects dead leader (heartbeat timeout × 3).
#   4. Sentinel runs election: highest-priority alive follower wins.
#   5. Sentinel sends VEX.PROMOTE <epoch+1> to the chosen follower.
#   6. Heal the partition.
#   7. Verify: old leader self-demotes when it sees the higher epoch in a
#      heartbeat. No split-brain — exactly one leader at the end.

set -euo pipefail

# TODO: implement. Depends on sentinel/ being functional past scaffold.
echo "partition-leader.sh: TODO — scaffold only, see README.md"
exit 0
