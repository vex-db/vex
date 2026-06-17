#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Autorun the vex/Redis/Dragonfly throughput benchmark on a throwaway EC2 fleet:
#
#   terraform init && apply  ->  poll S3 for results.csv  ->  download  ->  destroy
#
# Teardown is robust: we ALWAYS `terraform destroy` on exit (trap), even if the
# results object never appears within the timeout. As a backstop the instances
# also self-terminate (instance_initiated_shutdown_behavior=terminate + the
# orchestrator script's 70-min safety timer + the clients' 60-min timer), so a
# crashed laptop / lost creds can never leave the fleet running indefinitely.
#
# Usage:
#   ./run.sh                      # full cycle, results -> ./bench-results/
#   POLL_TIMEOUT=2700 ./run.sh    # override max wait for results (seconds)
#   KEEP=1 ./run.sh               # skip the final destroy (debug; you must clean up!)
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

BUCKET="$(terraform output -raw results_bucket)"
KEY="$(terraform output -raw results_key)"
S3_URI="$(terraform output -raw results_s3_uri)"
REGION="$(terraform output -raw region)"

log "Waiting for results at $S3_URI (timeout ${POLL_TIMEOUT}s)"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
found=0
while [[ $(date +%s) -lt $deadline ]]; do
  if aws s3api head-object --bucket "$BUCKET" --key "$KEY" --region "$REGION" >/dev/null 2>&1; then
    found=1
    break
  fi
  sleep "$POLL_INTERVAL"
done

if [[ "$found" == "1" ]]; then
  mkdir -p "$OUTDIR"
  dest="$OUTDIR/$(terraform output -raw run_id).csv"
  log "Results ready. Downloading -> $dest"
  aws s3 cp "$S3_URI" "$dest" --region "$REGION"
  log "Done. Results:"
  cat "$dest"
else
  log "TIMEOUT: results never appeared within ${POLL_TIMEOUT}s. Check the EC2 serial console / /var/log/server.log on the server box. Proceeding to teardown."
fi

# destroy() runs via the EXIT trap.
