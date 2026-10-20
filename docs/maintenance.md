# Maintenance

## Rolling reboots

`make reboot` (`ansible/playbooks/reboot.yml`) reboots the VMs one at a time and keeps every cluster up:

| Order | Group | Before the reboot | After the reboot |
|---|---|---|---|
| 1 | `dns` | | `named` active |
| 2 | `k8s_control_plane` | all control-plane nodes Ready (etcd quorum) | node Ready; etcd, API server, controller manager, scheduler pods Ready |
| 3 | `k8s_workers` | no degraded or faulted Longhorn volume; `kubectl drain` | node Ready, uncordon, Longhorn volumes healthy again |
| 4 | `swarm_managers` | every manager Ready and Leader/Reachable (Raft quorum) | node ready and reachable |

kubectl runs on another control-plane node through the API VIP. The play stops at the first check that fails, so a node that does not come back leaves the rest of the lab alone.

When to run it:

- **Kernel and libc updates.** unattended-upgrades installs security updates but never reboots. A pending reboot shows up as `/var/run/reboot-required`:

```
cd ansible && ansible all -b -m ansible.builtin.shell \
  -a 'test -f /var/run/reboot-required && cat /var/run/reboot-required.pkgs || true'
```

- **Network changes in netplan** (role `common`). Ansible writes the netplan file but never runs `netplan apply`, because systemd-networkd would drop the kube-vip VIP on the control plane. The change takes effect at the next boot.

Commands:

```
make reboot                                       # every VM
make reboot ARGS="-e reboot_required_only=true"   # only VMs that need it
make reboot ARGS="--limit k8s_workers"            # one group
```

Notes:

- A full run takes about 20–30 minutes. Most of it is the workers: Longhorn rebuilds the replicas of a returning worker before the next one is drained.
- Alerts can fire while a node is down (targets down, etcd member down, Longhorn volume degraded). They resolve on their own. For a quiet run, add a silence in Alertmanager first.
- Rebooting `swarm-m-01` restarts the alerting stack (ntfy, healthchecks, Traefik are pinned there), so notifications pause for a minute or two.
- The Proxmox host itself is not part of this play.

## Resizing a VM

Memory and CPU live in `nodes` (`terraform/envs/homelab/variables.tf`, or an override in `terraform.tfvars`). The VM module sets `reboot_after_update = false`, so Terraform never restarts a running VM: Proxmox keeps the new values as pending changes until the VM is restarted **from the host**. A reboot inside the guest (`make reboot`, `ansible.builtin.reboot`) keeps the same QEMU process and does not apply them; `qm reboot` on `pve` does.

1. Plan and apply. The plan must show only in-place updates (`~`) of the VMs you resized, nothing replaced:

```
make tf-plan
make tf-apply
```

2. On `pve`, check what is pending (lines starting with `pending`):

```
ssh root@pve.lab.theloomlabs.org qm pending 231
```

3. Restart one node at a time. For a Kubernetes worker, Longhorn must be healthy before the drain and again before the next worker (every volume has one replica per worker). Run from the devbox; replace `k8s-wk-01` / `231` for the second worker:

```
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=N:.metadata.name,ROBUST:.status.robustness
kubectl drain k8s-wk-01 --ignore-daemonsets \
  --delete-emptydir-data --timeout 900s
ssh root@pve.lab.theloomlabs.org qm reboot 231
kubectl wait node k8s-wk-01 --for condition=Ready --timeout 300s
kubectl uncordon k8s-wk-01
```

Wait until every volume is `healthy` again (first command) before the next worker. For the other groups follow the same order and checks as `make reboot` (table above): one control-plane node at a time with all three Ready, one Swarm manager at a time with all managers Reachable.

4. Check the new size inside the guest:

```
cd ansible && ansible k8s_workers -m ansible.builtin.command -a 'free -g'
```

If an apply fails with an error that the VM must be offline, the change can't be pending on a running VM. Stop that one VM after the drain (`ssh root@pve.lab.theloomlabs.org qm shutdown 231`), apply, and the provider starts it again.

## IPv6

The lab is IPv4 only. The Starlink router advertises a public IPv6 prefix, so with router advertisements on, every VM got a globally routable address (SLAAC) next to its LAN address. The VMs ignore RAs now (`common_ipv6_accept_ra: false`, netplan `accept-ra: false`). Only the `fe80::` link-local address is left.

This has to be the netplan setting. systemd-networkd always turns off the kernel's RA handling and runs SLAAC itself, so the `accept_ra` and `autoconf` sysctls change nothing on these interfaces.

Check after the reboots. Every VM prints `accept-ra: false` and no `inet6` line:

```
cd ansible && ansible all -b -m ansible.builtin.shell \
  -a 'grep accept-ra /etc/netplan/50-cloud-init.yaml; ip -6 addr show dev eth0 scope global'
```
