variable "region" {
  description = "AWS region. The whole harness is validated for ap-south-1 / non-prod only."
  type        = string
  default     = "ap-south-1"
}

# ---------------------------------------------------------------------------
# Non-prod guardrail inputs. These defaults point at the scrum (non-prod) VPC
# and its NAT-backed private subnet. The PROD VPC id is hard-coded as a deny
# list below; a precondition in main.tf ABORTS the apply if the chosen subnet
# resolves into prod (or into any VPC other than var.nonprod_vpc_id).
# ---------------------------------------------------------------------------

variable "nonprod_vpc_id" {
  description = "The ONLY VPC this harness is allowed to run in (scrum / non-prod)."
  type        = string
  default     = "vpc-09aaab70570435386"
}

variable "prod_vpc_id" {
  description = "Prod VPC id. Used purely as a deny guard; provisioning here is forbidden."
  type        = string
  default     = "vpc-0ab66663"
}

variable "subnet_id" {
  description = "Private subnet (must have NAT egress) inside nonprod_vpc_id."
  type        = string
  default     = "subnet-04589b1a5c59454fa"
}

variable "ami_id" {
  description = <<-EOT
    AMI to launch (Amazon Linux 2023, x86_64). Defaults to the value resolved
    from the SSM public parameter when left empty; the literal pin below is the
    validated fallback for ap-south-1.
  EOT
  type        = string
  default     = "ami-0e38835daf6b8a2b9"
}

variable "use_ssm_ami" {
  description = "If true, resolve the AMI from the AL2023 SSM public parameter instead of var.ami_id."
  type        = bool
  default     = true
}

variable "server_instance_type" {
  description = "Server (orchestrator) instance type — big box, pinned 0..47 cores."
  type        = string
  default     = "c5a.16xlarge"
}

variable "client_instance_type" {
  description = "Load-generator instance type."
  type        = string
  default     = "c5a.8xlarge"
}

variable "client_count" {
  description = "Number of dedicated load-generator boxes."
  type        = number
  default     = 3

  validation {
    condition     = var.client_count >= 1 && var.client_count <= 10
    error_message = "client_count must be between 1 and 10."
  }
}

variable "results_bucket" {
  description = "S3 bucket the server uploads results.csv into."
  type        = string
  default     = "aws-athena-query-results-208168340597-ap-south-1"
}

variable "results_prefix" {
  description = "Key prefix inside results_bucket for this harness's output."
  type        = string
  default     = "vex-loadtest/"
}

variable "owner" {
  description = "owner tag value (who launched this ephemeral fleet)."
  type        = string
  default     = "vex-bench"
}

variable "key_name_prefix" {
  description = "Prefix for the throwaway EC2 key pair name."
  type        = string
  default     = "vex-loadtest"
}
