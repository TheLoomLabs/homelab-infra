# Architecture

## Hardware

A single Proxmox VE node with 64 GB+ RAM. Every VM uses the same Ubuntu 24.04 LTS cloud image.

| VM | Role | vCPU | RAM | Disk | IP (example) |
|---|---|---|---|---|---|
| dns-01, dns-02 | Lab and house DNS (BIND primary, secondary) | 1 | 1 GB | 16 GB | .11–.12 |
| k8s-cp-01..03 | Kubernetes control plane + etcd | 2 | 4 GB | 40 GB | .21–.23 |
| k8s-wk-01..02 | Kubernetes workers, Longhorn storage | 4 | 8 GB | 80 GB + 100 GB data disk | .31–.32 |
| swarm-m-01..03 | Swarm managers (also run workloads) | 2 | 4 GB | 40 GB | .41–.43 |
| — | Kubernetes API VIP (kube-vip) | | | | .20 |

That adds up to about 42 GB of RAM. The rest stays free for the Proxmox host and for later additions such as more workers. Each node is one entry in the `nodes` map in `terraform/envs/homelab/variables.tf`.

## Network

| Range | Purpose |
|---|---|
| `192.168.10.0/24` | Lab network (placeholder, set in `terraform.tfvars`) |
| `10.244.0.0/16` | Kubernetes pods (Cilium, IPAM mode `kubernetes`) |
| `10.96.0.0/12` | Kubernetes services |
| `10.20.0.0/16` | Swarm overlay networks (default address pool) |
| `.60–.79` | `LoadBalancer` services (Cilium LB IPAM pool, announced with L2/ARP) |
| `.61` | Shared Gateway for `*.lab.theloomlabs.org` (fixed, from the LB pool) |

UFW on each node denies incoming traffic by default. Traffic is accepted from the lab, pod and service ranges (`trusted_cidrs`). SSH accepts keys only and is rate-limited.

The lab is IPv4 only. The Starlink router advertises a public IPv6 prefix, so the VMs ignore router advertisements (netplan `accept-ra: false`, role `common`) and have no global IPv6 address, only `fe80::` link-local. UFW denies incoming IPv6 too.

## Provisioning flow

