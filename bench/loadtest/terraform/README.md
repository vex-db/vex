# vex load-test — Terraform autorun (ephemeral EC2, ap-south-1)

Provisions a throwaway EC2 fleet, runs the validated vex/Redis/Dragonfly
throughput sweep (4 → 48 cores, SET/GET, pipeline 1/30), ships the results CSV
to S3, and tears everything down. **Non-prod only.**

## Trigger command

```bash
cd bench/loadtest/terraform
./run.sh
```

That single command does the whole cycle:

```
terraform init && terraform apply -auto-approve
  -> poll S3 for the results object (timeout, default 50 min)
  -> download it to ./bench-results/<run_id>.csv
  -> terraform destroy -auto-approve   (always, via an EXIT trap)
```

Useful knobs (env vars / pass-through args):

```bash
POLL_TIMEOUT=2700 ./run.sh          # max wait for results (seconds)
KEEP=1 ./run.sh                     # debug: skip destroy (you must clean up!)
./run.sh -var client_count=4        # extra args flow to apply AND destroy
```

Analyze the downloaded CSV with `../scripts/analyze.py`:

```bash
python3 ../scripts/analyze.py ./bench-results/<run_id>.csv
```

## What gets created

| Resource | Count | Type | Notes |
|---|---|---|---|
| server (orchestrator) | 1 | `c5a.16xlarge` | runs the sweep, has the IAM profile to `PutObject` to S3 |
| client (load-gen) | `client_count` (3) | `c5a.8xlarge` | memtier boxes driven over SSH by the server |
| security group | 1 | — | self-referencing ingress + all egress |
| EC2 key pair | 1 | ed25519 (`tls_private_key`) | throwaway; server → clients |
| IAM role + instance profile | 1 | — | server-only `s3:PutObject` to the results prefix |

All instances use `instance_initiated_shutdown_behavior = "terminate"` and are
tagged `Name`, `purpose=ephemeral-benchmark`, `owner`.

**Launch order:** clients first — their private IPs are substituted into the
server's `CLIENT_IPS_PLACEHOLDER` — then the server (`depends_on` the clients).
The server discovers its own IP via IMDS inside the script.

## Non-prod guardrail (do not remove)

The prod VPC was a near-miss before, so the apply is hard-gated. The server
instance carries two `lifecycle.precondition`s on the resolved subnet's VPC:

1. **Deny prod:** aborts if the subnet's VPC `== var.prod_vpc_id`
   (`vpc-0ab66663`).
2. **Allow-list non-prod:** aborts unless the subnet's VPC
   `== var.nonprod_vpc_id` (the scrum VPC `vpc-09aaab70570435386`).

The VPC is *resolved from the actual subnet* via `data.aws_subnet`, so pointing
`-var subnet_id=...` at a prod subnet still fails — you can't sneak past by only
changing the subnet. Defaults: subnet `subnet-04589b1a5c59454fa` (NAT-backed
private subnet in the scrum VPC).

A third precondition asserts the S3-upload swap actually fired (guards against
silent drift in the committed orchestrator script — see below).

## How results reach S3 (the Terraform-specific bit)

The committed orchestrator `../scripts/server-userdata.sh` emits its CSV to the
EC2 serial console. **Decision: we keep that script as the single source of
benchmark logic (no fork / no drift) and only rewrite its final output step in
Terraform.** In `main.tf`:

1. `file()` reads the committed script; `replace()` substitutes
   `CLIENT_IPS_PLACEHOLDER` (client private IPs) and `PRIVKEY_B64_PLACEHOLDER`
   (`base64encode(tls_private_key.bench.private_key_openssh)`).
2. A second `replace()` swaps the script's trailing *console-emit loop* for an
   `aws s3 cp /tmp/results.csv s3://<bucket>/vex-loadtest/<run_id>/results.csv`
   (with retries). The client-teardown + `shutdown -h now` lines that follow are
   left untouched, so the fleet still self-terminates as before.
3. AL2023 ships awscli v2 preinstalled; the server's IAM instance profile grants
   `s3:PutObject` scoped to `vex-loadtest/*` in `var.results_bucket`.

The S3 URI is exposed as the `results_s3_uri` output and is what `run.sh` polls.

## Self-termination backstops (besides `run.sh` destroy)

`run.sh` always destroys on exit, but if your machine dies mid-run the fleet
still goes away:

- `instance_initiated_shutdown_behavior = "terminate"` — any `shutdown` ends the
  instance.
- Server script: 70-min safety timer (`sleep 4200; shutdown -h now`) + normal
  end-of-sweep shutdown, which also SSHes each client a `shutdown -h now`.
- Client script: 60-min safety timer.

The only thing `run.sh`'s destroy cleans up that the timers don't is the SG /
key pair / IAM role (instances self-delete, these Terraform-managed bits do not),
so do run a `terraform destroy` eventually even after `KEEP=1`.

## Inputs (see `variables.tf`)

Key variables: `region` (ap-south-1), `nonprod_vpc_id`, `prod_vpc_id`,
`subnet_id`, `ami_id` / `use_ssm_ami` (resolves AL2023 x86_64 from the SSM
public parameter by default), `server_instance_type`, `client_instance_type`,
`client_count` (3), `results_bucket`, `results_prefix` (`vex-loadtest/`),
`owner`.

## Cost / safety notes

This launches large on-demand instances (`c5a.16xlarge` + 3×`c5a.8xlarge`).
A full sweep is ~25–35 min. **Never** run against prod creds or a prod VPC.
`terraform apply`/`plan` are intentionally not run by CI for this module.
