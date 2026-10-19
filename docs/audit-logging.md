# Kubernetes audit logging

The API server records who did what to which object, and what it answered. The record goes to Loki like every other log, a Grafana dashboard shows it, and Loki's ruler alerts on a few events worth a look.

```
kube-apiserver ──► /var/log/kubernetes/audit/audit.log ──► Alloy (DaemonSet) ──► Loki {source="audit"}
 (policy decides       (on each control-plane node,          (tails the file)          │
  what is kept)          rotated by size)                                               ├─► Grafana: Kubernetes audit
                                                                                        └─► ruler ──► Alertmanager ──► ntfy
```

| Piece | Where |
|---|---|
| Audit policy | `/etc/kubernetes/audit/policy.yaml` on each control-plane node, from `templates/audit-policy.yaml.j2` in role `k8s_control_plane` (variables `k8s_control_plane_audit_*`) |
| API server flags and volumes | `apiServer.extraArgs` / `extraVolumes` in the kubeadm ClusterConfiguration (`kubeadm-init.yaml.j2`, ConfigMap `kube-system/kubeadm-config`) |
| Rollout on a running cluster | `make audit` (`ansible/playbooks/audit.yml`) |
| Shipping | `kubernetes/platform/alloy/values.yaml` (`local.file_match "audit"`) |
| Alerts | `kubernetes/platform/loki/audit-rules.yaml` (ConfigMap `loki-rules-audit`, LogQL), ruler settings in `kubernetes/platform/loki/values.yaml` |
| Ruler health | `kubernetes/platform/loki/prometheusrule.yaml` (`LokiRuler*`, Prometheus) |
| Dashboard | `kubernetes/platform/loki/audit-dashboard.yaml` (Grafana → Dashboards → *Kubernetes audit*) |
| Checks before merge | `make manifests`: kubeconform, and `lokitool rules lint` on the rules ([ci.md](ci.md#loki-rules)) |

## What is logged

Every request passes the policy's rules top to bottom; the first match sets the level. `RequestReceived` is omitted (each request would be logged twice); `ResponseStarted` stays, so an `exec` or a watch shows up when it starts, not only when it ends.

| Requests | Level |
|---|---|
| `/healthz`, `/livez`, `/readyz`, `/metrics`, `/version`, API discovery (`/api`, `/apis`, `/openapi`) | not logged |
| Leases (leader election, node heartbeats), Events | not logged |
| Reads (`get`, `list`, `watch`) by kubelets, controller manager, scheduler, the API server itself, and service accounts in kube-system and longhorn-system | not logged |
| Secrets, ConfigMaps, `serviceaccounts/token`, TokenReviews — for everyone | metadata |
| `exec`, `attach`, `portforward`, proxies | metadata |
| Writes by the components above (mostly status updates) and by Argo CD's application controller | metadata |
| Reads by everyone else (people, other service accounts) | metadata |
| Writes by everyone else | metadata + request body |

Metadata is the user and groups, source IP, user agent, verb, object (resource, namespace, name, subresource), response code and annotations: the authorization decision and its reason, and Pod Security's `pod-security.kubernetes.io/audit-violations` ([pod-security.md](pod-security.md)). No Secret or ConfigMap content ever reaches the log. Argo CD writes only metadata because its content is in Git; a person's `kubectl apply` or `edit` keeps the body, so a change outside Git shows what it changed.

The file on each node is only a buffer for Loki outages: rotated at 100 MB, 3 old files kept, none older than 7 days. Events over 100 KB drop their bodies first (`--audit-log-truncate-enabled`), because Loki rejects lines over 256 KB. In Loki, audit events have the same 15-day retention as all logs.

## Querying

Streams have two labels, `source="audit"` and `host` (control-plane node). Everything else is in the JSON line; `| json` turns fields into labels at query time. Filter lines first, then parse:

```
{source="audit"} |= `"resource":"secrets"`
  | json user="user.username", verb="verb", ns="objectRef.namespace", name="objectRef.name"
  | user !~ "system:.*"
```

Useful fields: `user.username`, `user.groups`, `verb`, `objectRef.resource`, `objectRef.subresource`, `objectRef.namespace`, `objectRef.name`, `responseStatus.code`, `sourceIPs`, `userAgent`, `annotations` (e.g. `annotations["authorization.k8s.io/reason"]`), `requestObject` (writes by people). Users: `kubernetes-admin` is the kubeconfig from `make k8s`; service accounts are `system:serviceaccount:<namespace>:<name>`.

The dashboard *Kubernetes audit* has counters (changes by people, 403s, exec, secret reads, Pod Security violations), events by verb and user, errors by code, and log panels with one line per event (`user verb resource namespace/name → code`). Expand a line for the full event. The *User* variable is a regex for the log panels.

## Alerts

Loki's ruler runs inside the single Loki process. It reads `audit-rules.yaml` (mounted at `/etc/loki/rules/fake`; `fake` is the tenant when auth is off), evaluates every minute and sends to Alertmanager, which routes like Prometheus alerts. All are `warning` (ntfy priority 3). Grafana → Alerting → Alert rules lists them under the Loki data source.

| Alert | Fires when |
|---|---|
| `KubeAuditLogsMissing` | no audit event at all in Loki for 15 minutes (for 5 minutes) |
| `KubeAuditPodExec` | anyone used `exec`, `attach` or `portforward` on a pod in the last 5 minutes |
| `KubeAuditSecretReadByUser` | a user that is not `system:*` read Secrets (get, list, watch) |
| `KubeAuditAnonymousRequest` | a request without credentials reached the API, other than the health checks the policy drops |
| `KubeAuditForbiddenRequests` | one user or service account got more than 10 × 403 in 10 minutes |
| `KubeAuditPodSecurityViolation` | Pod Security audit annotation or rejection outside kube-system, longhorn-system and monitoring (privileged on purpose) |

The exec and secret alerts fire on your own `kubectl exec` and `kubectl get secret` too. That is the point: on a single-admin cluster, one you didn't do is the interesting one. Each resolves by itself 5 minutes later; silence it in Alertmanager beforehand for longer maintenance.

Prometheus watches the ruler itself: `LokiRulerNotEvaluating` (no rule group evaluated for 5 minutes, or none loaded at all), `LokiRuleEvaluationFailures`, `LokiRulerAlertsNotDelivered`. A rule file Loki can't parse makes it keep the rules it had and log `unable to list rules`; `make manifests` runs `lokitool rules lint` so that doesn't reach main.

### Adding a rule

1. Add it to `audit-rules.yaml` (or a new ConfigMap named `loki-rules-<topic>`, mounted in `values.yaml` under `singleBinary.extraVolumes`/`extraVolumeMounts`).
2. Try the expression in Grafana → Explore (Loki) first; alert expressions must return a number (`count_over_time`, `sum by (...)`).
3. `make manifests` (lints the LogQL), commit, push. The ConfigMap reaches the pod within a minute or two and the ruler picks it up on its next poll; no restart.

## Rollout on the running cluster

1. Push; Argo CD syncs `alloy` (tails the file, harmless before it exists) and `loki` (ruler, rules, dashboard; Loki restarts once for the new config and volume).
2. `make audit`: one control-plane node at a time, the kubeadm-config ConfigMap gets the flags and volumes, the API server manifest is regenerated, and the play waits for the API server to be ready and the first events in the file. A changed policy alone restarts the API server container (it reads the policy only at start).

New clusters get everything from `make k8s`.

### Checks

Flags and file on every control-plane node:

```
ansible k8s_control_plane -b -m ansible.builtin.shell \
  -a "grep -c -- '--audit-' /etc/kubernetes/manifests/kube-apiserver.yaml; ls -l /var/log/kubernetes/audit/"
```

Expected: `7` and a growing `audit.log` on each node.

Events in Loki (Grafana → Explore → Loki):

```
sum by (host) (count_over_time({source="audit"}[5m]))
```

Expected: one series per control-plane node.

The ruler has the rules and can reach Alertmanager. Trigger one alert on purpose:

```
kubectl -n monitoring port-forward svc/loki 13100:3100 >/dev/null &
sleep 2
curl -s http://127.0.0.1:13100/loki/api/v1/rules | grep 'alert:'
kill $!
kubectl -n argocd exec deploy/argocd-server -- true
```

Within about two minutes `KubeAuditPodExec` shows in Grafana → Alerting and arrives in ntfy.

## Volume

Most of the volume is writes by controllers and operators at metadata level, plus reads by people and service accounts outside kube-system. Check after a week:

```
sum by (user) (count_over_time({source="audit"} | json user="user.username" [24h]))
```

A noisy service account gets its own `None` or `Metadata` rule in the policy (variables `k8s_control_plane_audit_system_users`, `_system_groups`, `_metadata_users`), then `make audit`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `KubeAuditLogsMissing` | `ls -l /var/log/kubernetes/audit/` on a control-plane node: no growing file → `grep audit /etc/kubernetes/manifests/kube-apiserver.yaml`, run `make audit`. File grows → Alloy on that node: `kubectl -n monitoring logs ds/alloy` (pod on that node), look for `audit` errors |
| API server won't start after `make audit` | `crictl ps -a --name kube-apiserver` and `crictl logs <id>` on the node: usually a policy the API server can't parse. Fix the template, `make audit` again; the other two API servers kept serving |
| `KubeAuditForbiddenRequests` for a service account after an upgrade | The chart's new version needs RBAC it doesn't have yet, or a controller retries a denied call. Look at `annotations["authorization.k8s.io/reason"]` and the request in the dashboard |
| `LokiRulerNotEvaluating` | `kubectl -n monitoring logs sts/loki -c loki \| grep -i rule`. `unable to list rules`: a file doesn't parse (run `lokitool rules lint --dry-run` on it). No ruler lines: check `ruler` in `kubectl -n monitoring get cm loki -o yaml` |
| `LokiRulerAlertsNotDelivered` | Loki can't reach `kube-prometheus-stack-alertmanager:9093`: the CNPs `loki` (egress) and `alertmanager` (ingress) in [network-policy.md](network-policy.md), and the Alertmanager pod |
| Loki discards audit lines (`LokiDiscardingLogs`, reason `line_too_long`) | An event over 256 KB despite truncation; raise `limits_config.max_line_size` or keep that request at metadata level |
| `kubeadm upgrade` | The flags and volumes are in the ClusterConfiguration, so upgrades keep them; pass `--patches /etc/kubernetes/patches` as always ([monitoring.md](monitoring.md)) |
