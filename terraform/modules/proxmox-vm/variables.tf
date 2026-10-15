variable "name" {
  description = "VM name, also used as the guest hostname."
  type        = string
}

variable "vm_id" {
  description = "Proxmox VM ID."
  type        = number
}

variable "node_name" {
  description = "Proxmox node to place the VM on."
  type        = string
}

variable "tags" {
  description = "Proxmox tags (role, cluster, ...)."
  type        = list(string)
  default     = []
}

variable "cores" {
  description = "Number of vCPU cores."
  type        = number
  default     = 2
}

variable "memory_mb" {
  description = "Dedicated memory in MiB."
  type        = number
  default     = 4096
}

variable "disk_gb" {
  description = "Root disk size in GiB."
  type        = number
  default     = 32
}

variable "datastore_id" {
  description = "Datastore for the VM disk and cloud-init drive."
  type        = string
  default     = "local-lvm"
}

variable "snippets_datastore_id" {
  description = "File-based datastore with the 'Snippets' content type enabled."
  type        = string
  default     = "local"
}

variable "image_file_id" {
  description = "ID of the cloud image (content type 'import') to boot from."
  type        = string
}

variable "bridge" {
  description = "Linux bridge for the primary NIC."
  type        = string
  default     = "vmbr0"
}

variable "vlan_id" {
  description = "Optional VLAN tag for the primary NIC."
  type        = number
  default     = null
}

variable "ipv4_cidr" {
  description = "Static IPv4 address with prefix, e.g. 192.168.10.21/24."
  type        = string

  validation {
    condition     = can(cidrhost(var.ipv4_cidr, 0))
    error_message = "ipv4_cidr must be in CIDR notation, e.g. 192.168.10.21/24."
  }
}

variable "ipv4_gateway" {
  description = "Default IPv4 gateway."
  type        = string
}

variable "dns_servers" {
  description = "DNS resolvers for the guest."
  type        = list(string)
}

variable "dns_domain" {
  description = "Search domain for the guest."
  type        = string
}

variable "admin_user" {
  description = "Admin user created by cloud-init (SSH key only)."
  type        = string
}

variable "ssh_public_keys" {
  description = "SSH public keys authorised for the admin user."
  type        = list(string)
}

variable "timezone" {
  description = "Guest timezone."
  type        = string
  default     = "Etc/UTC"
}

variable "started" {
  description = "Whether the VM should be running."
  type        = bool
  default     = true
}
