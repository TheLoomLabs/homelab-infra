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
pveum role add TerraformProv -privs "Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate Datastore.Audit Sys.Audit Sys.Modify SDN.Use VM.Allocate VM.Audit VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.GuestAgent.Audit VM.PowerMgmt"
pveum user add terraform@pve
pveum aclmod / -user terraform@pve -role TerraformProv
# Privilege-separated token: it gets its own ACL and can never do more than the user.
pveum user token add terraform@pve provider --privsep 1
pveum aclmod / -token 'terraform@pve!provider' -role TerraformProv
```

Why these privileges (Proxmox VE 9, what the bpg provider does here):

| Privilege | Needed for |
|---|---|
| `Datastore.AllocateTemplate`, `Sys.Audit`, `Sys.Modify` | `proxmox_download_file`: the node downloads the cloud image (download-url) |
| `Datastore.Allocate`, `Datastore.AllocateSpace`, `Datastore.Audit` | VM disks (imported from the image), deleting snippets and images on destroy |
| `VM.Allocate`, `VM.Audit`, `VM.Config.*`, `VM.PowerMgmt` | Creating, configuring, starting and destroying the VMs; cloud-init drive (`VM.Config.CDROM`, `VM.Config.Cloudinit`) |
| `VM.GuestAgent.Audit` | Reading the VMs' IPs from the guest agent |
| `SDN.Use` | Attaching NICs to the `vmbr0` bridge |

Not granted, because nothing here uses them: `Pool.Allocate` (no resource pools), `Sys.Console` (no console access), `VM.Clone` (VMs import a cloud image, not clone a template), `VM.Migrate` (one node). The cloud-init snippets go over SSH (step 4), not the API.

Put the token into `.env`:

```bash
PROXMOX_VE_ENDPOINT=https://<proxmox-host>:8006/
PROXMOX_VE_API_TOKEN=terraform@pve!provider=<token-secret>
```

## 4. SSH to the Proxmox node

Uploading snippets goes over SSH as `root`, using **only** the key in your ssh-agent (the provider ignores `~/.ssh/config`):

```bash
ssh-copy-id root@<proxmox-host>
ssh-add -l   # make sure the key is loaded
# no agent? eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519
```

Check `ssh-add -l` before every apply that changes snippets. Without a working agent, Terraform deletes the old snippets and then fails to upload the new ones (see [dns.md](dns.md#gotchas)).

## 5. Settings

```bash
cp terraform/envs/homelab/terraform.tfvars.example terraform/envs/homelab/terraform.tfvars
```

Set `network_cidr`, `gateway`, `dns_servers`, `dns_domain` and `ssh_public_keys`. `dns_servers` are the lab DNS VMs (`dns-01`, `dns-02`).

## 6. First build: DNS VMs first

Every other VM resolves through `dns-01` and `dns-02` from its first boot, and cloud-init installs packages then. On a build from scratch, create and configure the DNS VMs first (they use the public resolvers themselves):

```bash
terraform -chdir=terraform/envs/homelab apply -target='module.vm["dns-01"]' -target='module.vm["dns-02"]' -target=local_file.ansible_inventory
cd ansible && ansible-playbook playbooks/base.yml --limit dns && cd ..
make dns
make tf-plan && make tf-apply     # the rest of the lab
make site
``` If your workstation is outside the lab network, add its subnet to `ssh_allowed_cidrs` in `ansible/inventory/group_vars/all.yml`.
