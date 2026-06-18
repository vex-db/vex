# =============================================================================
# vex/Redis/Dragonfly throughput benchmark — ephemeral EC2 fleet (ap-south-1).
#
# Provisions 1 server (orchestrator) + N client (load-gen) boxes, runs the
# validated sweep, ships results.csv to S3, and self-terminates. Pair with
# run.sh for the full apply -> poll S3 -> download -> destroy autorun cycle.
#
# NON-PROD ONLY. A precondition (see aws_instance.server) aborts the apply if
# the chosen subnet does not live in var.nonprod_vpc_id, and explicitly if it
# lands in var.prod_vpc_id. Do not remove the guardrail.
# =============================================================================

locals {
  scripts_dir = "${path.module}/../scripts"

  run_id = "run-${formatdate("YYYYMMDD-hhmmss", timestamp())}-${random_id.run.hex}"

  ami_id = var.use_ssm_ami ? nonsensitive(data.aws_ssm_parameter.al2023.value) : var.ami_id

  common_tags = {
    purpose = "ephemeral-benchmark"
    owner   = var.owner
  }
}

resource "random_id" "run" {
  byte_length = 3
}

# --- AMI (AL2023 x86_64) resolved from the SSM public parameter -------------
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# --- Guardrail data: resolve the chosen subnet's VPC ------------------------
data "aws_subnet" "target" {
  id = var.subnet_id
}

# --- Throwaway SSH key (server -> clients) ----------------------------------
resource "tls_private_key" "bench" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "bench" {
  key_name_prefix = "${var.key_name_prefix}-"
  public_key      = tls_private_key.bench.public_key_openssh
  tags            = merge(local.common_tags, { Name = "${var.key_name_prefix}-key" })
}

# --- Security group: self-referencing ingress + all egress ------------------
# The scrum "default" SG had no self rule, which broke box-to-box SSH/bench
# traffic. This dedicated SG allows everything from itself, and all egress for
# image pulls through the subnet's NAT.
resource "aws_security_group" "bench" {
  name_prefix = "${var.key_name_prefix}-"
  description = "Ephemeral vex benchmark fleet: self-ingress + all egress."
  vpc_id      = data.aws_subnet.target.vpc_id

  tags = merge(local.common_tags, { Name = "${var.key_name_prefix}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "self_all" {
  security_group_id            = aws_security_group.bench.id
  description                  = "All traffic from this SG to itself (SSH + benchmark)."
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.bench.id
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.bench.id
  description       = "All egress (image pulls via NAT)."
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# NOTE: no IAM here. This account's principal is denied IAM (CreateRole returns
# InvalidClientTokenId), and instance profiles are unavailable. Results are
# exfiltrated via the EC2 serial console (get-console-output) instead of S3 —
# the same mechanism the validated 2-box run used. The orchestrator script
# already prints the CSV to /dev/console between markers; run.sh polls for it.

# --- Client user-data: substitute the SSH public key placeholder ------------
locals {
  client_user_data = replace(
    file("${local.scripts_dir}/client-userdata.sh"),
    "PUBKEY_PLACEHOLDER",
    trimspace(tls_private_key.bench.public_key_openssh),
  )
}

# --- Client boxes (launched first; their private IPs feed the server) -------
resource "aws_instance" "client" {
  count = var.client_count

  ami                                  = local.ami_id
  instance_type                        = var.client_instance_type
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [aws_security_group.bench.id]
  key_name                             = aws_key_pair.bench.key_name
  user_data                            = local.client_user_data
  instance_initiated_shutdown_behavior = "terminate"

  tags = merge(local.common_tags, { Name = "${var.key_name_prefix}-client-${count.index}" })
}

# --- Server user-data: the committed orchestrator with the client IPs and the
# throwaway private key substituted in. The script already runs the sweep and
# prints results.csv to /dev/console between ===VEXBENCH-RESULTS-{START,END}===
# markers, then self-terminates. run.sh harvests that via get-console-output.
#
# DECISION: bench/loadtest/scripts/server-userdata.sh stays the single source
# of benchmark logic (no duplication / drift). Terraform only injects the IPs
# and key; it does NOT rewrite the output step (no S3 — this account denies IAM,
# so there is no instance profile to authorize an upload).
locals {
  server_user_data = replace(
    replace(
      file("${local.scripts_dir}/server-userdata.sh"),
      "CLIENT_IPS_PLACEHOLDER",
      join(" ", aws_instance.client[*].private_ip),
    ),
    "PRIVKEY_B64_PLACEHOLDER",
    base64encode(tls_private_key.bench.private_key_openssh),
  )
}

# --- Server box (launched after clients so private IPs are known) -----------
resource "aws_instance" "server" {
  ami                                  = local.ami_id
  instance_type                        = var.server_instance_type
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [aws_security_group.bench.id]
  key_name                             = aws_key_pair.bench.key_name
  user_data                            = local.server_user_data
  instance_initiated_shutdown_behavior = "terminate"

  depends_on = [aws_instance.client]

  tags = merge(local.common_tags, { Name = "${var.key_name_prefix}-server" })

  # ---------------------------------------------------------------------------
  # NON-PROD GUARDRAIL. Abort if the subnet's VPC is prod, or is anything other
  # than the configured non-prod VPC. The prod VPC was a near-miss before.
  # ---------------------------------------------------------------------------
  lifecycle {
    precondition {
      condition     = data.aws_subnet.target.vpc_id != var.prod_vpc_id
      error_message = "REFUSING: subnet ${var.subnet_id} is in the PROD VPC (${var.prod_vpc_id}). This harness is NON-PROD only."
    }
    precondition {
      condition     = data.aws_subnet.target.vpc_id == var.nonprod_vpc_id
      error_message = "REFUSING: subnet ${var.subnet_id} resolves to VPC ${data.aws_subnet.target.vpc_id}, not the allowed non-prod VPC ${var.nonprod_vpc_id}."
    }
    # The client IPs must have been injected, or the orchestrator has no load
    # generators to drive (it would idle, emit nothing, and self-terminate).
    precondition {
      condition     = !strcontains(local.server_user_data, "CLIENT_IPS_PLACEHOLDER")
      error_message = "Client IPs were not injected into the server user-data; the CLIENT_IPS_PLACEHOLDER anchor in scripts/server-userdata.sh changed."
    }
  }
}
