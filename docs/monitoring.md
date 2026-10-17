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

## Troubleshooting

- **No ntfy message:** `kubectl -n monitoring logs sts/alertmanager-kube-prometheus-stack-alertmanager -c alertmanager | grep -i -E 'notify|webhook'`. `401`/`403` means the token doesn't match the one in `swarm/alerting.sops.yaml`; a DNS or TLS error means the Swarm stack or its certificate isn't up ([alerting.md](alerting.md)).
- **Alertmanager config rejected:** the operator validates it; see `kubectl -n monitoring logs deploy/kube-prometheus-stack-operator | grep -i alertmanager`.
- **A ServiceMonitor is missing from the targets:** Prometheus selects ServiceMonitors from every namespace, so check that the Service exists and its port name matches (`kubectl -n <ns> get svc,servicemonitor -o yaml`).

## Later

- Prometheus scrapes the Swarm nodes and Traefik, so Kubernetes also watches the alerting stack; a second receiver (Matrix or Mattermost) for when ntfy is down.
- Control-plane metrics: bind etcd (`listen-metrics-urls`), kube-scheduler and kube-controller-manager to the node address in the kubeadm config and the static pod manifests, then enable `kubeEtcd`, `kubeScheduler`, `kubeControllerManager` and their default rules.
- Dashboards for Longhorn and Velero as ConfigMaps in Git (Grafana sidecar).
