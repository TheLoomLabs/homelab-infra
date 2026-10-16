# Storage (Longhorn)

Longhorn provides replicated block volumes on the two Kubernetes workers (ADR 0009). Each worker has a dedicated 100 GB data disk mounted at `/var/lib/longhorn`; every volume has one replica per worker. The control-plane nodes hold no storage.

| What | Where |
|---|---|
| Data disk per worker | `data_disk_gb` in `nodes` (`terraform/envs/homelab/variables.tf`), serial `longhorn` |
| Node preparation (iSCSI, LUKS tools, multipath, mount, node label) | `ansible/roles/longhorn_node`, `make longhorn` |
| Longhorn install and settings | `kubernetes/apps/longhorn.yaml`, `kubernetes/platform/longhorn/values.yaml` |
| Default StorageClass `longhorn-encrypted` | `kubernetes/platform/longhorn/storageclass-encrypted.yaml` |
| Volume encryption key | `kubernetes/platform/longhorn/crypto-key.sops.yaml` (SOPS) → Secret `longhorn-system/longhorn-crypto` |
| Backup target (Cloudflare R2) | `defaultBackupStore` in `values.yaml`; token + endpoint in `r2-credentials.sops.yaml` → Secret `longhorn-r2` |
| Snapshot and backup schedule | `kubernetes/platform/longhorn/recurring-jobs.yaml` |

## StorageClasses

| Class | Use for | Encrypted | Reclaim |
|---|---|---|---|
| `longhorn-encrypted` (default) | App data. Snapshots + backups to R2 (job group `encrypted`) | yes (LUKS) | Retain |
| `longhorn` | Data that needs no backup: metrics, caches | no | Delete |
| `longhorn-static` | Created by Longhorn for binding existing volumes; not used directly | no | Delete |

A PVC without `storageClassName` gets `longhorn-encrypted`. Ask for `longhorn` explicitly when the data is disposable.

StorageClass parameters are immutable. To change one, edit the manifest, push, then `kubectl delete sc <name>` and let Argo CD recreate it. Existing volumes keep their settings.

## The encryption key

Every volume of `longhorn-encrypted`, and every backup of one, is encrypted with the key in `longhorn-crypto`. **Without the key, the volumes and their backups can't be read.**

- In Git it is SOPS-encrypted with the age key (docs/secrets.md). Losing the age key and the offline copy of this key together means losing the data.
- Keep an offline copy next to the age key backup:

  ```bash
  sops decrypt kubernetes/platform/longhorn/crypto-key.sops.yaml
  ```

- Never rotate it by editing the value. Volumes created with the old key stay locked with it. Rotation means a new Secret and StorageClass, then migrating the data.

How it was created (the plaintext only goes through a pipe):

```bash
F=kubernetes/platform/longhorn/crypto-key.sops.yaml
KEY=$(openssl rand -base64 48 | tr -d '\n')
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: longhorn-crypto
  namespace: longhorn-system
spec:
  secretTemplates:
    - name: longhorn-crypto
      stringData:
        CRYPTO_KEY_VALUE: "$KEY"
        CRYPTO_KEY_PROVIDER: secret
        CRYPTO_KEY_CIPHER: aes-xts-plain64
        CRYPTO_KEY_HASH: sha256
        CRYPTO_KEY_SIZE: "256"
EOF
unset KEY
```

The heredoc must start at column 0, including the closing `EOF`.

## Check that it works

```bash
kubectl -n argocd get app longhorn                  # Synced / Healthy
kubectl -n longhorn-system get pods | grep -v Running
kubectl -n longhorn-system get nodes.longhorn.io    # both workers Ready + Schedulable
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=N:.metadata.name,ENC:.spec.encrypted,REPL:.spec.numberOfReplicas,ROBUST:.status.robustness,NODE:.status.currentNodeID
```

On a worker:

```bash
findmnt /var/lib/longhorn                # ext4 on the data disk
systemctl is-active iscsid
sudo dmsetup ls --target crypt           # one entry per attached encrypted volume
```

## Backups to Cloudflare R2

Bucket `theloomlabs-longhorn`, backup target `s3://theloomlabs-longhorn@auto/` (R2 needs the region `auto`). The R2 API token has *Object Read & Write* on this bucket only.

| Job | Schedule (UTC) | Keeps |
|---|---|---|
| `snapshot-hourly` | every hour at :15 | 24 local snapshots |
| `backup-daily` | 02:00 | 14 backups in R2 |
| `backup-weekly` | Sunday 03:30 | 8 backups in R2 |

The jobs apply to the group `encrypted`, which every `longhorn-encrypted` volume joins through the StorageClass (`recurringJobSelector`). Volumes of the plain `longhorn` class are in no group and never leave the cluster. Jobs skip volumes without new data and detached volumes.

- **No lifecycle rules on the bucket.** Longhorn deletes old backups itself, and its docs forbid retention rules on the backupstore. The only rule in R2 is Cloudflare's default that aborts unfinished multipart uploads after 7 days.
- Backups of encrypted volumes are ciphertext. Restoring one needs the key `longhorn-crypto` (see above).

Check the target and the backups:

```bash
kubectl -n longhorn-system get backuptargets.longhorn.io        # default: AVAILABLE true
kubectl -n longhorn-system get recurringjobs.longhorn.io
kubectl -n longhorn-system get backupvolumes.longhorn.io
kubectl -n longhorn-system get backups.longhorn.io
```

### Back up a volume now

In the UI (see below): **Volume** → the volume → **Create Backup**.

### Restore a backup into a new volume

This is the disaster-recovery path, for example on a rebuilt cluster that already has Longhorn, the backup target and `longhorn-crypto`.

```bash
B=<backup name from: kubectl -n longhorn-system get backups.longhorn.io>
URL=$(kubectl -n longhorn-system get backups.longhorn.io "$B" -o jsonpath='{.status.url}')
SIZE=$(kubectl -n longhorn-system get backups.longhorn.io "$B" -o jsonpath='{.status.volumeSize}')
NEW=<new volume name, e.g. vaultwarden-restore>

kubectl apply -f - <<EOF
apiVersion: longhorn.io/v1beta2
kind: Volume
metadata:
  name: $NEW
  namespace: longhorn-system
spec:
  size: "$SIZE"
  fromBackup: "$URL"
  numberOfReplicas: 2
  encrypted: true
  frontend: blockdev
  dataEngine: v1
EOF

# Wait until the restore is done: "false detached"
kubectl -n longhorn-system get volumes.longhorn.io "$NEW" -o jsonpath='{.status.restoreRequired} {.status.state}{"\n"}'
```

Then bind it to a PVC. The PV carries the same key references as the StorageClass:

```bash
NS=<namespace>; PVC=<pvc name>; CAP=<size, e.g. 1Gi>
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: $NEW
spec:
  capacity:
    storage: $CAP
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: longhorn-encrypted
  csi:
    driver: driver.longhorn.io
    fsType: ext4
    volumeHandle: $NEW
    volumeAttributes:
      numberOfReplicas: "2"
      encrypted: "true"
    nodeStageSecretRef:
      name: longhorn-crypto
      namespace: longhorn-system
    nodePublishSecretRef:
      name: longhorn-crypto
      namespace: longhorn-system
    nodeExpandSecretRef:
      name: longhorn-crypto
      namespace: longhorn-system
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
  namespace: $NS
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-encrypted
  volumeName: $NEW
  resources:
    requests:
      storage: $CAP
EOF
```

A restored volume does not join the backup schedule by itself:

```bash
kubectl -n longhorn-system label volumes.longhorn.io "$NEW" recurring-job-group.longhorn.io/encrypted=enabled
```

### R2 credentials

`r2-credentials.sops.yaml` is created like this. The values are read from the terminal and never stored in plaintext:

```bash
F=kubernetes/platform/longhorn/r2-credentials.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: longhorn-r2
  namespace: longhorn-system
spec:
  secretTemplates:
    - name: longhorn-r2
      stringData:
        AWS_ACCESS_KEY_ID: "$R2_KEY_ID"
        AWS_SECRET_ACCESS_KEY: "$R2_SECRET"
        AWS_ENDPOINTS: "https://$R2_ACCOUNT.r2.cloudflarestorage.com"
EOF
unset R2_ACCOUNT R2_KEY_ID R2_SECRET
```

To rotate the token: create a new one in the Cloudflare dashboard (R2 → Manage API Tokens, same scope), re-create the file as above, push, check that `backuptargets.longhorn.io` still shows `AVAILABLE true`, then delete the old token.

## Longhorn UI

The UI has no login, so it is not published on the Gateway. Use a port-forward:

```bash
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80
# http://localhost:8080
```

## Delete a volume

With `Retain`, deleting a PVC leaves the PV and the Longhorn volume behind on purpose. When the data is really not needed:

```bash
PV=$(kubectl -n <ns> get pvc <name> -o jsonpath='{.spec.volumeName}')
kubectl -n <ns> delete pvc <name>
kubectl delete pv "$PV"
kubectl -n longhorn-system delete volumes.longhorn.io "$PV"
```

## Node maintenance

Draining a worker that holds the last healthy replica of a volume is blocked until Longhorn has rebuilt a replica elsewhere (`nodeDrainPolicy: block-for-eviction-if-contains-last-replica`). With two workers, drain one at a time and wait until all volumes are `healthy` again before the next.

```bash
kubectl drain k8s-wk-01 --ignore-daemonsets --delete-emptydir-data
# ... maintenance, reboot ...
kubectl uncordon k8s-wk-01
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=N:.metadata.name,ROBUST:.status.robustness
```

## Add a worker

1. Add the node with `data_disk_gb` in `nodes`, `make tf-plan` / apply, `make base`, `make k8s`, `make longhorn`.
2. With three workers, raise the replica count to 3: `numberOfReplicas` in `storageclass-encrypted.yaml` (recreate the StorageClass, see above), `persistence.defaultClassReplicaCount` and `defaultSettings.defaultReplicaCount` in `values.yaml`. Existing volumes keep 2 replicas until changed in the UI or on the `volumes.longhorn.io` object.

## Upgrades

Bump `targetRevision` in `kubernetes/apps/longhorn.yaml` and push. Read the release notes first; Longhorn supports upgrades from the previous minor version only. The pre-upgrade check job is disabled (GitOps); the manager still refuses unsupported upgrade paths at start.