1. **Terraform** downloads the Ubuntu cloud image to the Proxmox `import` storage once.
2. For each node, Terraform uploads a per-VM cloud-init snippet. The snippet sets the hostname, the admin user with an SSH key, and qemu-guest-agent. Terraform then creates the VM with a static IP. Cloud-init only matters on first boot: Terraform ignores later cloud-init changes on existing VMs, and Ansible maintains settings such as DNS resolvers from then on.
3. Terraform renders `ansible/inventory/hosts.yml` from the same node map, so names and IPs are defined in one place.
4. **Ansible** `base.yml` applies the OS baseline and hardening to every node, including the DNS resolvers.
5. **Ansible** `dns.yml` sets up BIND on `dns-01` and `dns-02` (see [DNS](#dns)).
6. **Ansible** `kubernetes.yml`:
   - installs containerd 2.x with systemd cgroups, then kubeadm/kubelet/kubectl pinned to one minor version
   - runs `kubeadm init` on the first control-plane node. kube-vip runs as a static pod and holds the API VIP (ARP mode with leader election). kube-proxy is skipped. Per-node kubeadm patches serve etcd metrics, kube-scheduler and kube-controller-manager on the node address, so Prometheus can scrape them ([monitoring.md](monitoring.md#control-plane-metrics)).
   - joins the other control-plane nodes one at a time (`serial: 1`) using a fresh token and certificate key
   - joins the workers
   - installs the Gateway API CRDs (pinned version and checksum), then Cilium as the kube-proxy replacement, with Hubble relay, Hubble UI, L2 announcements and the Gateway API controller. When the Helm values change, the role runs `cilium upgrade` (`playbooks/cilium.yml` re-applies only this part)
   - fetches the admin kubeconfig to `.artifacts/kubeconfig` (git-ignored), where devbox points `KUBECONFIG`
   - prepares the workers for Longhorn (`playbooks/longhorn.yml`, role `longhorn_node`): iSCSI and LUKS tools, the data disk at `/var/lib/longhorn`, the node label (see [Storage](#storage))
7. **Ansible** `swarm.yml` installs Docker CE, creates the swarm on the first manager and joins the rest.
8. **Ansible** `argocd.yml` bootstraps GitOps:
   - installs Helm on the first control-plane node and installs the `argo-cd` chart, using the version from `kubernetes/apps/argocd.yaml` and the values from `kubernetes/platform/argocd/values.yaml`
   - applies the `root` Application. From then on Argo CD syncs everything in `kubernetes/apps/`, including itself.
9. **Ansible** `sops.yml` loads the age private key into the cluster, so sops-secrets-operator can decrypt the secrets in Git. In `site.yml` it runs before `argocd.yml`.

## LoadBalancer services

Cilium gives `LoadBalancer` services an address on the LAN, so no MetalLB is needed (ADR 0005):

- **LB IPAM** assigns each service an IP from the `CiliumLoadBalancerIPPool` in `kubernetes/platform/cilium-lb/`.
- **L2 announcements** make one worker node answer ARP for that IP. The node holds a Kubernetes lease per service; if it goes down, another worker takes the lease and announces the IP.
- Control-plane nodes don't announce. The API VIP (`.20`) is separate and stays with kube-vip.
- The pool and the announcement policy are an Argo CD Application (`cilium-lb`). Cilium itself is still installed by Ansible.

The pool range must stay outside the router's DHCP range. `externalTrafficPolicy: Local` does not work with L2 announcements, so services keep the default `Cluster`.

## Ingress and TLS

HTTP(S) traffic enters the cluster through one shared Gateway, served by Cilium's Gateway API controller (Envoy) (ADR 0007):

- `gateway/lab` has a fixed address from the LB pool (`.61`). Port 80 only redirects to HTTPS. Port 443 terminates TLS for `*.lab.theloomlabs.org`.
- cert-manager keeps a Let's Encrypt **wildcard certificate** for `*.lab.theloomlabs.org` in the `gateway` namespace. It proves ownership with **DNS-01** records in Cloudflare, so the lab never needs to be reachable from the internet.
- An app is exposed with an `HTTPRoute` in its own namespace:

  ```yaml
  parentRefs:
    - name: lab
      namespace: gateway
      sectionName: https
  hostnames:
    - forgejo.lab.theloomlabs.org
  ```

- There are no public DNS records for lab hosts. Names resolve to the Gateway address only on the LAN, through the lab DNS servers, and later over Tailscale (split DNS).
- Argo CD itself is at `https://argocd.lab.theloomlabs.org`.

## DNS

Two BIND servers on their own VMs are the DNS for the lab and the whole house (ADR 0008). They don't run on Kubernetes or Swarm, so DNS keeps working while a cluster is down.

- `dns-01` is the primary, `dns-02` the secondary. The secondary pulls the zones over AXFR/IXFR, signed with a TSIG key from `dns/tsig.sops.yaml`.
- They are authoritative for `lab.theloomlabs.org` and its reverse zone, and resolve everything else (forwarders `1.1.1.1` and `9.9.9.9`, DNSSEC validation) for the lab network only.
- Every inventory host gets an A and a PTR record automatically. Extra names, such as `*` → Gateway (`.61`), `pve` and `k8s-api`, are in `dns/records.yml`. `make dns` deploys them, with a date-based serial that changes only when a zone does.
- The Starlink router has no conditional forwarding, so its Custom DNS points at both servers and every device at home resolves through them. The lab VMs and CoreDNS use them too. The DNS VMs themselves use the public resolvers.
- If both are down, turn Custom DNS off in the Starlink app.

Runbook: [dns.md](dns.md).

## Secrets

Secrets are stored in Git, encrypted with SOPS and age (ADR 0006):

- Each secret is a `SopsSecret` manifest (`*.sops.yaml`) next to the component that uses it. Only `spec.secretTemplates` is encrypted.
- **sops-secrets-operator** (Argo CD Application `sops-secrets-operator`, namespace `sops`) decrypts them and creates normal `Secret`s. Argo CD never sees plaintext.
- Ansible-side secrets (the BIND TSIG key) are SOPS files next to their data (`dns/tsig.sops.yaml`), decrypted on the workstation by `community.sops.load_vars` when the playbook runs.
- The age private key is the one secret not in Git. It lives in `.artifacts/age.key` on the workstation, with an offline backup, and Ansible (`playbooks/sops.yml`) loads it into the cluster as `sops/sops-age-key-file`.
- Pre-commit, `make sops-check` and CI reject a `*.sops.yaml` file that is not encrypted.
- Inside the cluster, the API server encrypts Secrets and ConfigMaps before they reach etcd (secretbox, key in `k8s/encryption.sops.yaml`). Details: [encryption.md](encryption.md).
- Pod Security Admission sets what pods may do per namespace: `restricted` for everything except kube-system, longhorn-system and monitoring (node agents), `baseline` for namespaces without labels. The labels live in `kubernetes/platform/namespaces/`. Details: [pod-security.md](pod-security.md).
- Cilium network policies make every namespace except kube-system and longhorn-system default deny, in both directions. Each component's `networkpolicy.yaml` allows what it needs; the internet only by name (`toFQDNs`). Details: [network-policy.md](network-policy.md).
- The API servers write an audit log (policy in role `k8s_control_plane`); Alloy ships it to Loki, whose ruler alerts through Alertmanager on exec, secret reads by people, anonymous or repeatedly forbidden requests and Pod Security violations. Details: [audit-logging.md](audit-logging.md).

How to add, edit and rotate secrets: [secrets.md](secrets.md).

## Storage

Longhorn provides replicated block volumes on the two workers (ADR 0009). It is an Argo CD Application; Ansible only prepares the nodes.

- Each worker has a dedicated 100 GB data disk (`data_disk_gb` in the Terraform node map), mounted at `/var/lib/longhorn`. Control-plane nodes hold no storage.
- Every volume has two replicas, one per worker.
- The default StorageClass `longhorn-encrypted` encrypts volumes with LUKS, using one key from a SopsSecret (`longhorn-crypto`). Reclaim policy `Retain`. The plain `longhorn` class is for disposable data such as metrics.
- Encrypted volumes get hourly snapshots and daily and weekly backups to the Cloudflare R2 bucket `theloomlabs-longhorn` (RecurringJobs, group `encrypted`). The backups are ciphertext; R2 never sees plaintext.
- The Longhorn UI has no authentication and is not exposed; use `kubectl port-forward`.

Runbook: [storage.md](storage.md).

## Backups

Velero backs up the cluster every night at 03:00 UTC, after Longhorn's own daily backup at 02:00 (ADR 0010).

- Kubernetes objects go to the R2 bucket `theloomlabs-velero`, encrypted with SSE-C (a key only we hold). Each bucket has its own R2 token, scoped to that bucket. The R2 account ID only appears inside SOPS-encrypted Secrets.
- Volumes are not copied by Velero. Velero creates CSI VolumeSnapshots of class `longhorn-backup`, and each one becomes a Longhorn backup in `theloomlabs-longhorn`. The external-snapshotter CRDs and controller are their own Argo CD Application (`snapshot-controller`).
- The schedule keeps 30 days and skips `longhorn-system` (Longhorn restores its own volumes) and Cilium's runtime objects.
- One Velero restore brings back a namespace with its PVCs and data. This was tested by deleting and restoring a namespace with an encrypted volume.

| Lost | Comes back from |
|---|---|
| A pod, a node, one replica | Longhorn's second replica |
| An app's data or namespace | Velero restore (objects + volumes), or a single Longhorn backup |
| The whole cluster or the Proxmox host | Git (Terraform, Ansible, Argo CD), then Velero and Longhorn restores from R2 |

What must survive outside the lab: the age key, the Longhorn key and the SSE-C key. Runbook: [backup.md](backup.md).

## Monitoring and alerting

kube-prometheus-stack runs in the namespace `monitoring` (ADR 0011). The receivers of its alerts run on Docker Swarm, so they keep working when Kubernetes doesn't.

- **Prometheus** scrapes nodes, workloads and every ServiceMonitor in the cluster. Components ship their own ServiceMonitors and PrometheusRules next to their manifests: Longhorn (failed backups, unreachable backup target, degraded or faulted volumes, disk usage) and Velero (failed or missing nightly backup, unreachable R2). Metrics are kept 15 days on the plain `longhorn` class; Velero skips the namespace.
- **Grafana** is at `https://grafana.lab.theloomlabs.org`. Prometheus and Alertmanager have no authentication and are reached with `kubectl port-forward` until SSO exists.
- **Alertmanager** posts warnings and critical alerts to **ntfy** (`ntfy.lab.theloomlabs.org`); the phone subscribes with the ntfy app. The always-firing `Watchdog` alert pings **healthchecks** (`hc.lab.theloomlabs.org`) every minute; when the pings stop, healthchecks alerts through ntfy. That is the dead man's switch for the cluster.
- ntfy, healthchecks and **Traefik** (TLS, wildcard certificate via Cloudflare DNS-01) are the Swarm stack `alerting`, deployed by Ansible (role `alerting`). Their state is in local volumes on one Swarm node.
- Not covered: the whole Proxmox host, power or the Starlink link going down. Nothing on the host can report that; the stack moves to a second machine later.

Runbooks: [monitoring.md](monitoring.md), [alerting.md](alerting.md).

## Logs

Loki stores the logs of every node, VM and pod for 15 days; Grafana queries it next to the metrics (ADR 0012).

- **Loki** runs as one process (single binary) in `monitoring`, with its index and chunks on a 20 GiB volume of the plain `longhorn` class. No object storage: logs are disposable, like metrics, and Velero skips the namespace.
- **Grafana Alloy** ships the logs. In Kubernetes, a DaemonSet reads the pod log files and each node's journal, and a single replica ships Kubernetes events (the API server keeps them for an hour only). On the DNS and Swarm VMs, Alloy runs as a systemd service (role `alloy`) and ships the journal and the Swarm containers' logs.
- The VMs push through the shared Gateway (`loki.lab.theloomlabs.org`) to Loki's nginx gateway, which requires basic auth. Inside the cluster, Alloy and Grafana talk to Loki directly.
- When Kubernetes is down, the VMs can't push; every VM's journal still has the logs, and Alloy catches up from its saved position.
- Loki and Alloy alert through Prometheus like everything else (Loki down, rejected lines, compaction not running, Alloy dropping lines). Alerts on log content (Loki's ruler) come later.

Runbook: [logging.md](logging.md).

## Why two orchestrators?

Many companies still run Docker Swarm or plain Docker hosts and are moving to Kubernetes. Running both lets me practise that move end to end: run a service on Swarm, then migrate it to Kubernetes with a written plan, data migration, cutover and rollback.
