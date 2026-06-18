#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Autorun the vex/Redis/Dragonfly throughput benchmark on a throwaway EC2 fleet:
#
#   terraform init && apply  ->  poll the server's serial console for the CSV
#                            ->  save it  ->  destroy
#
# Results are exfiltrated via the EC2 serial console (get-console-output), NOT
# S3: this account's principal is denied IAM, so there is no instance profile
# to authorize an upload. The orchestrator script prints results.csv to
# /dev/console between ===VEXBENCH-RESULTS-START=== / ===VEXBENCH-RESULTS-END===
# markers (and repeats it a few times, since console output is captured lazily).
#
# Teardown is robust: we ALWAYS `terraform destroy` on exit (trap), even if the
# results never appear within the timeout. As a backstop the instances also
# self-terminate (instance_initiated_shutdown_behavior=terminate + the
# orchestrator's 70-min safety timer + the clients' timer), so a crashed laptop
# / lost creds can never leave the fleet running indefinitely.
#
# Usage:
#   ./run.sh                      # full cycle, results -> ./bench-results/
#   POLL_TIMEOUT=2700 ./run.sh    # override max wait for results (seconds)
#   KEEP=1 ./run.sh               # skip the final destroy (debug; clean up yourself!)
#   ./run.sh -var client_count=4  # any extra args pass through to apply/destroy
# -----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"

POLL_TIMEOUT="${POLL_TIMEOUT:-3000}" # 50 min default (sweep is ~25-35 min)
POLL_INTERVAL="${POLL_INTERVAL:-30}"
OUTDIR="${OUTDIR:-./bench-results}"
TF_ARGS=("$@")

log() { printf '[run.sh %(%H:%M:%S)T] %s\n' -1 "$*"; }

destroy() {
  if [[ "${KEEP:-0}" == "1" ]]; then
    log "KEEP=1 set — skipping destroy. REMEMBER to: terraform destroy -auto-approve"
    return
  fi
  log "Tearing down the fleet (terraform destroy)..."
  terraform destroy -auto-approve "${TF_ARGS[@]}" || \
    log "WARNING: destroy failed. Instances still self-terminate via shutdown-behavior + safety timers, but verify in the console."
}
trap destroy EXIT

log "terraform init"
terraform init -input=false >/dev/null

log "terraform apply (provisioning server + clients, starting sweep)"
terraform apply -auto-approve -input=false "${TF_ARGS[@]}"

SERVER_ID="$(terraform output -raw server_instance_id)"
REGION="$(terraform output -raw region)"
RUN_ID="$(terraform output -raw run_id)"

log "Polling serial console of $SERVER_ID for results (timeout ${POLL_TIMEOUT}s)"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
found=0
console=""
while [[ $(date +%s) -lt $deadline ]]; do
  # --latest gives the most recent console snapshot; it can lag a few minutes
  # after boot, which is why the orchestrator reprints the block several times.
  console="$(aws ec2 get-console-output --instance-id "$SERVER_ID" --region "$REGION" \
              --latest --output text --query Output 2>/dev/null || true)"
  if grep -q "===VEXBENCH-RESULTS-END===" <<<"$console"; then
    found=1
    break
  fi
  sleep "$POLL_INTERVAL"
done

if [[ "$found" == "1" ]]; then
  mkdir -p "$OUTDIR"
  dest="$OUTDIR/${RUN_ID}.csv"
  # Extract the LAST complete START..END block (most recent, most complete),
  # dropping the marker lines themselves.
  awk '/===VEXBENCH-RESULTS-START===/{cap=1;buf="";next}
       /===VEXBENCH-RESULTS-END===/{cap=0;last=buf;next}
       cap{buf=buf $0 "\n"}
       END{printf "%s", last}' <<<"$console" > "$dest"
  log "Results captured -> $dest"
  cat "$dest"
else
  log "TIMEOUT: results never appeared within ${POLL_TIMEOUT}s. Check the EC2 serial console / /var/log/server.log on the server box. Proceeding to teardown."
fi

# destroy() runs via the EXIT trap.
