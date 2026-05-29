#!/usr/bin/env bash
# Terminate the perf-testing instance brought up by aws-perf-up.sh.
# Pass the instance id via $INSTANCE_ID; the up-script prints it.
#
# Pairs with aws-perf-up.sh — symmetrical envvar surface.

set -euo pipefail

REGION=${AWS_REGION:-ap-south-1}
INSTANCE_ID=${INSTANCE_ID:?must export INSTANCE_ID (printed by aws-perf-up.sh)}

echo "[aws-perf-down] terminating $INSTANCE_ID in $REGION..."
aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$INSTANCE_ID"
echo "[aws-perf-down] $INSTANCE_ID terminated"
