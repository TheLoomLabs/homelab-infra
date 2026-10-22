# Logs: Loki and Grafana Alloy

Loki stores the logs of the whole lab for 15 days; Grafana queries them (ADR 0012). Grafana Alloy ships them:

- **Kubernetes:** an Alloy DaemonSet reads every pod's log files and every node's journal (kubelet, containerd, kernel, sshd). A second Alloy with one replica ships Kubernetes events, which the API server keeps for an hour only.
- **DNS, Swarm and runner VMs:** Alloy as a systemd service (role `alloy`) ships the journal and, on the Swarm nodes, every container's log. It pushes through the Gateway to Loki's nginx gateway, which requires basic auth.

Loki runs as one process (single binary) in `monitoring`, with its index and chunks on a 20 GiB volume of the plain `longhorn` class: no object storage, no encryption, no backups (Velero skips the namespace). Logs are disposable, like metrics.

| What | Where |
|---|---|
| Loki install and settings | `kubernetes/apps/loki.yaml`, `kubernetes/platform/loki/values.yaml` (chart `loki` from grafana-community) |
| Push user for the VMs (htpasswd) | `kubernetes/platform/loki/gateway-auth.sops.yaml` → Secret `monitoring/loki-gateway-auth` |
| Push endpoint for the VMs | `kubernetes/platform/loki/httproute.yaml` → `https://loki.lab.theloomlabs.org` |
| Loki alerts | `kubernetes/platform/loki/prometheusrule.yaml` |
| Alloy in the cluster | `kubernetes/apps/alloy.yaml`, `kubernetes/platform/alloy/values.yaml` (DaemonSet), `values-events.yaml` (events), `prometheusrule.yaml` |
| Alloy on the VMs | `ansible/roles/alloy`, `ansible/playbooks/logs.yml`, `make logs` |
| Push password for the VMs | `logging/alloy.sops.yaml` (SOPS, loaded by the playbook) |
| Grafana datasource | `grafana.additionalDataSources` in `kubernetes/platform/monitoring/values.yaml` |
| Log-based alerts | Loki's ruler → Alertmanager; rules in ConfigMaps `loki-rules-*` (`kubernetes/platform/loki/audit-rules.yaml`, [audit-logging.md](audit-logging.md#alerts)) |

## Labels

Every log line has `source` and `host`; the rest depends on the source. Keep queries starting from these labels.

| `source` | From | Other labels |
|---|---|---|
| `pods` | `/var/log/pods` on every Kubernetes node | `namespace`, `pod`, `container`, `app` |
| `journal` | systemd journal of every node and VM | `unit` (`kernel` for kernel messages), `level` (`emerg` … `debug`) |
| `events` | Kubernetes events | `namespace` of the object; kind, name, reason and message are in the line (logfmt) |
| `docker` | Containers on the Swarm nodes | `stack`, `service` |
| `audit` | Kubernetes API audit log on the control-plane nodes | none; one JSON event per line, parse with `\| json` ([audit-logging.md](audit-logging.md)) |

`host` is the Kubernetes node or the VM name from the inventory (`k8s-wk-01`, `dns-01`, `swarm-m-02` …). The events have no `host`.

## Add the secrets

One random password, stored twice: as an htpasswd entry for Loki's nginx gateway, and as plain text for the Alloys on the VMs. `openssl passwd -apr1` makes a hash nginx understands.

```bash
LOKI_PW=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-32)

mkdir -p logging
f=logging/alloy.sops.yaml
sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
alloy_loki_password: "${LOKI_PW}"
YAML

f=kubernetes/platform/loki/gateway-auth.sops.yaml
HTPASSWD="alloy:$(printf '%s' "$LOKI_PW" | openssl passwd -apr1 -stdin)"
sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: loki-gateway-auth
  namespace: monitoring
spec:
  secretTemplates:
    - name: loki-gateway-auth
      stringData:
        .htpasswd: "${HTPASSWD}"
YAML
unset LOKI_PW HTPASSWD

make sops-check
```

The user name `alloy` is `alloy_loki_username` in `ansible/roles/alloy/defaults/main.yml`. To rotate the password, run the block again, push, wait for Argo CD, then `make logs`.

## Deploy

The Kubernetes side comes with Argo CD (apps `loki` and `alloy`, sync wave -1) after a push to `main`. The VMs follow with:

```bash
make logs
```

