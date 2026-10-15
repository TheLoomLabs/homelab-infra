# Credentials are never stored in the repo. Export them in your devbox shell:
#   export PROXMOX_VE_ENDPOINT="https://pve.example.lan:8006/"
#   export PROXMOX_VE_API_TOKEN="terraform@pve!provider=<secret>"
# See docs/bootstrap.md for creating the API token with least privilege.
provider "proxmox" {
  insecure = var.proxmox_insecure_tls

  # Uploading cloud-init snippets requires SSH to the Proxmox node.
  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}
