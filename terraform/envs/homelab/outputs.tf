output "nodes" {
  description = "Node name to IPv4 address."
  value       = local.node_ips
}

output "k8s_api_endpoint" {
  description = "Kubernetes API endpoint (kube-vip virtual IP)."
  value       = "https://${local.k8s_api_vip}:6443"
}

output "ansible_inventory" {
  description = "Path of the generated Ansible inventory."
  value       = abspath(local_file.ansible_inventory.filename)
}