`make site` runs the same playbook last. The DNS VMs don't resolve through the lab DNS (they use the public resolvers), so the role puts `loki.lab.theloomlabs.org` in their `/etc/hosts`, pointing at the wildcard record's address from `dns/records.yml` (the Gateway). The VMs install Alloy from the GitHub release package (checked against the release's `SHA256SUMS`), not from an apt repository; `alloy_version` in the role defaults follows the Helm chart's Alloy version.

## Check

```bash
kubectl -n argocd get applications loki alloy
kubectl -n monitoring get pods,pvc -l 'app.kubernetes.io/name in (loki,alloy)'
kubectl -n monitoring get httproute loki
```

Expected: `loki-0` and `loki-gateway-…` running, one `alloy-…` pod per node (5), one `alloy-events-…` pod, PVC `storage-loki-0` bound (20Gi).

Loki itself:

```bash
kubectl -n monitoring port-forward svc/loki 3100 &
curl -s localhost:3100/ready; echo                              # ready
curl -s localhost:3100/loki/api/v1/label/source/values; echo    # pods, journal, events, docker
curl -s localhost:3100/loki/api/v1/label/host/values; echo      # every node and VM
kill %1
```

The push endpoint, from the workstation; `401` means it is reachable and asks for the password:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://loki.lab.theloomlabs.org/loki/api/v1/push
```

On a VM:

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org 'systemctl is-active alloy; sudo journalctl -u alloy -n 20 --no-pager'
curl -s http://swarm-m-01.lab.theloomlabs.org:12345/-/ready; echo   # Alloy listens on the VM address (LAN only)
```

In Prometheus (*Status → Targets*): `monitoring/loki`, `alloy`, `alloy-events` and the VMs' `alloy-vm` are up; *Alerts* shows the groups `loki`, `alloy` and `vms` (AlloyVMDroppingLogs, AlloyVMComponentUnhealthy).

## Query in Grafana

https://grafana.lab.theloomlabs.org → **Explore** (compass icon in the left menu) → datasource **Loki** at the top → switch the query editor from **Builder** to **Code** (top right of the query row) → type a query → **Run query** (or Shift+Enter). **Logs Drilldown** in the left menu browses the same data without writing queries.

```logql
# One app in Kubernetes, errors only
{source="pods", namespace="monitoring", app="grafana"} |= "error"

# Why did a pod restart? Events for one namespace
{source="events", namespace="longhorn-system"} | logfmt | reason=~"BackOff|OOMKilling|Killing"

# SSH logins on every node and VM
{source="journal", unit="ssh.service"} |= "Accepted"

# BIND on the DNS servers, warnings and worse
{source="journal", unit="named.service", level=~"warning|err|crit"}

# The alerting stack on Swarm
{source="docker", stack="alerting"}

# Kernel messages (OOM killer, disk errors) from the last day
{source="journal", unit="kernel"} |~ "(?i)oom|i/o error"
```

## Troubleshooting

- **No logs from a VM:** `sudo journalctl -u alloy` on the VM. `401` means the password in `logging/alloy.sops.yaml` doesn't match the hash in `gateway-auth.sops.yaml` (re-run [Add the secrets](#add-the-secrets)); `no such host` on a DNS VM means the `/etc/hosts` entry is missing (`grep loki /etc/hosts`, then `make logs`); elsewhere, a DNS or TLS error means the Gateway or `loki.lab.theloomlabs.org` isn't reachable. While Loki is unreachable, Alloy keeps its journal position and catches up later, as far back as the journal reaches (`common_journald_max_use`, 500 MB).
- **No logs from a Kubernetes node:** `kubectl -n monitoring logs ds/alloy` shows one pod's log; pick the node's pod with `kubectl -n monitoring get pods -l app.kubernetes.io/instance=alloy -o wide`. The Alloy UI shows every component and its health: `kubectl -n monitoring port-forward <pod> 12345` → http://localhost:12345.
- **`LokiDiscardingLogs`:** the reason label says why. `rate_limited` or `per_stream_rate_limit`: raise `ingestion_rate_mb` / `ingestion_burst_size_mb` in the Loki values. `greater_than_max_sample_age`: lines older than 7 days, e.g. from a VM whose clock was wrong.
- **Loki volume filling up:** retention deletes chunks older than 15 days, two hours after the compactor marks them. If the volume still fills, shorten `retention_period` or grow the PVC (Longhorn supports online expansion: edit `singleBinary.persistence.size`, then `kubectl -n monitoring patch pvc storage-loki-0` to the same size, since StatefulSets don't resize their volumes).
- **Grafana shows no Loki datasource:** the datasource sidecar reloads it from the kube-prometheus-stack ConfigMap; `kubectl -n monitoring rollout restart deploy/kube-prometheus-stack-grafana` forces it.

## Later

- Alloy on the Proxmox host itself.
- Loki's mixin dashboards (the chart's `monitoring.dashboards`), once they suit a single-binary install.
