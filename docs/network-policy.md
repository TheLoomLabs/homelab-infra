# Network policies (Cilium)

Every platform namespace is **default deny** for ingress and egress. A pod only gets the connections a CiliumNetworkPolicy allows, and the internet only by name (`toFQDNs`). That limits what a compromised or misconfigured pod can reach: no scanning the LAN, no calling home, no reading another app's database.

| Piece | Where |
|---|---|
| Policies | `kubernetes/platform/<component>/networkpolicy.yaml`, synced by the component's own Argo CD app |
| Audit mode switch | `cilium_policy_audit_mode` in `ansible/inventory/group_vars/k8s.yml` → Cilium `policyAuditMode` (`make cilium`) |
| Would-be drops and drops on each node | `/var/run/cilium/hubble/policy-verdicts.log` (Hubble static exporter: AUDIT, and DROPPED by policy; 5 × 10 MB) |
| Summary of flows | `scripts/hubble-flows.py` |
| Checks before merge | `make manifests`: every app namespace has a `default-deny` policy; kubeconform validates the policies against the pinned Cilium CRDs ([ci.md](ci.md#network-policies)) |

## Scope

| Namespace | Policies |
|---|---|
| argocd, cert-manager, sops, velero, gateway, monitoring | default deny + allows (below) |
| Phase 3 app namespaces (vaultwarden, homepage) | default deny from the first commit (`make manifests` fails without it) |
| kube-system, longhorn-system | none: the CNI, kube-vip, control plane and Longhorn's node agents. Longhorn ships its own policies (`networkPolicies.restrictInternalTraffic`) |

Not affected in any namespace: pods in the host network (node-exporter), and the kubelet's probes (Cilium always allows the local node).

## How the policies are built

Each namespace has one policy named **`default-deny`** with `endpointSelector: {}`. Selecting every pod turns on default deny for all of them, and this policy allows only what every pod needs:

- **DNS** to kube-dns, through Cilium's DNS proxy (`rules.dns`). The proxy is what makes `toFQDNs` work: it sees the lookup and opens the IPs it returned, for as long as the answer's TTL.
- **Scrapes** from Prometheus (`monitoring`, `app.kubernetes.io/name: prometheus`), on any port.

Everything else is one policy per component, named for what it allows:

| From | To | Port |
|---|---|---|
| Argo CD controller, applicationset, server | API server (`kube-apiserver` entity) | 6443 |
| Argo CD pods | repo-server, redis (same namespace) | 8081, 6379 |
| Gateway (`ingress` entity) | argocd-server | 8080 |
| argocd-repo-server | github.com, `*.github.io`, `*.githubusercontent.com`, charts.jetstack.io, charts.longhorn.io | 443 |
| cert-manager pods | API server | 6443 |
| API server (`kube-apiserver`, `remote-node`: through the tunnel it comes from its node's cilium_host address) | cert-manager webhook | 10250 |
| cert-manager controller | acme-v02 / acme-staging-v02.api.letsencrypt.org, api.cloudflare.com | 443 |
| cert-manager controller | 1.1.1.1, 1.0.0.1 (DNS-01 propagation checks) | 53 |
| sops-secrets-operator | API server | 6443 |
| Velero | API server; `*.r2.cloudflarestorage.com` | 6443; 443 |
| Prometheus | the whole cluster (`cluster` entity: pods, nodes, API server); the DNS and Swarm VMs (`toCIDR`, their LAN addresses) | any; VMs: 12345 (Alloy), 9323 (Docker), 8082 (Traefik) |
| Grafana | Prometheus, Alertmanager, Loki; API server (sidecars) | 9090, 9093, 3100; 6443 |
| Gateway | Grafana; loki-gateway | 3000; 8080 |
| Alertmanager | ntfy.lab / hc.lab.theloomlabs.org (Swarm) | 443 |
| Loki (ruler) | Alertmanager | 9093 |
| kube-state-metrics, Prometheus operator | API server | 6443 |
| API server (same) | Prometheus operator webhook | 10250 |
| loki-gateway, Alloy, Grafana | Loki | 3100 |
| Loki | Loki (memberlist, gRPC) | 7946, 9095 |
| Alloy (both releases) | API server | 6443 |
| Gateway | vaultwarden | 8080 |
| Vaultwarden backup jobs | `*.r2.cloudflarestorage.com` (Vaultwarden itself: nothing) | 443 |
| Gateway | homepage | 3000 |
| Homepage | API server; argocd-server; Prometheus | 6443; 8080; 9090 |
| Homepage | status / portainer / ntfy / hc.lab.theloomlabs.org (Swarm); pve.lab.theloomlabs.org | 443; 8006 |

The R2 rule matches any R2 endpoint because the hostname contains the account ID, which stays out of Git. Grafana has no internet access, so `values.yaml` turns off Gravatar and the news feed.

## Rollout: audit mode first

1. **Audit mode on.** `cilium_policy_audit_mode: true`, then `make cilium`. Policies now log what they would drop (verdict `AUDIT`) and let it through.
2. **Policies.** Push; each Argo CD app syncs its `networkpolicy.yaml`.
3. **A few days of audit.** That covers the nightly Velero backup (03:00 UTC), Argo CD syncs and chart pulls, alerts and the Alertmanager watchdog, and Grafana use. Review the would-be drops (below) and add the missing allows.
4. **Enforce.** `cilium_policy_audit_mode: false`, then `make cilium`. Watch for drops for a day.

Audit mode is cluster-wide. While it is on, no policy drops anything, Longhorn's included.

### Review

Every would-be drop since the agents started, from all nodes:

```
for p in $(kubectl -n kube-system get pods -l k8s-app=cilium -o name); do
kubectl -n kube-system exec "$p" -c cilium-agent -- \
  cat /var/run/cilium/hubble/policy-verdicts.log; done \
  | python3 scripts/hubble-flows.py --since 24h
```

`--since` drops older lines, such as leftovers from the minute in which the policies were synced one after the other. The `LAST` column shows when each connection was last seen.

Live, through Hubble Relay:

```
cilium hubble port-forward &
hubble observe --follow --verdict AUDIT --verdict DROPPED
kill %1
```

Each line of `hubble-flows.py` is one connection: source, destination (namespace/app, or an entity like `kube-apiserver`, `world(name)`), port, verdict (with the drop reason) and count. A legitimate line becomes an allow in that component's `networkpolicy.yaml`. Anything else is a finding.

### Checks

```
kubectl get ciliumnetworkpolicies -A
cilium config view | grep -E 'policy-audit-mode|enable-policy'
```

Every policy should show `VALID True`. Then check that each component still works: Argo CD syncs (hard refresh the root app), Grafana dashboards and Explore (Loki), a test alert reaches ntfy, `kubectl -n velero get backups.velero.io` has a new completed backup, and `kubectl get certificates -A` shows Ready.

## Adding a component or an app

1. Put the app in its own namespace with a `networkpolicy.yaml` next to its manifests: a `default-deny` (copy one, with the namespace changed), then one policy per pod with what it needs. Use `fromEntities: [ingress]` for traffic through the shared Gateway, `toEntities: [kube-apiserver]` for the API, `toFQDNs` for anything outside.
2. Allow it into the target's namespace too, if it calls another namespace (both ends need a policy).
3. `make manifests` fails without a `default-deny` in the app's namespace.
4. A new app can't wait for audit mode. Watch `hubble observe --namespace <ns> --verdict DROPPED` while you deploy it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `hubble observe` shows `DROPPED` with `Policy denied` | Missing allow: add it to the source's egress **and** the destination's ingress |
| Name-based egress drops right after a pod starts | The pod resolved the name before the policy existed, or uses its own resolver. Restart the pod; check it uses kube-dns |
| A policy shows `VALID False` | `kubectl describe cnp -n <ns> <name>`; usually a typo the CI schema check would also catch |
| Everything dropped in a namespace | `default-deny` without the DNS rule: names don't resolve, so every FQDN rule fails too |
| Argo CD can't pull a new chart | Add the chart repo's host (and where it redirects the download) to `repo-server-to-git-and-charts` |
