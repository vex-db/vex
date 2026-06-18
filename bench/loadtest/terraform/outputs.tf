output "region" {
  description = "AWS region the fleet runs in."
  value       = var.region
}

output "run_id" {
  description = "Unique id for this benchmark run."
  value       = local.run_id
}

output "server_instance_id" {
  description = "Server (orchestrator) instance id — run.sh polls its serial console for results."
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
