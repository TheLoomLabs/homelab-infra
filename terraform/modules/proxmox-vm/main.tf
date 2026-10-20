# Per-VM cloud-init user-data. Kept minimal on purpose: identity, access and
# the guest agent only. Everything else is Ansible's job.
resource "proxmox_virtual_environment_file" "user_data" {
  content_type = "snippets"
  datastore_id = var.snippets_datastore_id
  node_name    = var.node_name

  source_raw {
    file_name = "${var.name}-user-data.yaml"
    data = templatefile("${path.module}/templates/user-data.yaml.tftpl", {
      hostname        = var.name
      fqdn            = "${var.name}.${var.dns_domain}"
      admin_user      = var.admin_user
      ssh_public_keys = var.ssh_public_keys
      timezone        = var.timezone
    })
  }
}

resource "proxmox_virtual_environment_vm" "this" {
  name        = var.name
  description = "Managed by Terraform (homelab-infra)"
  tags        = sort(distinct(concat(["terraform"], var.tags)))
  node_name   = var.node_name
  vm_id       = var.vm_id
  started     = var.started
  on_boot     = true

  machine       = "q35"
  bios          = "seabios"
  scsi_hardware = "virtio-scsi-single"

  # The guest agent is installed by cloud-init; Terraform waits for it to
  # report the IP address.
  agent {
    enabled = true
    timeout = "10m"
  }

  stop_on_destroy = true

  # Never reboot a running VM from Terraform: a change to every worker would
  # restart them all at once, without a drain. Changes that need a restart
  # (memory, cores) stay pending in Proxmox until the VM is restarted from
  # the host, one node at a time (docs/maintenance.md, "Resizing a VM").
  reboot_after_update = false

  dynamic "startup" {
    for_each = var.startup_order == null ? [] : [var.startup_order]
    content {
      order = startup.value
    }
  }

  cpu {
    cores = var.cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.memory_mb
  }

  disk {
    datastore_id = var.datastore_id
    import_from  = var.image_file_id
    interface    = "scsi0"
    size         = var.disk_gb
    discard      = "on"
    iothread     = true
    ssd          = true
  }

  # Optional empty data disk (e.g. Longhorn on Kubernetes workers). Ansible
  # finds it by its serial (/dev/disk/by-id/*_<serial>), formats and mounts it.
  # It is left out of Proxmox backups: the application backs up its own data.
  dynamic "disk" {
    for_each = var.data_disk_gb == null ? [] : [var.data_disk_gb]
    content {
      datastore_id = var.datastore_id
      interface    = "scsi1"
      size         = disk.value
      file_format  = "raw"
      serial       = var.data_disk_serial
      discard      = "on"
      iothread     = true
      ssd          = true
      backup       = false
    }
  }

  network_device {
    bridge  = var.bridge
    vlan_id = var.vlan_id
  }

  operating_system {
    type = "l26"
  }

  serial_device {}

  initialization {
    datastore_id = var.datastore_id

    dns {
      domain  = var.dns_domain
      servers = var.dns_servers
    }

    ip_config {
      ipv4 {
        address = var.ipv4_cidr
        gateway = var.ipv4_gateway
      }
    }

    user_data_file_id = proxmox_virtual_environment_file.user_data.id
  }

  lifecycle {
    # A newer cloud image must not recreate running cluster nodes.
    # Cloud-init only sets a VM up on first boot: changing it later would
    # recreate the VM (new user-data file) or reboot it with a new cloud-init
    # instance ID. Day-2 changes such as DNS resolvers go through Ansible
    # (role common), so existing VMs ignore it and new VMs get the new values.
    ignore_changes = [disk[0].import_from, initialization]
  }
}
