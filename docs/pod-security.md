# Pod Security Admission

The API server's built-in PodSecurity admission checks every new pod against one of the three [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/):

- **privileged**: no limits. Only for node agents that need the host.
- **baseline**: no host namespaces, hostPath, privileged containers or extra capabilities.
- **restricted**: baseline, plus non-root, no privilege escalation, all capabilities dropped and the `RuntimeDefault` seccomp profile.

Each namespace has three labels, one for each mode:

| Mode | When a pod breaks the level |
|---|---|
| `enforce` | the API server rejects it |
| `audit` | the request is annotated in the API server audit log |
| `warn` | the client that sent the request gets a warning (kubectl prints it) |

| Piece | Where |
|---|---|
| Level per namespace | `kubernetes/platform/namespaces/namespaces.yaml` (Argo CD app `namespaces`, wave -15); the gateway namespace in `kubernetes/platform/gateway/namespace.yaml` |
| Defaults for namespaces without labels | `/etc/kubernetes/admission/config.yaml` on each control-plane node, from `templates/admission-config.yaml.j2` in role `k8s_control_plane` (variables `k8s_control_plane_pod_security_*`) |
| API server flag and volume | `apiServer.extraArgs` / `extraVolumes` in the kubeadm ClusterConfiguration (`kubeadm-init.yaml.j2`, ConfigMap `kube-system/kubeadm-config`) |
| Rollout on a running cluster | `make pod-security` (`ansible/playbooks/pod-security.yml`) |
| Checks before merge | `make manifests` ([ci.md](ci.md#pod-security-labels)) |

## Levels

| Namespace | enforce | audit + warn | Why |
|---|---|---|---|
| kube-system | privileged | – | static control-plane pods, Cilium, kube-vip; also exempt in the defaults |
| longhorn-system | privileged | – | CSI plugin, engine images, instance managers |
| monitoring | privileged | restricted | node-exporter (host network/PID, hostPath), Alloy DaemonSet (hostPath journal) |
| argocd, cert-manager, gateway, sops, velero | restricted | restricted | |
| default, kube-public, kube-node-lease, cilium-secrets | restricted | restricted | no pods |
| any other namespace (no labels) | baseline | restricted | cluster defaults |

`audit` and `warn` use `latest`. `enforce: restricted` is pinned to the cluster's minor version (`enforce-version: v1.37`), so a Kubernetes upgrade never starts rejecting pods by itself. Bump it after an upgrade, once `warn` (on `latest`) shows nothing new.

Why monitoring stays `privileged` and isn't split up: node-exporter comes with kube-prometheus-stack and Alloy needs the node's journal. Moving them to their own namespace would mean re-creating running releases, for little gain on a single-admin cluster. `warn: restricted` there still shows what the other pods lack.

## How it works

- **Labels from Git.** The `namespaces` app labels every namespace with server-side apply, so it owns only the labels and its annotations, not namespaces other tools created (kube-system, cilium-secrets). Every Namespace carries `argocd.argoproj.io/sync-options: Prune=false,Delete=false` and the app has `prune: false`: removing a line or the app never deletes a namespace.
- **Bootstrap.** Role `argocd` applies the same file before it installs Argo CD, so the labels exist before the first platform pod. Argo CD's sync waves don't wait for one Application to finish before the next, so `CreateNamespace` could otherwise come first.
- **Apps can't lower their level.** The `apps` AppProject allows no cluster-scoped kinds, not even `Namespace`. A Phase 3 app's namespace is added to `namespaces.yaml` (with `restricted`) by the platform.
- **Cluster defaults.** The API server's `--admission-control-config-file` sets `baseline` enforce and `restricted` audit/warn for namespaces without labels, so a namespace that skipped Git (`kubectl create ns`) is still not privileged. kube-system is exempt, so a fresh cluster can start its control plane and Cilium before Argo CD exists.
- **Existing pods are never evicted.** PodSecurity checks pods when they are created. A label change only warns about running pods that break it; they are rejected at their next restart.

## Rollout on the running cluster

Done in steps, each one checked before the next:

1. **Labels, audit + warn only.** Push; Argo CD syncs the `namespaces` app. The privileged namespaces get `enforce: privileged` right away (no change in behaviour); everything else only gets `audit` and `warn`.
2. **Charts fixed.** sops-secrets-operator and Velero run as non-root with the restricted security context (their values files).
3. **Cluster defaults.** `make pod-security`: a server-side dry run checks that no running pod in a namespace without an `enforce` label breaks `baseline`, then the API servers are reconfigured one at a time.
4. **Enforce.** Once there are no warnings, the restricted namespaces get `enforce: restricted` and `enforce-version: v1.37`.

### Checks

No warnings for the restricted namespaces (should print only `labeled (server dry run)` lines, except kube-system, longhorn-system and monitoring):

```
kubectl label --dry-run=server --overwrite ns --all \
  pod-security.kubernetes.io/enforce=restricted
```

Labels and the defaults in place:

```
kubectl get ns -L pod-security.kubernetes.io/enforce \
  -L pod-security.kubernetes.io/warn
ansible k8s_control_plane -b -m ansible.builtin.shell \
  -a "grep -c admission-control-config-file /etc/kubernetes/manifests/kube-apiserver.yaml"
```

The defaults reject a privileged pod in a namespace without labels:

```
kubectl create namespace psa-test
kubectl --namespace psa-test run psa-test --image=busybox:1.37 \
  --overrides='{"spec":{"hostNetwork":true}}' -- sleep 60
kubectl delete namespace psa-test
```

Expected: `Error from server (Forbidden): pods "psa-test" is forbidden: violates PodSecurity "baseline:latest": host namespaces (hostNetwork=true)`.

## Adding a namespace

1. Add a Namespace to `kubernetes/platform/namespaces/namespaces.yaml` with `audit` and `warn` set to `restricted`, and `enforce: restricted` + `enforce-version: v1.37` once the app's pods pass. Copy the annotations of the others.
2. Add the namespace to the `destinations` in `kubernetes/apps/projects.yaml`: of the app's project, and, for a Phase 3 app, also of `platform` (the namespaces app, project `platform`, creates the Namespace object).
3. `make manifests` fails if an Application's destination namespace has no labelled Namespace.

A chart that needs more than `restricted` gets the lowest level that works, and a comment saying which pod needs what.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| ReplicaSet or DaemonSet events `violates PodSecurity "restricted:v1.37"` | The chart's pods lack a security context field. Set it in the values (see sops-secrets-operator and Velero), or give the namespace a lower level with a comment |
| `make pod-security` stops at "No running pod breaks the defaults" | A namespace without an `enforce` label runs pods that need more than baseline. Label it in Git, push, wait for Argo CD, run again |
| Role `k8s_control_plane` fails with "no admission config yet" | `make k8s` reached a running API server without the flag. Use `make pod-security`, which runs the check first |
| `kubeadm upgrade` | The flag and the volume are in the ClusterConfiguration, so upgrades keep them. Bump `enforce-version` afterwards |
