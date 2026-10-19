# Encryption at rest (Secrets and ConfigMaps)

The API server encrypts Secrets and ConfigMaps before it stores them in etcd. Someone who gets hold of the etcd data on a control-plane disk, an etcd snapshot or a Proxmox backup of a control-plane VM, only sees ciphertext.

| Piece | Where |
|---|---|
| Key(s) | `k8s/encryption.sops.yaml` (SOPS, variable `k8s_control_plane_encryption_keys`) |
| Config on each control-plane node | `/etc/kubernetes/encryption/config.yaml` (root, 0600), from `templates/encryption-config.yaml.j2` in role `k8s_control_plane` |
| API server flag and volume | `apiServer.extraArgs` / `extraVolumes` in the kubeadm ClusterConfiguration (`kubeadm-init.yaml.j2`, ConfigMap `kube-system/kubeadm-config`) |
| Rollout on a running cluster | `make encryption` (`ansible/playbooks/encryption.yml`) |
| Alerts | `kubernetes/platform/monitoring/encryption-prometheusrule.yaml` |

## How it works

- Provider **secretbox** (XSalsa20-Poly1305) with a 32-byte key. The first key in `k8s_control_plane_encryption_keys` encrypts new writes; every key in the list can decrypt. `identity` (plaintext) comes last, so objects written before encryption was turned on can still be read.
- Stored values start with `k8s:enc:secretbox:v1:<key name>:`. That prefix is how the API server picks the key, and how the checks below tell encrypted from plaintext.
- The API server reads the config file only at start. Ansible restarts it, one control-plane node at a time, whenever the file changes.
- **New cluster:** role `k8s_control_plane` places the config file before `kubeadm init` and `join`. The flag and the volume are in the ClusterConfiguration, so every API server starts with encryption from the first object on.
- **Running cluster:** `make encryption` adds the flag and the volume to the `kubeadm-config` ConfigMap, then regenerates each API server manifest with `kubeadm init phase control-plane apiserver`. Because the setting lives in the ClusterConfiguration, `kubeadm upgrade` and new control-plane nodes keep it.

What it does **not** protect against:

- Anyone with API access who can read Secrets (RBAC decides that), or root on a control-plane node, where the key file is.
- Velero backups. Velero reads objects through the API, so its backups hold plaintext Secrets. Those are protected by SSE-C and the bucket lock instead ([backup.md](backup.md)).

## One-time: create the key

The key is generated on the workstation and goes straight into SOPS:

```
devbox shell
mkdir -p k8s
f=k8s/encryption.sops.yaml
key=$(head -c 32 /dev/urandom | base64)
printf 'k8s_control_plane_encryption_keys:\n  - name: key1\n    secret: "%s"\n' "$key" |
  sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f"
unset key
make sops-check
```

The key is in Git, encrypted with the age key. The age key backup ([secrets.md](secrets.md#one-time-the-age-key)) therefore covers it too. **Without the key, nothing encrypted in etcd can be read.**

## Turn it on (running cluster)

Each pass restarts the three API servers one after the other. The other two keep serving, and kube-vip moves the VIP if needed.

1. Pass 1: every API server gets the key, but still writes plaintext. Otherwise an API server that is already encrypting would store Secrets that the not-yet-updated ones can't read.

```
make encryption ARGS="-e k8s_control_plane_encryption_identity_first=true"
```

2. Pass 2: the key goes first. The playbook then rewrites every Secret and ConfigMap, reads all values back from etcd and fails if any of them is not encrypted with the active key.

```
make encryption
```

The last task prints `All N values in etcd start with k8s:enc:secretbox:v1:key1:`.

A run on a node that has no encryption flag yet, without pass 1, stops before it changes anything.

## Check

API server flag on every control-plane node:

```
cd ansible && ansible k8s_control_plane -b -m ansible.builtin.shell \
  -a 'grep -- --encryption-provider-config /etc/kubernetes/manifests/kube-apiserver.yaml'
```

One Secret, read directly from etcd (the etcd image has no shell, so the output is cut on the workstation):

```
kubectl -n default create secret generic encryption-test --from-literal=foo=bar
kubectl -n kube-system exec etcd-k8s-cp-01 -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/secrets/default/encryption-test --print-value-only | head -c 32; echo
kubectl -n default delete secret encryption-test
```

It prints `k8s:enc:secretbox:v1:key1:` followed by binary data. Plaintext would show `k8s` followed by the protobuf object, with `foo` and `bar` readable.

In Prometheus (Grafana → Explore), what each API server reads and writes:

```
sum by (instance, transformation_type, transformer_prefix) (
  rate(apiserver_storage_transformation_operations_total[5m])
)
```

Writes (`to_storage`) should only show the `k8s:enc:secretbox:v1:<active key>:` prefix.

## Rotate the key

Rotate after a suspected leak, or every year or so. Every step is a full `make encryption` run, and each one waits for the previous step to be on all three nodes.

1. Add a new key at the **end** of the list. Every API server can now decrypt with it; the old key still encrypts.

```
sops edit k8s/encryption.sops.yaml
```

```
k8s_control_plane_encryption_keys:
  - name: key1
    secret: "...old..."
  - name: key2
    secret: "...output of: head -c 32 /dev/urandom | base64..."
```

```
make encryption
```

2. Move `key2` to the top of the list. New writes use it; the playbook rewrites everything and checks for the `key2` prefix.

```
sops edit k8s/encryption.sops.yaml
make encryption
```

3. Remove `key1` and roll out once more. From now on, an old etcd snapshot needs the old key to be read.

```
sops edit k8s/encryption.sops.yaml
make encryption
```

Commit `k8s/encryption.sops.yaml` after each step.

## Upgrades, rebuilds and restores

- `kubeadm upgrade` keeps the flag and the volume (they are in the ClusterConfiguration). Pass `--patches` as before ([monitoring.md](monitoring.md)).
- A rebuilt or new control-plane node gets the config file from `make k8s` before it joins.
- Restoring etcd from a snapshot needs the key that was active when the snapshot was taken. Keep removed keys (from Git history) until no snapshot needs them.
- Lost key with the cluster still running: the API servers still have it in memory and on disk. Copy `/etc/kubernetes/encryption/config.yaml` from a control-plane node back into SOPS before anything restarts.

## Alerts

| Alert | Severity | Meaning |
|---|---|---|
| `KubeAPIServerEncryptionErrors` | critical | An API server fails to encrypt or decrypt, or finds data no configured key can read (for example a key removed too early). |
| `KubeAPIServerWritesUnencrypted` | warning | An API server still writes plaintext an hour after pass 1. Run pass 2. |
| `KubeAPIServerEncryptionNotConfigured` | warning | An API server runs without an encryption config, for example after its manifest was regenerated from an old configuration. Run `make encryption`. |

A rollout takes each API server down for well under a minute. That is shorter than the `for:` of the kube-prometheus-stack alerts (`TargetDown` waits 10 minutes), so a normal run stays quiet.
