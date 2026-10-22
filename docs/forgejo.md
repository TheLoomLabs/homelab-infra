# Forgejo (Git server)

[Forgejo](https://forgejo.org) hosts Git repositories, issues, pull requests, packages (container registry included) and Actions. It runs on Kubernetes at **https://git.lab.theloomlabs.org**, with Git over SSH at **git-ssh.lab.theloomlabs.org** (port 22). Both are LAN and Tailscale only, like every lab name. Forgejo is for new and private repositories. GitHub stays the home of the public repositories, and Argo CD keeps reading this repo from GitHub. Nothing is mirrored between them. The reasons for Kubernetes, SQLite, the LTS line and a separate runner VM are in [ADR 0020](adr/0020-forgejo.md).

| What | Where |
|---|---|
| Argo CD app (project `apps`) | `kubernetes/apps/forgejo.yaml` |
| Manifests | `kubernetes/services/forgejo/` |
| Settings (`FORGEJO__section__KEY` environment) | `config.yaml` (ConfigMap `forgejo-env`) |
| Secret keys, metrics token, restic + R2 credentials | `secrets.sops.yaml` (SOPS) → Secrets `forgejo-secrets`, `forgejo-metrics`, `forgejo-restic` |
| Deployment, PVC, Services (web, SSH LoadBalancer), HTTPRoute | `forgejo.yaml` |
| Nightly SQL dump + restic, weekly check | `backup.yaml` |
| Network policies | `networkpolicy.yaml` |
| Metrics scrape, alerts | `servicemonitor.yaml`, `prometheusrule.yaml` |
| Namespace + Pod Security level | `kubernetes/platform/namespaces/namespaces.yaml` (`restricted`) |
| SSH name | `git-ssh` in `dns/records.yml` (`make dns`) |
| Uptime monitors | `forgejo`, `forgejo-ssh` in `uptime_kuma_monitors` (`make uptime`) |
| Homepage card | Apps → Forgejo (`kubernetes/services/homepage/config/services.yaml`) |
| restic from the workstation | `scripts/forgejo-restic.sh <restic args>` |
| Actions runner | VM `runner-01`, Ansible role `forgejo_runner`, `make runner` ([forgejo-runner.md](forgejo-runner.md)) |

## How it runs

- One pod (Deployment, `Recreate`). The image is `codeberg.org/forgejo/forgejo:15.x-rootless` and runs as git (UID 1000) with a read-only root filesystem. **15 is the LTS line** (supported until July 2027), and Renovate stays on it.
- **Settings only from Git.** At every start the image writes `/etc/gitea/app.ini` (an emptyDir) from its template and applies `forgejo-env` and `forgejo-secrets` on top. The admin panel doesn't change `app.ini`. Change settings in `config.yaml`.
- Data in `/var/lib/gitea` on the PVC `forgejo-data`: 20 GiB, StorageClass `longhorn-encrypted` (LUKS, 2 replicas, hourly snapshots, daily and weekly Longhorn backups to R2), `Retain`, never pruned by Argo CD.

  | Path | Content |
  |---|---|
  | `git/repositories/` | bare repositories |
  | `git/lfs/` | LFS objects |
  | `data/` | `forgejo.db` (SQLite, WAL), attachments, avatars, packages, sessions, the SSH host key (`ssh/`) |
  | `indexers/`, `queues/` | issue search index, queues (rebuildable) |
  | `backups/` | nightly SQL dumps |

- **Web** through the shared Gateway (`HTTPRoute forgejo`, port 3000), with TLS from the wildcard certificate. The `*.lab` record already points at the Gateway, so the web name needs no DNS change. Git over HTTPS, LFS and the registry (`/v2/`) use the same route.
- **SSH**: Forgejo's built-in SSH server listens on 2222. The LoadBalancer service `forgejo-ssh` publishes it on **192.168.1.62:22**, and the DNS name is `git-ssh` (not `ssh.git`, see [dns.md](dns.md)). Clone URLs look like `git@git-ssh.lab.theloomlabs.org:owner/repo.git`.
- **No egress.** The pod reaches only DNS. Mirrors, migrations from other hosts, the update check, mail, Gravatar and federation are off. A webhook to a lab service needs an egress rule in `networkpolicy.yaml`.
- **Private.** Self-registration is off and nothing is visible without login (`REQUIRE_SIGNIN_VIEW`). New repositories are private. Accounts are created with the CLI ([Add a user](#add-a-user)). OIDC comes with the SSO step.
- **No SMTP.** No mails: no password reset by mail. 2FA is TOTP or a security key/passkey (WebAuthn).
- **Metrics**: `/metrics` with a bearer token (`forgejo-metrics`), scraped by Prometheus (job `forgejo`). It exposes `gitea_*` counts (repos, users, issues) and Go/process metrics. Homepage reads the repo count from there.

## Set up (once)

### 1. R2 bucket, lock and token

Same as Vaultwarden ([vaultwarden.md](vaultwarden.md#1-r2-bucket-lock-and-token)), with its own bucket and token:

1. **R2 Object Storage → Create bucket**: `theloomlabs-forgejo-backup`, location Automatic, Standard.
2. **Settings → Bucket lock rules**: five rules with retention **Indefinite**, for the prefixes `data/`, `index/`, `snapshots/`, `keys/` and `config`. Nothing for `locks/`, and no rule without a prefix.
3. **Manage API tokens → Create API token**: `forgejo-backup`, **Object Read & Write**, only bucket `theloomlabs-forgejo-backup`. Note the keys and the account ID.

### 2. Secrets

The script reads the R2 values from the terminal and generates everything else. Nothing is written unencrypted.

```bash
F=kubernetes/services/forgejo/secrets.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
SECRET_KEY=$(openssl rand -hex 32)
INTERNAL_TOKEN=$(openssl rand -hex 48)
# JWT secrets: 32 random bytes, base64url without padding (as `forgejo generate secret JWT_SECRET`).
OAUTH2_JWT=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')
LFS_JWT=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')
METRICS_TOKEN=$(openssl rand -hex 32)
RESTIC_PW=$(openssl rand -base64 36)
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: forgejo
  namespace: forgejo
spec:
  secretTemplates:
    - name: forgejo-secrets
      stringData:
        FORGEJO__security__SECRET_KEY: "$SECRET_KEY"
        FORGEJO__security__INTERNAL_TOKEN: "$INTERNAL_TOKEN"
        FORGEJO__oauth2__JWT_SECRET: "$OAUTH2_JWT"
        FORGEJO__server__LFS_JWT_SECRET: "$LFS_JWT"
        FORGEJO__metrics__TOKEN: "$METRICS_TOKEN"
    - name: forgejo-metrics
      stringData:
        token: "$METRICS_TOKEN"
    - name: forgejo-restic
      stringData:
        RESTIC_REPOSITORY: "s3:https://$R2_ACCOUNT.r2.cloudflarestorage.com/theloomlabs-forgejo-backup"
        RESTIC_PASSWORD: "$RESTIC_PW"
        AWS_ACCESS_KEY_ID: "$R2_KEY_ID"
        AWS_SECRET_ACCESS_KEY: "$R2_SECRET"
EOF
unset R2_ACCOUNT R2_KEY_ID R2_SECRET SECRET_KEY INTERNAL_TOKEN OAUTH2_JWT LFS_JWT METRICS_TOKEN RESTIC_PW
sops decrypt "$F" | yq '.spec.secretTemplates[].name'
```

The last command lists `forgejo-secrets`, `forgejo-metrics` and `forgejo-restic`.

**`SECRET_KEY` must never change.** Forgejo encrypts 2FA secrets and other stored credentials with it. If it is lost, those can't be decrypted, and 2FA has to be reset for every account. The SOPS file keeps it (with the age key). **Keep offline**, next to the age key: the restic password and your account's 2FA recovery codes.

### 3. Deploy and check

Push. The root app picks up `kubernetes/apps/forgejo.yaml`, and the namespaces app creates the `forgejo` namespace (the app retries until it exists). Then add the SSH name to the lab DNS:

```bash
make dns
dig @192.168.1.11 +short git-ssh.lab.theloomlabs.org   # 192.168.1.62
dig @192.168.1.11 +short git.lab.theloomlabs.org       # 192.168.1.61 (wildcard)
```

Check the app:

```bash
kubectl -n argocd get app forgejo                      # Synced, Healthy
kubectl -n forgejo get pods,pvc,svc,httproute,secrets
kubectl -n forgejo get svc forgejo-ssh                 # EXTERNAL-IP 192.168.1.62
kubectl -n forgejo get cnp                             # VALID True
curl -s https://git.lab.theloomlabs.org/api/healthz; echo   # "status": "pass"
make uptime                                            # monitors "Forgejo", "Forgejo SSH"
```

Watch the policy verdicts for SSH while you test it (step 5). With audit mode on they show as `AUDIT`, and after enforcement as `DROPPED`:

```bash
cilium hubble port-forward &
sleep 5
hubble observe --namespace forgejo --verdict AUDIT --verdict DROPPED --follow
```

In Prometheus (Status → Targets), `serviceMonitor/forgejo/forgejo/0` must be UP.

### 4. Your account

Pick your own username (`admin` is reserved). The command prints a random password, and Forgejo asks for a new one at the first login.

```bash
read -rp 'Forgejo username: ' FJ_USER
read -rp 'E-mail: ' FJ_MAIL
kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create \
  --config /etc/gitea/app.ini --admin \
  --username "$FJ_USER" --email "$FJ_MAIL" --random-password
unset FJ_USER FJ_MAIL
```

1. Open `https://git.lab.theloomlabs.org` and sign in with that password. Set a new one and save it in Vaultwarden.
2. Go to **avatar → Settings → Security → Two-factor authentication → Enroll** and set up TOTP. Save the recovery codes. A security key or passkey can be added under **WebAuthn**.
3. Go to **Settings → SSH / GPG keys → Add key** and paste your public key (`cat ~/.ssh/id_ed25519.pub`).

### 5. SSH and a first repository

The first connection asks you to trust the host key. Compare the fingerprint with the one from the pod:

```bash
kubectl -n forgejo exec deploy/forgejo -- sh -c 'cat /var/lib/gitea/data/ssh/*.pub' | ssh-keygen -lf -
ssh -T git@git-ssh.lab.theloomlabs.org
# Hi there, <user>! You've successfully authenticated with the key named ..., but Forgejo does not provide shell access.
```

If the pod has no `.pub` files, trust the key on first use (`yes`); it's on your LAN.

Next, create a repository in the UI (**+ → New repository**, e.g. `test`, private) and push to it:

```bash
cd "$(mktemp -d)" && git init -b main test && cd test
echo hello > README.md && git add README.md && git commit -m init
git remote add origin git@git-ssh.lab.theloomlabs.org:"$(git config user.name)"/test.git   # adjust owner
git push -u origin main
```

Use the clone URL the UI shows if your Git user name isn't your Forgejo username.

### 6. First backup and check

```bash
kubectl -n forgejo create job --from=cronjob/forgejo-backup backup-first
kubectl -n forgejo logs -f job/backup-first --all-containers   # dump, then restic
kubectl -n forgejo create job --from=cronjob/forgejo-backup-check check-first
kubectl -n forgejo logs -f job/check-first                     # "no errors were found"
scripts/forgejo-restic.sh snapshots
```

Next, test the bucket lock: in the dashboard, delete any object under `data/`. R2 must refuse.

The `…NeverRan` alerts clear after the first scheduled runs (01:45 UTC, and Sunday 05:45 UTC for the check). A job created by hand may not count toward the CronJob's status.

## Add a user

Run step 4's command with the new person's username and e-mail. Leave out `--admin`. Give them the printed password; they set their own at the first login. To lock an account, go to **Site administration → User accounts → Edit** and use **Disable sign-in** (the data stays). **Delete** removes the account and its repositories.

## Backups

There are five copies of Forgejo's data:

| Copy | When (UTC) | What | Off-site | Readable with |
|---|---|---|---|---|
| Longhorn snapshots | hourly :15, 24 kept | the whole volume, crash-consistent | no (local) | the cluster |
| Longhorn backups | 02:00 daily (14), Sunday 03:30 weekly (8) | the whole volume, including the night's dump | R2 `theloomlabs-longhorn` | Longhorn key |
| Velero | 03:00, 30 days | namespace objects + a Longhorn backup of the PVC | R2 `theloomlabs-velero` | SSE-C key + Longhorn key |
| **App-level: dump** | 01:45, newest 7 in `backups/` | `forgejo dump` without repositories, LFS, attachments, packages, logs, indexes: `forgejo-db.sql` (consistent SQL dump of the database), `app.ini`, small files from `data/` | with the two above | Longhorn key |
| **App-level: restic** | 01:45, after the dump | `/var/lib/gitea` without the live database, indexes, queues, archives | R2 `theloomlabs-forgejo-backup`, bucket lock | restic password |

**Inside a dump:** `forgejo-db.sql` (the consistent database dump, the only one to restore from), `app.ini`, and `data/` without attachments and packages: the SSH host key (`ssh/gitea.rsa`), the OAuth2 and Actions token signing keys (`jwt/private.pem`, `actions_id_token/private.pem`), avatars. `forgejo dump` also copies the live `data/forgejo.db` with its `-wal`/`-shm` files; that copy is taken mid-write, **don't restore from it**.

The restic copy needs neither the cluster nor the Longhorn key, only the R2 token, the restic password and `restic` on any machine. Repositories are read while Forgejo runs. If someone pushes during the backup, that repository's refs can be one push behind, but its objects are never broken (Git writes objects before it moves refs).

**The job** (`forgejo-backup`) has two containers:

1. An init container in the Forgejo image writes `app.ini` exactly as the server does, runs `forgejo dump`, checks that the archive contains the SQL file, and keeps the newest 7.
2. The `restic` container (volume read-only) backs up `/var/lib/gitea` with `--host forgejo --tag forgejo`.

The job runs only on the node with the Forgejo pod (ReadWriteOnce, podAffinity). It never runs `forget` or `prune` ([Maintenance](#maintenance-quarterly)).

**The check** (`forgejo-backup-check`, Sunday 05:45) runs `restic check --read-data-subset 5%`.

| Alert | Fires when |
|---|---|
| `ForgejoBackupFailed` | A backup or check Job failed (until the Job is removed, 20 h later) |
| `ForgejoBackupStale` | The last successful backup is older than 26 hours (for 1 h) |
| `ForgejoBackupNeverRan` | The backup CronJob has never succeeded (for 26 h) |
| `ForgejoBackupCheckStale` / `…CheckNeverRan` | No passing check in 8 days |
| `ForgejoMetricsDown` | Prometheus can't scrape `/metrics` for 15 minutes (token or policy) |

To read the logs, run `kubectl -n forgejo logs job/<job> --all-containers`, or query Loki with `{namespace="forgejo", container=~"dump|restic"}`.

## Restore

Pick the smallest restore that fixes the problem.

| Problem | Restore |
|---|---|
| Deleted a branch or force-pushed over commits | `git reflog` on any clone that has them, then push again |
| Deleted a repository | Restore its directory from restic ([below](#from-a-dump-or-from-restic), case c), then **Site administration → Repositories → Unadopted repositories → Adopt** |
| Bad upgrade, broken database | [From a dump or from restic](#from-a-dump-or-from-restic), cases a/b |
| Namespace or volume gone, cluster rebuilt | [Velero](backup.md#restore-a-namespace) (objects + volume), then check Forgejo |
| Longhorn or Velero backups unusable (key lost) | Fresh deploy from Git, then everything from restic (cases b + c) |

### From a dump or from restic

First, stop Argo CD from scaling Forgejo back up, stop Forgejo, and start a helper pod with the volume. The pod has the backup label, so its network policy lets restic reach R2.

```bash
kubectl -n argocd annotate app forgejo argocd.argoproj.io/skip-reconcile=true
kubectl -n forgejo scale deploy/forgejo --replicas=0
kubectl -n forgejo wait --for=delete pod -l app.kubernetes.io/name=forgejo --timeout=120s
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: forgejo-restore
  namespace: forgejo
  labels:
    app.kubernetes.io/name: forgejo-backup
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
            name: forgejo-restic
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
        - {name: data, mountPath: /var/lib/gitea}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: forgejo-data
    - name: tmp
      emptyDir: {}
EOF
kubectl -n forgejo wait --for=condition=Ready pod/forgejo-restore
```

**The database (cases a and b).** Neither image has `sqlite3`, so you rebuild the database from the SQL dump on the workstation (Devbox has `sqlite`) and copy it in. Start by getting a dump onto the workstation:

```bash
mkdir -p /tmp/fj-restore && cd /tmp/fj-restore
# a) a local dump from the volume
kubectl -n forgejo exec forgejo-restore -- ls -l /var/lib/gitea/backups/
kubectl -n forgejo cp forgejo-restore:/var/lib/gitea/backups/forgejo-db-YYYYMMDD_HHMMSS.tar.gz dump.tar.gz
# b) or the newest dump from restic (run from the repo root)
#    scripts/forgejo-restic.sh restore latest --target /tmp/fj-restore/r --include /var/lib/gitea/backups
#    cp "$(ls -1t /tmp/fj-restore/r/var/lib/gitea/backups/*.tar.gz | head -1)" dump.tar.gz
```

Then build the database:

```bash
tar -xzf dump.tar.gz forgejo-db.sql 2>/dev/null || tar -xzf dump.tar.gz gitea-db.sql
sqlite3 forgejo.db < ./*-db.sql
sqlite3 forgejo.db 'PRAGMA integrity_check;'    # ok
```

Copy it in. Keep the current database aside, together with its WAL files, which belong to the old database:

```bash
kubectl -n forgejo exec forgejo-restore -- sh -c \
  'cd /var/lib/gitea/data && mkdir -p before-restore && mv forgejo.db* before-restore/'
kubectl -n forgejo cp forgejo.db forgejo-restore:/var/lib/gitea/data/forgejo.db
```

**Files from restic (case c: repositories, LFS, attachments, packages).** Run these inside the pod (`kubectl -n forgejo exec -it forgejo-restore -- sh`):

```sh
restic snapshots
restic restore latest --target /tmp/r --include /var/lib/gitea/git/repositories/OWNER/REPO.git
cp -a /tmp/r/var/lib/gitea/git/repositories/OWNER/REPO.git /var/lib/gitea/git/repositories/OWNER/
# Everything: restore the whole snapshot to /tmp/r and copy git/ and data/ (not data/forgejo.db) back.
exit
```

Start Forgejo again, then check that you can log in and see your repositories:

```bash
kubectl -n forgejo delete pod forgejo-restore
kubectl -n argocd annotate app forgejo argocd.argoproj.io/skip-reconcile-
kubectl -n forgejo scale deploy/forgejo --replicas=1   # Argo CD would do it too
```

After a database restore, run **Site administration → Dashboard → Resynchronize pre-receive, update and post-receive hooks of all repositories** and **Reinitialize all missing Git repositories for which records exist**. When everything works, delete the copy you kept aside: `kubectl -n forgejo exec deploy/forgejo -- rm -rf /var/lib/gitea/data/before-restore`.

## Maintenance (quarterly)

The jobs never delete. Remove old snapshots from the workstation, outside 01:45–02:15 UTC and Sunday 05:45 UTC:

1. Dashboard → `theloomlabs-forgejo-backup` → **Settings → Bucket lock rules**: delete the five rules.
2. Forget and prune:

   ```bash
   scripts/forgejo-restic.sh forget --host forgejo \
     --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
   scripts/forgejo-restic.sh check
   ```

3. Add the five rules again and repeat the delete test.

To clean up Forgejo itself, go to **Site administration → Dashboard** and run *Delete all repositories' archives*, *Garbage collect all repositories* and *Delete old Actions artifacts*. Most of these also run as built-in cron tasks.

## Upgrades

Renovate opens PRs for `codeberg.org/forgejo/forgejo` within 15.x (the server and the dump job use the same image). Read the release notes, and merge after the night's backup. The database migrates at start, and the startup probe allows 5 minutes for it. To go back, use the newest dump or the Longhorn snapshot. **Moving to the next LTS** is a manual PR: change the tag and the Renovate rule, read every release's breaking changes in between, and upgrade the runner first if its notes say so.

## Rotate

- **Metrics token**: `sops edit` the file and set the same new value in `FORGEJO__metrics__TOKEN` and `forgejo-metrics.token`. Push, then `kubectl -n forgejo rollout restart deploy/forgejo`.
- **`INTERNAL_TOKEN`, JWT secrets**: same procedure. A new `oauth2` JWT secret logs out OAuth clients, and a new LFS one invalidates LFS tokens in flight. **Never rotate `SECRET_KEY`** ([Secrets](#2-secrets)).
- **R2 token**: create a new token, `sops edit` the two `AWS_*` values, push, run a backup by hand, then delete the old token.
- **restic password**: `restic key add` / `key remove` need the `keys/` lock rule removed for a moment. Then update `RESTIC_PASSWORD` and the offline copy.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pod `CrashLoopBackOff`, log *is not writable* | Volume ownership: the PVC must be writable by UID 1000 (fsGroup). Check `kubectl -n forgejo exec deploy/forgejo -- ls -ld /var/lib/gitea` |
| A setting in `config.yaml` has no effect | Key spelling: `FORGEJO__<section>__<KEY>`, four underscores for top-level keys, `_0X2E_` for a dot in a section name. The effective file: `kubectl -n forgejo exec deploy/forgejo -- cat /etc/gitea/app.ini` |
| `ssh: connect to host git-ssh… port 22: Connection refused / timed out` | `kubectl -n forgejo get svc forgejo-ssh` (EXTERNAL-IP .62?), L2 lease `kubectl -n kube-system get lease \| grep forgejo`, policy drops in Hubble |
| SSH *Permission denied (publickey)* | Key not added to your account, or the wrong key offered (`ssh -vT …`) |
| Clone URL shows `localhost` or the wrong port | `SSH_DOMAIN`/`SSH_PORT`/`ROOT_URL` in `config.yaml` |
| Large HTTPS clone or push cut off | Gateway (Envoy) timeout. Use SSH, or add `timeouts` to the HTTPRoute |
| Backup pod `Pending`, event *didn't match pod affinity rules* | Forgejo isn't running; the job runs only next to it |
| Dump *no SQL dump* | `forgejo dump` changed its file name; check `tar -tzf` on a dump and update `backup.yaml` |
| restic *unable to open config file* / *wrong password* | `forgejo-restic` Secret: repository URL, keys, password |
| restic *Fatal: unable to save … 403* | The bucket lock refused an overwrite (interrupted upload, or a prune with the rules on); the next night succeeds |
| `ForgejoMetricsDown` | 401: the two token values differ. Timeout: network policy |
