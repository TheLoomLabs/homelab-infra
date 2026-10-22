# Monitoring and alerting

kube-prometheus-stack runs Prometheus, Alertmanager and Grafana in the namespace `monitoring` (ADR 0011). Alertmanager sends alerts to the self-hosted ntfy on Docker Swarm, and pings the self-hosted healthchecks as a dead man's switch. Those two live on Swarm so they keep working when Kubernetes doesn't: [alerting.md](alerting.md).

| What | Where |
|---|---|
| Prometheus Operator CRDs | `kubernetes/apps/prometheus-operator-crds.yaml` (sync wave -9) |
| Stack install and settings | `kubernetes/apps/kube-prometheus-stack.yaml`, `kubernetes/platform/monitoring/values.yaml` |
| Grafana admin login | `kubernetes/platform/monitoring/grafana-admin.sops.yaml` → Secret `monitoring/grafana-admin` |
| ntfy token, healthchecks ping URL | `kubernetes/platform/monitoring/notify.sops.yaml` → Secret `monitoring/alertmanager-notify` |
| Grafana on the Gateway | `kubernetes/platform/monitoring/grafana-httproute.yaml` |
| Longhorn metrics and alerts | `metrics.serviceMonitor` in `kubernetes/platform/longhorn/values.yaml`, `kubernetes/platform/longhorn/prometheusrule.yaml` |
| Velero metrics and alerts | `metrics.serviceMonitor` in `kubernetes/platform/velero/values.yaml`, `kubernetes/platform/velero/prometheusrule.yaml` |
| Control-plane metrics (etcd, scheduler, controller manager) | kubeadm patches in `ansible/roles/k8s_control_plane/templates/patches/`, `kubeEtcd`/`kubeScheduler`/`kubeControllerManager` in `values.yaml` |
| DNS, Swarm and runner VMs: targets, alerts, dashboard | `kubernetes/platform/monitoring/scrapeconfig-vms.yaml`, `vms-prometheusrule.yaml`, `swarm-dashboard.yaml`; on the VMs roles `alloy`, `docker`, `alerting` ([below](#vms-outside-kubernetes-dns-and-swarm)) |
| Logs (Loki, Alloy) and their alerts | [logging.md](logging.md) |

## Alert routing

| Alert | Goes to |
|---|---|
| `severity: critical` | ntfy topic `alerts`, priority 5 (overrides Do Not Disturb) |
| `severity: warning` | ntfy topic `alerts`, priority 3 |
| `severity: info` | nowhere (visible in Grafana and Alertmanager) |
| `Watchdog` (always firing) | healthchecks, every minute |

Alerts are grouped per namespace and alert name, repeated every 12 h while firing, and followed by a "resolved" message.

## Add the secrets

The Swarm stack must be set up first ([alerting.md](alerting.md), steps 1–5): the ntfy token comes from `swarm/alerting.sops.yaml`, the ping URL from the healthchecks check.

```bash
f=kubernetes/platform/monitoring/notify.sops.yaml
NTFY_TOKEN=$(sops decrypt --extract '["alerting_ntfy_token_alertmanager"]' swarm/alerting.sops.yaml)
read -rsp 'healthchecks ping URL: ' HC_URL; echo
sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: alertmanager-notify
  namespace: monitoring
spec:
  secretTemplates:
    - name: alertmanager-notify
      stringData:
        ntfy-token: "${NTFY_TOKEN}"
        healthchecks-url: "${HC_URL}"
YAML
unset NTFY_TOKEN HC_URL

f=kubernetes/platform/monitoring/grafana-admin.sops.yaml
read -rp 'Grafana admin username: ' GRAFANA_USER
sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: grafana-admin
  namespace: monitoring
spec:
  secretTemplates:
    - name: grafana-admin
      stringData:
        admin-user: "${GRAFANA_USER}"
        admin-password: "$(openssl rand -base64 24)"
YAML
unset GRAFANA_USER

make sops-check
```

Read the Grafana login later with `sops decrypt kubernetes/platform/monitoring/grafana-admin.sops.yaml | grep admin-`, and keep it in the password manager.

## Check

```bash
kubectl -n argocd get applications prometheus-operator-crds kube-prometheus-stack
kubectl -n monitoring get pods,pvc
kubectl get servicemonitors,prometheusrules -A
```

Prometheus targets and rules (no UI on the Gateway):

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090 &
# http://localhost:9090/targets  → every target UP (longhorn-system, velero, kube-system, monitoring)
# http://localhost:9090/rules    → groups longhorn-backup, longhorn-volumes, velero
kill %1
```

Grafana: https://grafana.lab.theloomlabs.org, with the user from `grafana-admin.sops.yaml`.

## Test the alert path

1. **ntfy:** send a test alert straight to Alertmanager; it reaches the phone within about 30 seconds, and a "resolved" message after the end time.

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093 &
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d "[{
\"labels\": {\"alertname\": \"TestAlert\", \"severity\": \"warning\", \"namespace\": \"monitoring\"},
\"annotations\": {\"summary\": \"Test from docs/monitoring.md\"},
\"endsAt\": \"$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%SZ)\"}]"
kill %1
```

2. **Dead man's switch:** the check `k8s-alertmanager-watchdog` on https://hc.lab.theloomlabs.org shows a ping every minute. To test it, silence `Watchdog` for 10 minutes (scaling Alertmanager down doesn't work: Argo CD self-heals it). Within about 6 minutes (period + grace) the phone gets a "down" message from healthchecks; when the silence ends, an "up" message follows.

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093 &
curl -s -XPOST localhost:9093/api/v2/silences -H 'Content-Type: application/json' -d "{
\"matchers\": [{\"name\": \"alertname\", \"value\": \"Watchdog\", \"isRegex\": false}],
\"startsAt\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"endsAt\": \"$(date -u -d '+10 minutes' +%Y-%m-%dT%H:%M:%SZ)\",
\"createdBy\": \"docs\", \"comment\": \"dead man's switch test\"}"
kill %1
```

3. **Backup alerts:** `VeleroNoRecentBackup` and `LonghornBackupFailed` can be checked in the Prometheus UI under *Alerts*; they should be green after the nightly run.

## Control-plane metrics

kubeadm binds etcd's metrics listener, kube-scheduler and kube-controller-manager to 127.0.0.1, where Prometheus can't reach them. Per-node kubeadm patches (JSON patches in `/etc/kubernetes/patches/` on each control-plane node, from role `k8s_control_plane`) move them to the node's own address:

| Component | Listens on | Scraped as |
|---|---|---|
| etcd | `http://127.0.0.1:2381` and `http://<node>:2381` (metrics and health only, no data) | job `kube-etcd`, plain http |
| kube-controller-manager | `https://<node>:10257` | job `kube-controller-manager`, https with the Prometheus service account token |
| kube-scheduler | `https://<node>:10259` | job `kube-scheduler`, https with the same token |

- The patches add the flag again at the end of the command (the last one wins) and move the scheduler and controller manager probes to the node address. etcd keeps 127.0.0.1 first in the list, so its probes don't change.
- kubeadm applies the patches on `init` and `join`. On running nodes, the tasks in `tasks/metrics.yml` make the same change in the static pod manifests, one node at a time, and wait for each component on the node address.
- The firewall needs nothing new: `trusted_cidrs` on the Kubernetes nodes already include the lab network and the pod network.
- etcd over https (client port 2379) would need an etcd client certificate in the `monitoring` namespace, which is full access to etcd. The metrics-only port avoids that.

**kubeadm upgrades must pass the patches**, or the manifests go back to 127.0.0.1 and `KubeSchedulerDown`, `KubeControllerManagerDown` and `etcdMembersDown` fire:

```bash
sudo kubeadm upgrade apply v1.X.Y --patches /etc/kubernetes/patches   # first control-plane node
sudo kubeadm upgrade node --patches /etc/kubernetes/patches            # the other control-plane nodes
```

### Roll out to a running cluster

Run Ansible **before** pushing the values change: once Argo CD enables the monitors, components still on 127.0.0.1 show as down and alert.

1. Patch the three control-plane nodes (serial, one etcd member at a time):

```bash
cd ansible && ansible-playbook playbooks/kubernetes.yml \
  --limit k8s_control_plane --tags cp_metrics && cd ..
```

2. Check the listeners on the three nodes:

```bash
cd ansible && ansible k8s_control_plane -b -m ansible.builtin.shell \
  -a "ss -ltnp | grep -E ':(2381|10257|10259) '" && cd ..
```

Expected: 2381 on 127.0.0.1 and the node address, 10257 and 10259 on the node address only.

3. Check that the control plane is healthy:

```bash
kubectl get nodes
kubectl -n kube-system get pods -l tier=control-plane -o wide
```

All pods `Running` and `1/1`, restart count up by one at most.

4. Push the commits. Argo CD syncs `kube-prometheus-stack` (refresh the app in the UI to skip the wait).

5. Prometheus targets:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 &
# http://localhost:19090/targets → kube-etcd, kube-controller-manager, kube-scheduler: 3/3 UP each
# http://localhost:19090/rules   → groups etcd, kubernetes-system-controller-manager,
#                                  kubernetes-system-scheduler, kube-scheduler.rules
kill $!
```

6. Grafana: dashboards *etcd*, *Kubernetes / Controller Manager* and *Kubernetes / Scheduler* show data.

### Troubleshooting control-plane metrics

- **Target down with `connection refused`:** the component still listens on 127.0.0.1. Check the flags with `sudo grep -E 'bind-address|listen-metrics' /etc/kubernetes/manifests/*.yaml` and run step 1 again.
- **Target down with `401`/`403`:** the token Secret `monitoring/kube-prometheus-stack-prometheus-token` is missing or the Prometheus ClusterRole lost `nonResourceURLs: /metrics`.
- **Scheduler or controller manager restarting:** the probe host and the bind address differ. `kubectl -n kube-system describe pod kube-scheduler-<node>` shows the failing probe.
- **etcd latency alerts** (`etcdHighFsyncDurations`, `etcdHighCommitDurations`, `etcdGRPCRequestsSlow`): the three members share one Proxmox disk with Longhorn. Check the *etcd* dashboard against Longhorn activity (backups, rebuilds) before tuning a rule.

## VMs outside Kubernetes (DNS and Swarm)

Prometheus on Kubernetes also scrapes the VMs outside the cluster (dns-01, dns-02, swarm-m-01..03, and the Forgejo runner runner-01). Nothing new runs on them except one systemd timer: Alloy, which already ships their logs, serves the metrics too.

| Job | Hosts | Source | Port, path |
|---|---|---|---|
| `node-exporter` | all | Alloy's built-in node_exporter (`prometheus.exporter.unix`), plus textfiles | 12345, `/api/v0/component/prometheus.exporter.unix.node/metrics` |
| `alloy-vm` | all | Alloy itself | 12345, `/metrics` |
| `docker-cadvisor` | Swarm | Alloy's built-in cAdvisor (`prometheus.exporter.cadvisor`), Docker containers only | 12345, `/api/v0/component/prometheus.exporter.cadvisor.containers/metrics` |
| `docker-engine` | Swarm | dockerd (`metrics-addr` in daemon.json, role `docker`): engine, and on managers the Swarm (`swarm_manager_*`, Raft) | 9323, `/metrics` |
| `traefik` | Swarm managers | Traefik of the edge stack (global service), own entrypoint, host-mode port | 8082, `/metrics` |

- **Targets are static** (`scrapeconfig-vms.yaml`, ScrapeConfig CRD): the VMs' LAN addresses, with the labels `host` (VM name, also used as `instance`) and `group` (`dns`, `swarm`, `ci`). A new or re-addressed VM means changing that file and the `prometheus` network policy (egress `toCIDR` per address and port).
- **Host metrics use job `node-exporter`**, like the cluster nodes. kube-prometheus-stack's node alerts (disk filling up, memory, clock, network errors), its recording rules and the *Node Exporter / Nodes* and *USE Method* dashboards cover the VMs without new rules. Pick the VM as instance there.
- **Replicas per Swarm service:** the Swarm has no exporter for desired versus running tasks. A systemd timer on every manager (`swarm-services-metrics.timer`, every 30 s, as user `alloy`) runs `docker service ls` and writes `swarm_service_replicas_desired` and `swarm_service_replicas_running` (labels `stack`, `service`, `mode`) into `/var/lib/alloy/textfile/`, which the node exporter serves. All three managers write the same numbers; the rules take the max.
- **Network:** UFW on these VMs already trusts the whole lab network, and pod traffic leaves the cluster with the node's address, so the firewall needs nothing new. Alloy's HTTP server (metrics and a read-only UI) now listens on the VM address instead of 127.0.0.1, without authentication: reachable from the LAN only. Traefik's port is published by Docker, which bypasses UFW; also LAN-only.
- **Dashboard:** *Docker Swarm* (`swarm-dashboard.yaml`): managers, leader, nodes and tasks by state, services with desired and running tasks, Raft latency, CPU/memory/disk/network per VM, CPU/memory/network per Swarm service, Traefik requests, latency and certificate days left.

Alerts (`vms-prometheusrule.yaml`):

| Alert | Severity | Fires when |
|---|---|---|
| VMHostDown | warning | Alloy on a VM can't be scraped for 3 minutes. Inhibits the chart's TargetDown for the VM jobs |
| AlloyVMDroppingLogs, AlloyVMComponentUnhealthy | warning | Same as the cluster's Alloy alerts, per VM |
| SwarmQuorumLost | critical | Fewer than two managers' Docker engines reachable |
| SwarmNoLeader | critical | No manager reports itself as Raft leader |
| SwarmNodeNotReady | warning | A Swarm node is `down`, `unknown` or `disconnected` for 5 minutes |
| SwarmServiceReplicasMismatch | warning | A service (not a job) runs fewer tasks than desired for 5 minutes |
| SwarmServiceMetricsStale | warning | A manager's replica file is older than 5 minutes, or no manager writes one |
| DockerContainerOOMKilled | warning | A container hit its memory limit |
| SwarmVipDown, SwarmVipSplitBrain | critical | No manager holds the Swarm VIP for 2 minutes, or more than one does for 1 minute (keepalived textfile metrics, [edge.md](edge.md)) |
| SwarmVipNodeFault | warning | A manager's Traefik fails keepalived's check for 5 minutes: the VIP can't move there |
| EdgeCertRenewalFailing | warning | edge-cert hasn't succeeded anywhere for two days, or never reported |
| TraefikCertificateExpiringSoon | warning | The wildcard certificate has under 14 days left (edge-cert renews with a third of the lifetime left, 30 of 90 days) |
| TraefikCertificateMismatch | warning | The Traefik instances serve different certificates for 2 hours (a renewal's rolling update stopped) |

### Roll out

1. Docker engine metrics, and the containerd socket for the docker group (cAdvisor). `playbooks/swarm.yml` now runs one VM at a time: the daemon.json change restarts dockerd, which stops that node's containers (the alerting stack on swarm-m-01 is down for about a minute), and waits until the node is back in the Swarm with a leader before the next one.

```bash
make swarm
```

2. Traefik's metrics entrypoint (since Swarm HA: the edge stack, [edge.md](edge.md)):

```bash
make edge
```

3. Alloy on the VMs: listens on the VM address, host and container metrics, replica timer on the managers.

```bash
make logs
```

4. Check on the VMs (from the workstation):

```bash
for h in dns-01 dns-02 swarm-m-01 swarm-m-02 swarm-m-03; do \
  printf '%s ' $h; \
  curl -s -o /dev/null -w '%{http_code}\n' \
  http://$h.lab.theloomlabs.org:12345/api/v0/component/prometheus.exporter.unix.node/metrics; \
done
curl -s http://swarm-m-01.lab.theloomlabs.org:9323/metrics | grep -E '^swarm_manager_(leader|nodes)'
curl -s http://swarm.lab.theloomlabs.org:8082/metrics | grep -c '^traefik_'   # Traefik behind the VIP
curl -s http://swarm-m-01.lab.theloomlabs.org:12345/api/v0/component/prometheus.exporter.unix.node/metrics \
  | grep '^swarm_service_replicas'
```

Expected: `200` five times; one manager with `swarm_manager_leader 1`, `swarm_manager_nodes{state="ready"} 3`; a positive count of Traefik metrics; desired and running replicas for the alerting services.

5. Push the commits. Argo CD syncs `kube-prometheus-stack` (ScrapeConfigs, rules, dashboard, network policy, inhibit rule).

6. Prometheus targets and rules:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 &
# http://localhost:19090/targets → scrapeConfig/monitoring/vm-node 5/5, vm-alloy 5/5,
#                                  docker-cadvisor 3/3, docker-engine 3/3, traefik 3/3 UP
# http://localhost:19090/rules   → groups vms, swarm, swarm-backup, swarm-edge
kill $!
```

7. Grafana: dashboard *Docker Swarm* shows data; *Node Exporter / Nodes* lists the VMs as instances.

8. Test an alert with a service that can't be scheduled (on a manager):

```bash
docker service create --detach --name replicas-test \
  --constraint node.labels.replicas-test==true alpine:3 sleep 1d
```

After about 5 minutes ntfy shows `SwarmServiceReplicasMismatch` for `replicas-test`. Then remove it; the resolved message follows:

```bash
docker service rm replicas-test
```

### Troubleshooting VM metrics

- **Target down, `connection refused`:** Alloy still listens on 127.0.0.1 (`sudo grep listen /etc/default/alloy` on the VM, then `make logs`), or dockerd has no `metrics-addr` (`cat /etc/docker/daemon.json`, then `make swarm`).
- **Target down, `context deadline exceeded`:** the `prometheus` network policy doesn't allow that address and port (Hubble: `hubble observe --namespace monitoring --verdict DROPPED`), or the VM is down.
- **No container metrics** (only `id="/"` in the cAdvisor output): `journalctl -u alloy | grep 'container factory'`. cAdvisor needs the Docker socket and, since Docker 29 (images and containers in containerd), the containerd socket: the alloy user is in the docker group (`id alloy`), and the role `docker` gives that group the containerd socket (`[grpc] gid` in `/etc/containerd/config.toml`, `ls -l /run/containerd/containerd.sock` shows group `docker`). Restart Alloy after the socket changes; cAdvisor registers its factories only at start.
- **`SwarmServiceMetricsStale`:** `systemctl status swarm-services-metrics.service` and `journalctl -u swarm-services-metrics` on the manager; `sudo -u alloy docker service ls` must work there.
- **Alloy upgrades:** the exporter paths (`/api/v0/component/...`) are Alloy's HTTP API. Check them after an Alloy upgrade (step 4).

## Resource metrics API (metrics-server)

metrics-server serves `metrics.k8s.io`: the current CPU and memory of every node and pod, read from the kubelets every 15 s. `kubectl top`, Homepage's cluster and app numbers ([homepage.md](homepage.md)) and any future HorizontalPodAutoscaler use it. It keeps nothing; history stays in Prometheus.

| What | Where |
|---|---|
| Argo CD app (wave -6, after cert-manager) | `kubernetes/apps/metrics-server.yaml`, namespace `kube-system` |
| Values | `kubernetes/platform/metrics-server/values.yaml` |

- Two replicas on different workers with a PodDisruptionBudget: the API server sends every `metrics.k8s.io` request to it, and the aggregated API is down while no replica is ready.
- **API server → metrics-server is verified.** The chart creates a self-signed Issuer and Certificate; cert-manager's cainjector writes the CA into the APIService (`insecureSkipTLSVerify: false`).
- **metrics-server → kubelets is not.** The kubelets serve self-signed certificates (`serverTLSBootstrap: false`), so it runs with `--kubelet-insecure-tls`, like Prometheus' kubelet scrape. It connects to the node addresses only (`--kubelet-preferred-address-types=InternalIP`).
- `kube-system` has no network policies, so nothing to allow. Alerts: kube-prometheus-stack's `KubeAggregatedAPIDown` and `KubeAggregatedAPIErrors` cover the APIService.

Check:

```bash
kubectl -n argocd get app metrics-server              # Synced, Healthy
kubectl -n kube-system get pods -l app.kubernetes.io/name=metrics-server -o wide
kubectl -n kube-system get certificate                # metrics-server Ready
kubectl get apiservice v1beta1.metrics.k8s.io         # AVAILABLE True
kubectl top nodes
kubectl top pods -A --sort-by=memory | head
```

| Symptom | Cause / fix |
|---|---|
| APIService `False (FailedDiscoveryCheck)` | Pods not ready, or the CA isn't injected yet: `kubectl get apiservice v1beta1.metrics.k8s.io -o yaml` (annotation `cert-manager.io/inject-ca-from`, non-empty `caBundle`), cert-manager cainjector logs |
| `kubectl top` *metrics not available yet* | Normal for the first minute after a start |
| Logs: *dial tcp …:10250: i/o timeout* | The host firewall drops pod → kubelet traffic: `trusted_cidrs` (pod CIDR) in group_vars/k8s.yml |
| One pod `Pending` during a node drain | Expected with two workers and required anti-affinity; the PDB keeps the other one up |

## Troubleshooting

- **No ntfy message:** `kubectl -n monitoring logs sts/alertmanager-kube-prometheus-stack-alertmanager -c alertmanager | grep -i -E 'notify|webhook'`. `401`/`403` means the token doesn't match the one in `swarm/alerting.sops.yaml`; a DNS or TLS error means the Swarm stack or its certificate isn't up ([alerting.md](alerting.md)).
- **Alertmanager config rejected:** the operator validates it; see `kubectl -n monitoring logs deploy/kube-prometheus-stack-operator | grep -i alertmanager`.
- **A ServiceMonitor is missing from the targets:** Prometheus selects ServiceMonitors from every namespace, so check that the Service exists and its port name matches (`kubectl -n <ns> get svc,servicemonitor -o yaml`).

## Later

- A second receiver (Matrix or Mattermost) for when ntfy is down.
- Metrics from the Proxmox host itself (node metrics, VM states).
- Tune or override etcd latency alerts after about a week of real data (`defaultRules.disabled` plus an own rule).
- Dashboards for Longhorn and Velero as ConfigMaps in Git (Grafana sidecar).
