# Bootstrap (one-time)

## 1. Devbox on the workstation

Devbox is the only tool installed on the workstation. Everything else is pulled from Nix into a project-local environment.

```bash
curl -fsSL https://get.jetify.com/devbox | bash
cd homelab-infra
devbox shell          # first run downloads the toolchain; commit devbox.lock afterwards
```

## 2. Proxmox storage content types

The `local` (directory) storage must allow **Import** (for the cloud image) and **Snippets** (for cloud-init). Enable them under *Datacenter → Storage → local → Edit → Content*, or run:

```bash
pvesm set local --content iso,vztmpl,backup,import,snippets
```

## 3. Least-privilege API token for Terraform

Run on the Proxmox node:

```bash
pveum role add TerraformProv -privs "Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate Datastore.Audit Pool.Allocate Sys.Audit Sys.Console Sys.Modify SDN.Use VM.Allocate VM.Audit VM.Clone VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Migrate VM.PowerMgmt"
# Proxmox VE 9: also add VM.GuestAgent.Audit so Terraform can read guest IPs.
pveum user add terraform@pve
pveum aclmod / -user terraform@pve -role TerraformProv
pveum user token add terraform@pve provider --privsep 0
```

Put the token into `.env`:

```bash
PROXMOX_VE_ENDPOINT=https://<proxmox-host>:8006/
PROXMOX_VE_API_TOKEN=terraform@pve!provider=<token-secret>
```

## 4. SSH to the Proxmox node

Uploading snippets goes over SSH, using the key in your ssh-agent:

```bash
ssh-copy-id root@<proxmox-host>
ssh-add -l   # make sure the key is loaded
```

## 5. Settings

```bash
cp terraform/envs/homelab/terraform.tfvars.example terraform/envs/homelab/terraform.tfvars
```

Set `network_cidr`, `gateway`, `dns_servers`, `dns_domain` and `ssh_public_keys`. If your workstation is outside the lab network, add its subnet to `ssh_allowed_cidrs` in `ansible/inventory/group_vars/all.yml`.
