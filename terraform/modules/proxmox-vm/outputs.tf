output "name" {
  description = "VM name."
  value       = proxmox_virtual_environment_vm.this.name
}

output "vm_id" {
  description = "Proxmox VM ID."
  value       = proxmox_virtual_environment_vm.this.vm_id
}

output "ipv4_address" {
  description = "Configured static IPv4 address (without prefix)."
  value       = split("/", var.ipv4_cidr)[0]
}
