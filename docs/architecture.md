# Architecture

## Hardware

A single Proxmox VE node with 64 GB+ RAM. Every VM uses the same Ubuntu 24.04 LTS cloud image.

| VM | Role | vCPU | RAM | Disk | IP (example) |
|---|---|---|---|---|---|
| k8s-cp-01..03 | Kubernetes control plane + etcd | 2 | 4 GB | 40 GB | .21–.23 |
| k8s-wk-01..02 | Kubernetes workers | 4 | 8 GB | 80 GB | .31–.32 |
| swarm-m-01..03 | Swarm managers (also run workloads) | 2 | 4 GB | 40 GB | .41–.43 |
| — | Kubernetes API VIP (kube-vip) | | | | .20 |

That adds up to about 40 GB of RAM. The rest stays free for the Proxmox host and for later additions such as Longhorn replicas or more workers. Each node is one entry in the `nodes` map in `terraform/envs/homelab/variables.tf`.

## Network

| Range | Purpose |
|---|---|
| `192.168.10.0/24` | Lab network (placeholder, set in `terraform.tfvars`) |
| `10.244.0.0/16` | Kubernetes pods (Cilium, IPAM mode `kubernetes`) |
| `10.96.0.0/12` | Kubernetes services |
| `10.20.0.0/16` | Swarm overlay networks (default address pool) |
| `.200–.220` | Reserved for MetalLB / Cilium LB IPAM (phase 2) |

UFW on each node denies incoming traffic by default. Traffic is accepted from the lab, pod and service ranges (`trusted_cidrs`). SSH accepts keys only and is rate-limited.

## Provisioning flow

1. **Terraform** downloads the Ubuntu cloud image to the Proxmox `import` storage once.
2. For each node, Terraform uploads a per-VM cloud-init snippet. The snippet sets the hostname, the admin user with an SSH key, and qemu-guest-agent. Terraform then creates the VM with a static IP.
3. Terraform renders `ansible/inventory/hosts.yml` from the same node map, so names and IPs are defined in one place.
4. **Ansible** `base.yml` applies the OS baseline and hardening to every node.
5. **Ansible** `kubernetes.yml`:
   - installs containerd 2.x with systemd cgroups, then kubeadm/kubelet/kubectl pinned to one minor version
   - runs `kubeadm init` on the first control-plane node. kube-vip runs as a static pod and holds the API VIP (ARP mode with leader election). kube-proxy is skipped.
   - joins the other control-plane nodes one at a time (`serial: 1`) using a fresh token and certificate key
   - joins the workers
   - installs Cilium as the kube-proxy replacement, with Hubble relay and UI
   - fetches the admin kubeconfig to `.artifacts/kubeconfig` (git-ignored), where devbox points `KUBECONFIG`
6. **Ansible** `swarm.yml` installs Docker CE, creates the swarm on the first manager and joins the rest.

## Why two orchestrators?

Many companies still run Docker Swarm or plain Docker hosts and are moving to Kubernetes. Running both lets me practise that move end to end: run a service on Swarm, then migrate it to Kubernetes with a written plan, data migration, cutover and rollback.
