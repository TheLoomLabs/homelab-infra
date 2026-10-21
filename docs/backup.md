# Backups (Velero)

Swarm volumes are covered separately: [swarm-backup.md](swarm-backup.md). Vaultwarden and Forgejo add their own app-level backups (consistent dump + restic to R2) on top of this: [vaultwarden.md](vaultwarden.md#backups), [forgejo.md](forgejo.md#backups).

Velero backs up the Kubernetes objects of the cluster to Cloudflare R2 every night and, through CSI VolumeSnapshots, has Longhorn back up every volume at the same time (ADR 0010). One Velero backup can restore a whole namespace: objects, PVCs and the data behind them.

| What | Where |
|---|---|
| Velero install and settings | `kubernetes/apps/velero.yaml`, `kubernetes/platform/velero/values.yaml` |
| R2 token, endpoint, SSE-C key | `kubernetes/platform/velero/r2-credentials.sops.yaml` (SOPS) → Secret `velero/velero-r2` |
| CSI snapshot CRDs + controller | `kubernetes/apps/snapshot-controller.yaml` (external-snapshotter, same version as Longhorn's csi-snapshotter) |
| How volumes are snapshotted | VolumeSnapshotClass `longhorn-backup` (`kubernetes/platform/longhorn/volumesnapshotclass.yaml`): a Longhorn backup to R2 |
| Volume backups without Velero | Longhorn RecurringJobs (docs/storage.md) |

## What is where

| Bucket | Content | Encryption |
|---|---|---|
| `theloomlabs-velero` | Velero: object manifests (including Secrets), backup metadata | SSE-C with our key (`customer-key`) |
| `theloomlabs-longhorn` | Longhorn: volume blocks, from RecurringJobs and from Velero's VolumeSnapshots | LUKS (`longhorn-crypto`), data encrypted before it leaves the node |

Each bucket has its own R2 token, limited to that bucket. The R2 account ID only exists inside the encrypted Secrets.

**Keys to keep offline**, next to the age key: the SSE-C key, the Longhorn key and the restic passwords of the Swarm backups ([swarm-backup.md](swarm-backup.md)) and the Vaultwarden and Forgejo backups ([vaultwarden.md](vaultwarden.md), [forgejo.md](forgejo.md)). Without them, the backups can't be read.

```bash
sops decrypt kubernetes/platform/velero/r2-credentials.sops.yaml | grep customer-key
sops decrypt kubernetes/platform/longhorn/crypto-key.sops.yaml | grep CRYPTO_KEY_VALUE
```

## Schedule

| Schedule | When (UTC) | Keeps | Scope |
|---|---|---|---|
| `velero-daily` | 03:00 | 30 days | all namespaces except `longhorn-system`; without CiliumEndpoints/Identities |

`longhorn-system` is excluded: Longhorn's own objects (volumes, replicas, engines) are restored by Longhorn, never by Velero. Velero deletes expired backups, including the Longhorn backups of their VolumeSnapshots. No lifecycle rules on either bucket.

## Check

The `velero` CLI isn't needed; kubectl shows everything. Use the full resource names (`backups.velero.io`, `restores.velero.io`): Longhorn also has a `Backup` kind, and `kubectl get backup` resolves to `backups.longhorn.io`.

```bash
kubectl -n velero get backupstoragelocations          # default: PHASE Available
kubectl -n velero get schedules
kubectl -n velero get backups.velero.io --sort-by=.metadata.creationTimestamp
kubectl -n velero get backups.velero.io <name> -o jsonpath='{.status.phase} errors={.status.errors} warnings={.status.warnings}{"\n"}'
kubectl get volumesnapshots -A
```

`velero backup logs` and `velero backup describe --details` download files through presigned URLs, which don't carry the SSE-C key, so they don't work with this bucket. Use the server log instead:

```bash
kubectl -n velero logs deploy/velero | grep <backup name>
```

Known harmless restore warnings: `ConfigMap:kube-root-ca.crt already exists` (Kubernetes creates it in every new namespace) and `No annotations found for …`.

### Velero started before the snapshot CRDs

On a fresh cluster Argo CD may start Velero before `snapshot-controller` has installed the VolumeSnapshot CRDs. Velero then caches an API list without them and every backup ends `PartiallyFailed` with `no matches for snapshot.storage.k8s.io/, Resource=volumesnapshots`, although the Longhorn backup itself completes. Fix: `kubectl -n velero rollout restart deploy/velero`, then delete the orphaned VolumeSnapshots of the failed backup (`kubectl get volumesnapshots -A`).

Velero's `Backup` and `Restore` kinds have no status columns, so `kubectl get … -w` shows only the name. Poll the phase instead:

```bash
until s=$(kubectl -n velero get backups.velero.io <name> -o jsonpath='{.status.phase}'); [[ "$s" =~ ^(Completed|PartiallyFailed|Failed|FailedValidation)$ ]]; do echo "${s:-New}"; sleep 10; done; echo "$s"
```

## Back up now

```bash
kubectl apply -f - <<EOF
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: manual-$(date +%Y%m%d-%H%M)
  namespace: velero
spec:
  includedNamespaces: [<namespace>]
  snapshotVolumes: true
  storageLocation: default
  ttl: 720h
EOF
```

## Restore a namespace

The namespace must not exist (or be deleted first). Velero recreates the objects; each PVC is provisioned from its VolumeSnapshot, which Longhorn restores from R2.

```bash
kubectl apply -f - <<EOF
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: restore-$(date +%Y%m%d-%H%M)
  namespace: velero
spec:
  backupName: <backup name>
  includedNamespaces: [<namespace>]
  restorePVs: true
EOF
kubectl -n velero get restores.velero.io -w     # Completed
```

Apps managed by Argo CD come back from Git as well; Velero matters for what Git doesn't have: PVC data, runtime-created Secrets, and the exact state at backup time.

## Rotate the R2 token

Create a new token (R2 → Manage API Tokens, Object Read & Write on `theloomlabs-velero` only), re-create `r2-credentials.sops.yaml` **with the same `customer-key`** (old backups need it), push, check `backupstoragelocations` is Available, then delete the old token.

How the file is created (values read from the terminal, never stored in plaintext):

```bash
mkdir -p kubernetes/platform/velero
F=kubernetes/platform/velero/r2-credentials.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
SSEC=$(openssl rand -base64 24)   # rotation: paste the existing key instead
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: velero-r2
  namespace: velero
spec:
  secretTemplates:
    - name: velero-r2
      stringData:
        cloud: |
          [default]
          aws_access_key_id=$R2_KEY_ID
          aws_secret_access_key=$R2_SECRET
          endpoint_url=https://$R2_ACCOUNT.r2.cloudflarestorage.com
        customer-key: "$SSEC"
EOF
unset R2_ACCOUNT R2_KEY_ID R2_SECRET SSEC
```

## Disaster recovery (new cluster)

1. Rebuild the VMs and the cluster (docs/bootstrap.md); restore the age key; Argo CD syncs everything from Git, including Longhorn, the backup targets and Velero.
2. Velero finds the existing backups in R2 by itself (`kubectl -n velero get backups.velero.io`).
3. Restore the app namespaces one by one, as above.
