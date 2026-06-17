output "region" {
  description = "AWS region the fleet runs in."
  value       = var.region
}

output "run_id" {
  description = "Unique id for this benchmark run (also the S3 key segment)."
  value       = local.run_id
}

output "results_s3_uri" {
  description = "Full s3:// URI of the results.csv the server uploads at the end of the sweep."
  value       = local.result_s3_uri
}

output "results_bucket" {
  description = "Bucket the results land in."
  value       = var.results_bucket
}

output "results_key" {
  description = "S3 object key of the results.csv (use with `aws s3api wait object-exists`)."
  value       = local.result_key
}

output "server_instance_id" {
  description = "Server (orchestrator) instance id."
  value       = aws_instance.server.id
}

output "server_private_ip" {
  description = "Server private IP (for EC2 serial-console / SSM debugging)."
  value       = aws_instance.server.private_ip
}

output "client_instance_ids" {
  description = "Client (load-generator) instance ids."
  value       = aws_instance.client[*].id
}

output "client_private_ips" {
  description = "Client private IPs fed to the server orchestrator."
  value       = aws_instance.client[*].private_ip
}
