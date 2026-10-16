locals {
  prefix_length = split("/", var.network_cidr)[1]

  node_ips = {
    for name, n in var.nodes : name => cidrhost(var.network_cidr, n.ip_offset)
  }

  groups = distinct([for n in values(var.nodes) : n.group])

  k8s_api_vip = cidrhost(var.network_cidr, var.k8s_api_vip_offset)

  # Second Proxmox tag per group: the platform the VM belongs to.
  platform_tag = {
    for g in local.groups : g => (
      startswith(g, "k8s") ? "kubernetes" : startswith(g, "swarm") ? "swarm" : g
    )
  }
}

# One shared Ubuntu cloud image, imported into every VM's disk.
resource "proxmox_download_file" "ubuntu" {
  content_type = "import"
  datastore_id = var.file_datastore
  node_name    = var.proxmox_node
  url          = var.cloud_image_url
  file_name    = "${basename(trimsuffix(var.cloud_image_url, ".img"))}.qcow2"
  overwrite    = false
}

module "vm" {
  source   = "../../modules/proxmox-vm"
  for_each = var.nodes

  name      = each.key
  vm_id     = each.value.vm_id
  node_name = var.proxmox_node
  tags      = [replace(each.value.group, "_", "-"), local.platform_tag[each.value.group]]

  startup_order = each.value.startup_order

  cores     = each.value.cores
  memory_mb = each.value.memory_mb
  disk_gb   = each.value.disk_gb

  datastore_id          = var.vm_datastore
  snippets_datastore_id = var.file_datastore
  image_file_id         = proxmox_download_file.ubuntu.id

  bridge       = var.bridge
  vlan_id      = var.vlan_id
  ipv4_cidr    = "${local.node_ips[each.key]}/${local.prefix_length}"
  ipv4_gateway = var.gateway
  dns_servers  = var.dns_servers
  dns_domain   = var.dns_domain

  admin_user      = var.admin_user
  ssh_public_keys = var.ssh_public_keys
  timezone        = var.timezone
}

# Terraform hands over to Ansible through a generated inventory.
resource "local_file" "ansible_inventory" {
  filename        = "${path.root}/../../../ansible/inventory/hosts.yml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/hosts.yml.tftpl", {
    groups = {
      for g in local.groups : g => {
        for name, n in var.nodes : name => local.node_ips[name] if n.group == g
      }
    }
    admin_user   = var.admin_user
    k8s_api_vip  = local.k8s_api_vip
    network_cidr = var.network_cidr
    dns_domain   = var.dns_domain
  })
}
