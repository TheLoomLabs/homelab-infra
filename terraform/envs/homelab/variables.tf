# --- Proxmox -----------------------------------------------------------------

variable "proxmox_node" {
  description = "Proxmox node name."
  type        = string
  default     = "pve"
}

variable "proxmox_insecure_tls" {
  description = "Skip TLS verification (self-signed Proxmox certificate)."
  type        = bool
  default     = true
}

variable "proxmox_ssh_user" {
  description = "SSH user on the Proxmox node, used to upload snippets."
  type        = string
  default     = "root"
}

variable "vm_datastore" {
  description = "Datastore for VM disks."
  type        = string
  default     = "local-lvm"
}

variable "file_datastore" {
  description = "File datastore with 'Import' and 'Snippets' content types enabled."
  type        = string
  default     = "local"
}

variable "cloud_image_url" {
  description = "Ubuntu cloud image used for every node."
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}

# --- Network -----------------------------------------------------------------

variable "bridge" {
  description = "Linux bridge for the VMs."
  type        = string
  default     = "vmbr0"
}

variable "vlan_id" {
  description = "Optional VLAN tag for the lab network."
  type        = number
  default     = null
}

variable "network_cidr" {
  description = "Lab network. Node IPs are derived from it with host offsets."
  type        = string
  default     = "192.168.10.0/24"
}

variable "gateway" {
  description = "Default gateway."
  type        = string
  default     = "192.168.10.1"
}

variable "dns_servers" {
  description = "DNS resolvers for the lab VMs (the lab DNS servers dns-01/dns-02)."
  type        = list(string)
  default     = ["192.168.10.11", "192.168.10.12"]
}

variable "upstream_dns_servers" {
  description = "Public resolvers. The DNS VMs use them for themselves, so a rebuilt DNS VM never depends on the lab DNS it provides."
  type        = list(string)
  default     = ["1.1.1.1", "9.9.9.9"]
}

variable "dns_domain" {
  description = "Lab DNS domain."
  type        = string
  default     = "lab.example.internal"
}

# --- Access ------------------------------------------------------------------

variable "admin_user" {
  description = "Admin user created on every VM."
  type        = string
  default     = "ops"
}

variable "ssh_public_keys" {
  description = "SSH public keys for the admin user."
  type        = list(string)
}

variable "timezone" {
  description = "Guest timezone."
  type        = string
  default     = "Europe/Zagreb"
}

# --- Clusters ----------------------------------------------------------------

variable "k8s_api_vip_offset" {
  description = "Host offset in network_cidr for the Kubernetes API virtual IP (kube-vip)."
  type        = number
  default     = 20
}

variable "nodes" {
  description = <<-EOT
    Every VM in the lab. 'group' maps to the Ansible inventory group
    (dns, k8s_control_plane, k8s_workers, swarm_managers, swarm_workers);
    'ip_offset' is the host number inside network_cidr. 'startup_order' sets
    the Proxmox boot order (lower starts first; unset = no order).
    'data_disk_gb' adds an empty second disk for Longhorn (Kubernetes workers
    only; unset = none).
  EOT
  type = map(object({
    vm_id         = number
    group         = string
    ip_offset     = number
    cores         = number
    memory_mb     = number
    disk_gb       = number
    startup_order = optional(number)
    data_disk_gb  = optional(number)
  }))

  default = {
    # DNS boots first: every other VM and the whole house resolve through it.
    dns-01     = { vm_id = 211, group = "dns", ip_offset = 11, cores = 1, memory_mb = 1024, disk_gb = 16, startup_order = 1 }
    dns-02     = { vm_id = 212, group = "dns", ip_offset = 12, cores = 1, memory_mb = 1024, disk_gb = 16, startup_order = 1 }
    k8s-cp-01  = { vm_id = 221, group = "k8s_control_plane", ip_offset = 21, cores = 2, memory_mb = 4096, disk_gb = 40 }
    k8s-cp-02  = { vm_id = 222, group = "k8s_control_plane", ip_offset = 22, cores = 2, memory_mb = 4096, disk_gb = 40 }
    k8s-cp-03  = { vm_id = 223, group = "k8s_control_plane", ip_offset = 23, cores = 2, memory_mb = 4096, disk_gb = 40 }
    k8s-wk-01  = { vm_id = 231, group = "k8s_workers", ip_offset = 31, cores = 4, memory_mb = 12288, disk_gb = 80, data_disk_gb = 100 }
    k8s-wk-02  = { vm_id = 232, group = "k8s_workers", ip_offset = 32, cores = 4, memory_mb = 12288, disk_gb = 80, data_disk_gb = 100 }
    swarm-m-01 = { vm_id = 241, group = "swarm_managers", ip_offset = 41, cores = 2, memory_mb = 4096, disk_gb = 40 }
    swarm-m-02 = { vm_id = 242, group = "swarm_managers", ip_offset = 42, cores = 2, memory_mb = 4096, disk_gb = 40 }
    swarm-m-03 = { vm_id = 243, group = "swarm_managers", ip_offset = 43, cores = 2, memory_mb = 4096, disk_gb = 40 }
  }

  validation {
    condition = alltrue([
      for n in values(var.nodes) :
      contains(["dns", "k8s_control_plane", "k8s_workers", "swarm_managers", "swarm_workers"], n.group)
    ])
    error_message = "Each node group must be one of: dns, k8s_control_plane, k8s_workers, swarm_managers, swarm_workers."
  }

  validation {
    condition     = length(distinct([for n in values(var.nodes) : n.ip_offset])) == length(var.nodes)
    error_message = "ip_offset values must be unique."
  }

  validation {
    # Longhorn runs on the workers only; control-plane nodes stay storage-free.
    condition     = alltrue([for n in values(var.nodes) : n.data_disk_gb == null || n.group == "k8s_workers"])
    error_message = "data_disk_gb is only supported on k8s_workers nodes (Longhorn)."
  }
}
