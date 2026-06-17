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

  results_prefix = trimsuffix(var.results_prefix, "/")
  result_key     = "${local.results_prefix}/${local.run_id}/results.csv"
  result_s3_uri  = "s3://${var.results_bucket}/${local.result_key}"

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

# --- IAM: server instance may PutObject under the results prefix ------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "s3_put" {
  statement {
    sid       = "PutResults"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${var.results_bucket}/${local.results_prefix}/*"]
  }
}

resource "aws_iam_role" "server" {
  name_prefix        = "${var.key_name_prefix}-srv-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = merge(local.common_tags, { Name = "${var.key_name_prefix}-server-role" })
}

resource "aws_iam_role_policy" "server_s3" {
  name_prefix = "s3-put-"
  role        = aws_iam_role.server.id
  policy      = data.aws_iam_policy_document.s3_put.json
}

resource "aws_iam_instance_profile" "server" {
  name_prefix = "${var.key_name_prefix}-srv-"
  role        = aws_iam_role.server.name
  tags        = merge(local.common_tags, { Name = "${var.key_name_prefix}-server-profile" })
}

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

# --- Server user-data: start from the committed orchestrator, substitute the
# client IPs + private key, then SWAP the trailing "emit CSV to console + tail
# shutdown" block for an S3 upload of /tmp/results.csv followed by shutdown.
#
# DECISION: we keep bench/loadtest/scripts/server-userdata.sh as the single
# source of benchmark logic (no duplication / drift). The Terraform layer only
# rewrites the final output step. The console block is a stable, unique anchor.
locals {
  server_base = replace(
    replace(
      file("${local.scripts_dir}/server-userdata.sh"),
      "CLIENT_IPS_PLACEHOLDER",
      join(" ", aws_instance.client[*].private_ip),
    ),
    "PRIVKEY_B64_PLACEHOLDER",
    base64encode(tls_private_key.bench.private_key_openssh),
  )

  # We anchor ONLY on the console-emit loop (a stable, unique 7-line block in
  # the committed script) and replace it with: a one-shot console echo of the
  # CSV (handy for `terraform console`-less debugging via EC2 serial console)
  # plus an S3 upload with retries. The client-teardown + self-shutdown lines
  # that follow in the committed script are left untouched, so the fleet still
  # tears itself down exactly as before.
  console_loop = "for rep in 1 2 3 4 5; do\n  echo \"===VEXBENCH-RESULTS-START===\" > /dev/console\n  cat \"$R\" > /dev/console\n  echo \"===VEXBENCH-RESULTS-END===\" > /dev/console\n  sleep 25\ndone"

  s3_emit = "echo \"===VEXBENCH-RESULTS-START===\" > /dev/console\ncat \"$R\" > /dev/console\necho \"===VEXBENCH-RESULTS-END===\" > /dev/console\n# Ship results to S3 (instance profile grants PutObject); retry as NAT/creds settle.\nfor attempt in 1 2 3 4 5; do\n  if aws s3 cp \"$R\" \"${local.result_s3_uri}\" --region ${var.region} > /dev/console 2>&1; then\n    echo \"===VEXBENCH-S3-OK ${local.result_s3_uri}===\" > /dev/console\n    break\n  fi\n  echo \"===VEXBENCH-S3-RETRY $attempt===\" > /dev/console\n  sleep 15\ndone"

  # AL2023 ships awscli v2 (`aws`) preinstalled; no extra install needed.
  # The replace() is asserted to actually fire by a precondition on the server.
  server_user_data = replace(local.server_base, local.console_loop, local.s3_emit)
}

# --- Server box (launched after clients so private IPs are known) -----------
resource "aws_instance" "server" {
  ami                                  = local.ami_id
  instance_type                        = var.server_instance_type
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [aws_security_group.bench.id]
  key_name                             = aws_key_pair.bench.key_name
  iam_instance_profile                 = aws_iam_instance_profile.server.name
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
    # Fail loudly if the committed script's console-emit loop drifted and the
    # S3 swap silently no-op'd (results would never reach S3 otherwise).
    precondition {
      condition     = strcontains(local.server_user_data, "aws s3 cp")
      error_message = "S3 upload was not injected into server user-data: the console-emit anchor in scripts/server-userdata.sh changed. Update local.console_loop in main.tf."
    }
  }
}
