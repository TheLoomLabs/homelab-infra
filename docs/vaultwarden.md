# Vaultwarden (password manager)

[Vaultwarden](https://github.com/dani-garcia/vaultwarden) is a Bitwarden-compatible server: the official Bitwarden apps, browser extensions and CLI talk to it. It runs on Kubernetes at **https://vault.lab.theloomlabs.org**, LAN and Tailscale only, like every lab name. Why Kubernetes, SQLite and an extra backup: [ADR 0018](adr/0018-vaultwarden-kubernetes.md).

| What | Where |
|---|---|
| Argo CD app (project `apps`) | `kubernetes/apps/vaultwarden.yaml` |
| Manifests | `kubernetes/services/vaultwarden/` |
| Settings (environment) | `config.yaml` (ConfigMap `vaultwarden-env`) |
| Admin token hash, restic + R2 credentials | `secrets.sops.yaml` (SOPS) → Secrets `vaultwarden-admin`, `vaultwarden-restic` |
| Deployment, PVC, Service, HTTPRoute | `vaultwarden.yaml` |
| Nightly dump + restic, weekly check | `backup.yaml` |
| Network policies | `networkpolicy.yaml` |
| Backup alerts | `prometheusrule.yaml` |
| Namespace + Pod Security level | `kubernetes/platform/namespaces/namespaces.yaml` (`restricted`) |
| Uptime monitor | `vaultwarden` in `uptime_kuma_monitors` (`make uptime`) |
| restic from the workstation | `scripts/vaultwarden-restic.sh <restic args>` |

## How it runs

- One pod (Deployment, `Recreate`), image `vaultwarden/server` (Debian), as UID 1000 on port 8080, read-only root filesystem.
- Data in `/data` on the PVC `vaultwarden-data`: 2 GiB, StorageClass `longhorn-encrypted` (LUKS, 2 replicas, hourly snapshots, daily and weekly Longhorn backups to R2), `Retain`, never pruned by Argo CD. SQLite (`db.sqlite3`, WAL), `attachments/`, `sends/`, `rsa_key.pem` (signs login tokens), `config.json` (only if someone saves settings on /admin), `backups/` (nightly dumps).
- Through the shared Gateway (`HTTPRoute vaultwarden`), TLS with the wildcard certificate. The `*.lab` record already points at the Gateway: no DNS change.
- **No egress.** The pod reaches nothing but DNS: icon downloads, push notifications (Bitwarden's relay), SMTP and Have I Been Pwned are all off. Turn **Settings → Appearance → Show website icons** off in the clients, or they show placeholders.
- **No self-registration.** `SIGNUPS_ALLOWED=false`; accounts are invited on `/admin` ([Add a user](#add-a-user)). Password hints are off.
- **No SMTP.** No mails: invitations are accepted by registering, 2FA is TOTP or a passkey, never email. New-device and failed-login mails don't exist; failed logins are in the log (`{namespace="vaultwarden"} |= "Username or password is incorrect"` in Loki).
- **Admin page** `/admin`, protected by the admin token (an Argon2id hash in the Secret; you type the password). Use it for users and invitations only. **Don't save settings there**: they land in `/data/config.json` and override `config.yaml` without showing up in Git.

## Set up (once)

### 1. R2 bucket, lock and token

Same as the Swarm backups ([swarm-backup.md](swarm-backup.md#1-r2-bucket-lock-and-token)), with a separate bucket and token:

1. **R2 Object Storage → Create bucket**: `theloomlabs-vaultwarden-backup`, location Automatic, Standard.
2. **Settings → Bucket lock rules**, five rules with retention **Indefinite**: prefixes `data/`, `index/`, `snapshots/`, `keys/`, `config`. Nothing for `locks/`, no rule without a prefix.
3. **Manage API tokens → Create API token**: `vaultwarden-backup`, **Object Read & Write**, only bucket `theloomlabs-vaultwarden-backup`. Note the keys and the account ID.

### 2. Secrets

`argon2` comes from Devbox (`libargon2`; run `devbox shell` once after pulling so it's installed). Values are read from the terminal and only written encrypted; the passwords are generated.

```bash
F=kubernetes/services/vaultwarden/secrets.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
ADMIN_PW=$(openssl rand -base64 30)
# Bitwarden's Argon2id parameters (64 MiB, 3 iterations, 4 lanes), as `vaultwarden hash` uses.
ADMIN_HASH=$(printf '%s' "$ADMIN_PW" | argon2 "$(openssl rand -base64 32)" -e -id -k 65540 -t 3 -p 4)
RESTIC_PW=$(openssl rand -base64 36)
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: vaultwarden
  namespace: vaultwarden
spec:
  secretTemplates:
    - name: vaultwarden-admin
      stringData:
        ADMIN_TOKEN: '$ADMIN_HASH'
    - name: vaultwarden-restic
      stringData:
        RESTIC_REPOSITORY: "s3:https://$R2_ACCOUNT.r2.cloudflarestorage.com/theloomlabs-vaultwarden-backup"
        RESTIC_PASSWORD: "$RESTIC_PW"
        AWS_ACCESS_KEY_ID: "$R2_KEY_ID"
        AWS_SECRET_ACCESS_KEY: "$R2_SECRET"
EOF
echo "Admin password (for /admin): $ADMIN_PW"
unset R2_ACCOUNT R2_KEY_ID R2_SECRET ADMIN_PW ADMIN_HASH RESTIC_PW
```

Check the file: `sops decrypt "$F" | yq '.spec.secretTemplates[].name'` lists both Secrets, and the `ADMIN_TOKEN` starts with `$argon2id$`.

**Offline, next to the age key** (paper or a second password manager, not only this vault): the restic password (`scripts/vaultwarden-restic.sh` reads it from the file, but without the age key the file is useless), your master password, and the 2FA recovery code of your account (step 4). The admin password can be reset any time by writing a new hash.

### 3. Deploy and check

Commit and push. The root app picks up `kubernetes/apps/vaultwarden.yaml`; the namespaces app creates the `vaultwarden` namespace (the app retries until it exists).

```bash
kubectl -n argocd get app vaultwarden                  # Synced, Healthy
kubectl -n vaultwarden get pods,pvc,httproute,secrets
kubectl -n vaultwarden get cnp                         # VALID True
curl -s https://vault.lab.theloomlabs.org/alive; echo  # a timestamp
make uptime                                            # monitor "Vaultwarden"
```

Watch for policy drops while it starts (enforce mode can't wait for an audit period, [network-policy.md](network-policy.md#adding-a-component-or-an-app)):

```bash
cilium hubble port-forward &
hubble observe --namespace vaultwarden --verdict DROPPED --follow
```

### 4. Your account

1. `https://vault.lab.theloomlabs.org/admin` → admin password → **Users → Invite User** → your email address.
2. `https://vault.lab.theloomlabs.org` → **Create account** with the same address and a strong master password. (Without SMTP the verification step completes in the browser.)
3. **Settings → Security → Two-step login**: Authenticator app (TOTP) or a passkey. **Write down the recovery code.**
4. Clients: Bitwarden app / extension → *Logging in on: self-hosted* → server URL `https://vault.lab.theloomlabs.org`. Off the LAN the apps work from their encrypted offline copy (read only) until Tailscale is set up (Phase 4).

### 5. First backup and check

```bash
kubectl -n vaultwarden create job --from=cronjob/vaultwarden-backup backup-first
kubectl -n vaultwarden logs -f job/backup-first --all-containers    # dump, then restic
kubectl -n vaultwarden create job --from=cronjob/vaultwarden-backup-check check-first
kubectl -n vaultwarden logs -f job/check-first                      # "no errors were found"
scripts/vaultwarden-restic.sh snapshots
```

Then try the lock: in the dashboard, delete any object under `data/`; R2 must refuse.

The `…NeverRan` alerts clear with the first scheduled runs (01:30 UTC, Sunday 05:30 UTC); a job created by hand may not count for the CronJob's status.

## Add a user

`/admin` → **Users → Invite User** → address. The person opens the vault URL and creates an account with that address (no mail arrives; tell them). Organizations (shared collections, e.g. for the family) are created by a user in the web vault; that user invites the others to the organization, and they must exist or be invited on `/admin` first.

Remove: `/admin` → user → **Delete User**. Deactivate keeps the data.

## Backups

Three independent copies of the vault:

| Copy | When (UTC) | What | Off-site | Readable with |
|---|---|---|---|---|
| Longhorn snapshots | hourly :15, 24 kept | the whole volume, crash-consistent | no (local) | the cluster |
| Longhorn backups | 02:00 daily (14), Sunday 03:30 weekly (8) | the whole volume, including the night's dump | R2 `theloomlabs-longhorn` | Longhorn key |
| Velero | 03:00, 30 days | namespace objects + a Longhorn backup of the PVC | R2 `theloomlabs-velero` | SSE-C key + Longhorn key |
| **App-level: dump** | 01:30, newest 7 in `/data/backups/` | consistent SQLite copy (`VACUUM INTO`, `/vaultwarden backup`) | with the two above | Longhorn key |
| **App-level: restic** | 01:30, after the dump | `/data` without the live database, `lost+found`, icon cache | R2 `theloomlabs-vaultwarden-backup`, bucket lock | restic password |

The restic copy needs neither the cluster nor the Longhorn key: R2 token + restic password + `restic` on any machine.

**The job** (`vaultwarden-backup`): an init container runs `/vaultwarden backup` in the Vaultwarden image (same settings from the ConfigMap), moves the file to `/data/backups/` and deletes all but the newest 7; then the `restic` container (volume read-only) backs up `/data` with `--host vaultwarden --tag vaultwarden`. It must run on the node where the Vaultwarden pod runs, because the volume is ReadWriteOnce (podAffinity). If Vaultwarden isn't running, the job waits and fails after 30 minutes. The nightly job never runs `forget` or `prune`: the bucket lock would refuse them ([Maintenance](#maintenance-quarterly)).

**The check** (`vaultwarden-backup-check`, Sunday 05:30): `restic check --read-data-subset 5%`.

| Alert | Fires when |
|---|---|
| `VaultwardenBackupFailed` | A backup or check Job failed (until the Job is removed, 20 h later) |
| `VaultwardenBackupStale` | The last successful backup is older than 26 hours (for 1 h) |
| `VaultwardenBackupNeverRan` | The backup CronJob has never succeeded (for 26 h) |
| `VaultwardenBackupCheckStale` / `…CheckNeverRan` | No passing check in 8 days |

Logs: `kubectl -n vaultwarden logs job/<job> --all-containers`, or in Loki `{namespace="vaultwarden", container=~"dump|restic"}`.

## Restore

Pick the smallest restore that fixes the problem.

| Problem | Restore |
|---|---|
| Deleted an item | Trash (30 days) in the web vault |
| Bad upgrade, broken or wrong database, lost attachments | [From a dump or from restic](#from-a-dump-or-from-restic) |
| Namespace or volume gone, cluster rebuilt | [Velero](backup.md#restore-a-namespace) (objects + volume), then check the vault |
| Longhorn or Velero backups unusable (key lost) | Fresh deploy from Git, then [from restic](#from-a-dump-or-from-restic) |

### From a dump or from restic

Stop Argo CD from scaling Vaultwarden back up, stop it, and start a helper pod with the volume. The pod has the backup label, so its network policy lets restic reach R2.

```bash
kubectl -n argocd annotate app vaultwarden argocd.argoproj.io/skip-reconcile=true
kubectl -n vaultwarden scale deploy/vaultwarden --replicas=0
kubectl -n vaultwarden wait --for=delete pod -l app.kubernetes.io/name=vaultwarden --timeout=120s
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: vaultwarden-restore
  namespace: vaultwarden
  labels:
    app.kubernetes.io/name: vaultwarden-backup
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: restic
      image: restic/restic:0.19.1
      command: [sleep, "7200"]
      envFrom:
        - secretRef:
            name: vaultwarden-restic
      env:
        - {name: AWS_DEFAULT_REGION, value: auto}
        - {name: RESTIC_CACHE_DIR, value: /tmp/cache}
        - {name: HOME, value: /tmp}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      volumeMounts:
        - {name: data, mountPath: /data}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: vaultwarden-data
    - name: tmp
      emptyDir: {}
EOF
kubectl -n vaultwarden wait --for=condition=Ready pod/vaultwarden-restore
kubectl -n vaultwarden exec -it vaultwarden-restore -- sh
```

Inside the pod, keep the current database aside first. The WAL files belong to the old database and must not stay next to the restored one.

```sh
cd /data
mkdir -p before-restore && mv db.sqlite3* before-restore/

# a) from a local dump
ls -l backups/
cp backups/db_<YYYYMMDD_HHMMSS>.sqlite3 db.sqlite3

# b) from restic (the snapshot's path is /data)
restic snapshots
restic restore <snapshot ID or latest> --target /tmp/r
ls -l /tmp/r/data/backups/
cp /tmp/r/data/backups/db_<newest>.sqlite3 /data/db.sqlite3
cp -a /tmp/r/data/attachments /tmp/r/data/rsa_key.pem /data/   # if they are lost too
exit
```

Start Vaultwarden again and check you can log in and see your items:

```bash
kubectl -n vaultwarden delete pod vaultwarden-restore
kubectl -n argocd annotate app vaultwarden argocd.argoproj.io/skip-reconcile-
kubectl -n vaultwarden scale deploy/vaultwarden --replicas=1   # Argo CD would do it too
```

When everything is fine: `kubectl -n vaultwarden exec deploy/vaultwarden -- rm -rf /data/before-restore`.

Clients keep working after a restore; items changed after the dump are gone on the server, and the apps sync to the server's state.

## Maintenance (quarterly)

The jobs never delete. Remove old snapshots from the workstation, outside 01:30–02:00 UTC and Sunday 05:30 UTC:

1. Dashboard → `theloomlabs-vaultwarden-backup` → **Settings → Bucket lock rules**: delete the five rules.
2. Forget and prune:

   ```bash
   scripts/vaultwarden-restic.sh forget --host vaultwarden \
     --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
   scripts/vaultwarden-restic.sh check
   ```

3. Add the five rules again and repeat the delete test.

Repository errors and stale locks: as for the Swarm repository ([swarm-backup.md](swarm-backup.md#repository-errors)), with `scripts/vaultwarden-restic.sh`.

## Upgrades

Renovate opens a PR for `vaultwarden/server` (server and dump use the same image) and groups `restic/restic` with the restic on the Swarm nodes. Read the release notes before merging: new Bitwarden clients often need a minimum server version, and the database migrates at start. Merge after the night's backup; the way back is the newest dump or the Longhorn snapshot.

## Rotate

- **Admin password**: new `ADMIN_PW`/`ADMIN_HASH` as in [Secrets](#2-secrets); `sops edit kubernetes/services/vaultwarden/secrets.sops.yaml` and replace `ADMIN_TOKEN`; push. The pod doesn't restart on Secret changes: `kubectl -n vaultwarden rollout restart deploy/vaultwarden`.
- **R2 token**: new token, `sops edit` the two `AWS_*` values, push, run a backup by hand, delete the old token.
- **restic password**: `restic key add` / `key remove` need the `keys/` lock rule removed for a moment; then update `RESTIC_PASSWORD` and the offline copy.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Backup pod `Pending`, event *didn't match pod affinity rules* | Vaultwarden isn't running; the job runs only next to it |
| Backup pod stuck `ContainerCreating`, *Multi-Attach error* | The job landed on another node (affinity changed?); delete the Job, check `backup.yaml` |
| restic `exit status 3`, *permission denied* | An unreadable file in `/data` (not UID 1000); exclude it or fix the owner from a helper pod |
| restic *unable to open config file* / *wrong password* | `vaultwarden-restic` Secret: repository URL, keys, password |
| restic *Fatal: unable to save … 403* | The bucket lock refused an overwrite (interrupted upload, or a prune with the rules on); the next night succeeds |
| A setting in `config.yaml` has no effect | `/data/config.json` (saved on `/admin`) overrides it: copy it aside and delete it (`kubectl -n vaultwarden exec deploy/vaultwarden -- mv /data/config.json /data/config.json.bak`), then restart the pod |
| Clients: *Cannot connect* off the LAN | Expected until Tailscale (Phase 4); the apps use their offline copy |
| No icons | On purpose (`DISABLE_ICON_DOWNLOAD`, no egress); turn icons off in the client |
