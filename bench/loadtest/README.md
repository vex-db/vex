# vex load-test harness

Compares vex / Redis / Dragonfly throughput, fairly (server pinned to N cores,
load generated from separate boxes, server-CPU sampled to prove saturation).

Two packagings:
- `terraform/` — big-node "autorun" on dedicated EC2 (the 4→48-core *scaling*
  story; Dragonfly's regime). `terraform apply` provisions a server + several
  client boxes, runs the sweep, ships results to S3, tears down.
- `helm/` — in-cluster regression benchmark for the 4–8 core regime (vex's
  target), runnable on every release. `helm install` → results in pod logs.
  Limited to the cluster's node size (scrum nodepools cap at 8 vCPU).

`scripts/` holds the validated benchmark logic (server orchestrator + client
setup + analysis) shared/adapted by both.

See [docs/benchmarks.md](../../docs/benchmarks.md) for methodology and results.
