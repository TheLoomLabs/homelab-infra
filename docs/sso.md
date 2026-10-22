# Single sign-on (Authentik)

[Authentik](https://goauthentik.io) is the lab's identity provider. It runs on Kubernetes at **https://auth.lab.theloomlabs.org**, LAN and Tailscale only like every lab name. Apps sign in with OpenID Connect (OIDC) against it, and UIs without a login of their own get one through Authentik's proxy. Every app keeps a local admin as break-glass for when Authentik or Kubernetes is down. The reasons for Authentik, the chart and a plain PostgreSQL are in [ADR 0021](adr/0021-authentik-sso.md).

| What | Where |
|---|---|
| Argo CD app (project `apps`): chart `authentik` + manifests | `kubernetes/apps/authentik.yaml` |
| Chart values (server, worker, settings as `AUTHENTIK_*` env) | `kubernetes/services/authentik/values.yaml` |
| Secret key, database password, bootstrap admin, restic + R2 credentials | `secrets.sops.yaml` (SOPS) → Secrets `authentik-env`, `authentik-postgresql`, `authentik-restic` |
| PostgreSQL (StatefulSet, PVC, Service) | `postgresql.yaml` |
| Authentik's config: groups, login flow, apps (blueprints) | `blueprints.yaml` (ConfigMap `authentik-blueprints`) |
| Apps' OIDC client secrets (Authentik side) | `oidc.sops.yaml` → Secret `authentik-oidc` (worker env) |
| Route `auth.lab`; route `authentik-proxy` (longhorn / prometheus / alertmanager.lab) | `httproute.yaml` |
| Nightly pg_dump + restic, weekly check | `backup.yaml` |
| Network policies | `networkpolicy.yaml` |
| Alerts (scrape, backup) | `prometheusrule.yaml`; ServiceMonitors from the chart |
| Namespace + Pod Security level | `kubernetes/platform/namespaces/namespaces.yaml` (`restricted`) |
| Uptime monitor | `authentik` in `uptime_kuma_monitors` (`make uptime`) |
| Homepage card | Apps → Authentik, Status → Backups |
| Proxied UIs: providers, apps, embedded outpost | blueprint `lab-proxy.yaml` ([Proxied UIs](#proxied-uis)) |
| restic from the workstation | `scripts/authentik-restic.sh <restic args>` |

## How it runs

- **Server** (`authentik-server`): web UI, the OIDC endpoints and the embedded outpost (the proxy for UIs without a login). **Worker** (`authentik-worker`): background tasks and the blueprints. One replica each, image `ghcr.io/goauthentik/server` at the chart's version. Non-root (UID 1000), read-only root filesystem; `/data` (uploaded files) is an emptyDir, so upload nothing in the UI: icons are URLs in the blueprints.
- **Settings only from Git**: `AUTHENTIK_*` variables in `values.yaml` and the Secret `authentik-env`. Since 2025.10 Authentik needs only PostgreSQL (no Redis).
- **PostgreSQL 18** (`postgres:18.x-trixie`), one pod, database and user `authentik`, data on the PVC `postgresql-data`: 5 GiB, `longhorn-encrypted` (LUKS, 2 replicas, hourly snapshots, daily and weekly Longhorn backups to R2), `Retain`, never pruned by Argo CD. The chart's bundled Bitnami PostgreSQL is off.
- **Config as blueprints**: groups and the login flow are YAML in `blueprints.yaml`. The worker applies them at start and whenever the ConfigMap changes, and puts back attributes changed in the UI. Users are not in Git (no usernames in the public repo): you create them in the UI.
- **Groups**: `lab-admins` (Authentik superuser, admin in every app) and `lab-users` (signed in, no admin rights). The bootstrap admin `akadmin` is in `authentik Admins`.
- **Login**: username or e-mail, password, then a second factor: a passkey/security key (WebAuthn) or an authenticator app (TOTP). An account without one has to set one up at its next login (blueprint `lab-mfa.yaml`). Recovery codes can be added in the user settings.
- **No egress** to the outside. Server and worker reach PostgreSQL and DNS: update check, analytics and error reporting are off, there is no SMTP (no password reset by mail). Apps don't need to reach the pods: browsers follow the redirects through the Gateway. The one exception is the server's embedded outpost, which forwards to the [proxied UIs](#proxied-uis) inside the cluster.
- **Metrics**: server and worker on port 9300, scraped by Prometheus through the chart's ServiceMonitors.

## Set up (once)

### 1. R2 bucket, lock and token

Same as Vaultwarden ([vaultwarden.md](vaultwarden.md#1-r2-bucket-lock-and-token)), with its own bucket and token:

1. **R2 Object Storage → Create bucket**: `theloomlabs-authentik-backup`, location Automatic, Standard.
2. **Settings → Bucket lock rules**: five rules with retention **Indefinite**, for the prefixes `data/`, `index/`, `snapshots/`, `keys/` and `config`. Nothing for `locks/`, and no rule without a prefix.
3. **Manage API tokens → Create API token**: `authentik-backup`, **Object Read & Write**, only bucket `theloomlabs-authentik-backup`. Note the keys and the account ID.

### 2. Secrets

The script reads the R2 values from the terminal and generates everything else. It prints the bootstrap admin's password once: put it into Vaultwarden. Nothing is written unencrypted.

```bash
F=kubernetes/services/authentik/secrets.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
SECRET_KEY=$(openssl rand -base64 60 | tr -d '\n')
PG_PW=$(openssl rand -hex 32)
BOOT_PW=$(openssl rand -base64 24 | tr -d '\n')
RESTIC_PW=$(openssl rand -base64 36)
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: authentik
  namespace: authentik
spec:
  secretTemplates:
    - name: authentik-env
      stringData:
        AUTHENTIK_SECRET_KEY: "$SECRET_KEY"
        AUTHENTIK_POSTGRESQL__PASSWORD: "$PG_PW"
        AUTHENTIK_BOOTSTRAP_PASSWORD: "$BOOT_PW"
        AUTHENTIK_BOOTSTRAP_EMAIL: "akadmin@auth.lab.theloomlabs.org"
    - name: authentik-postgresql
      stringData:
        password: "$PG_PW"
    - name: authentik-restic
      stringData:
        RESTIC_REPOSITORY: "s3:https://$R2_ACCOUNT.r2.cloudflarestorage.com/theloomlabs-authentik-backup"
        RESTIC_PASSWORD: "$RESTIC_PW"
        AWS_ACCESS_KEY_ID: "$R2_KEY_ID"
        AWS_SECRET_ACCESS_KEY: "$R2_SECRET"
EOF
echo "akadmin password (into Vaultwarden): $BOOT_PW"
unset R2_ACCOUNT R2_KEY_ID R2_SECRET SECRET_KEY PG_PW BOOT_PW RESTIC_PW
sops decrypt "$F" | yq '.spec.secretTemplates[].name'
```

The last command lists `authentik-env`, `authentik-postgresql` and `authentik-restic`.

- **`AUTHENTIK_SECRET_KEY` must never change.** Authentik signs sessions and derives user IDs from it (the `sub` claim apps see): a new key logs everyone out and makes apps see new users. The SOPS file keeps it (with the age key).
- The bootstrap values only matter at the first start: they create `akadmin` if it doesn't exist. Changing them later changes nothing.
- The database password is set when PostgreSQL first creates its data directory. To change it later, see [Rotate](#rotate).
- **Keep offline**, next to the age key: the restic password, and the recovery codes of your own account (step 5).

### 3. Deploy and check

Push. The root app picks up `kubernetes/apps/authentik.yaml`, and the namespaces app creates the `authentik` namespace (the app retries until it exists). `auth.lab` is covered by the `*.lab` wildcard, so DNS needs nothing. The first start runs the database migrations and the default blueprints, which takes a few minutes.

```bash
kubectl -n argocd get app authentik                     # Synced, Healthy
kubectl -n authentik get pods,pvc,svc,httproute,secrets
kubectl -n authentik get cnp                            # VALID True
kubectl -n authentik logs deploy/authentik-worker | grep -i blueprint | tail
curl -s -o /dev/null -w '%{http_code}\n' https://auth.lab.theloomlabs.org/-/health/live/   # 200
make uptime                                             # monitor "Authentik"
```

Watch for dropped flows while it starts:

```bash
cilium hubble port-forward &
sleep 5
hubble observe --namespace authentik --verdict DROPPED --follow
```

In Prometheus (Status → Targets), `serviceMonitor/authentik/authentik-server/0` and `serviceMonitor/authentik/authentik-worker/0` must be UP.

### 4. First login (akadmin)

1. Open https://auth.lab.theloomlabs.org, log in as `akadmin` with the password from step 2.
2. Authentik asks you to set up a second factor (blueprint `lab-mfa.yaml`). Choose **WebAuthn device** and register a passkey or security key, or **TOTP device** and scan the code with your authenticator app (and keep the TOTP secret in Vaultwarden next to the password).
3. Top right: **Admin interface**. **Customization → Blueprints**: the two `Lab - …` blueprints show status **successful**.
4. **Directory → Groups**: `lab-admins` and `lab-users` exist.

### 5. Your account

`akadmin` stays the break-glass account. For daily use, create your own:

1. **Directory → Users → Create**: your username, name and e-mail (any address: there is no mail). **Create**.
2. Open the user → **Set password**: a new password from Vaultwarden → **Update password**.
3. Tab **Groups → Add to existing group** → `lab-admins` → **Add**.
4. Log out (top right → **Sign out**), log in with your new account, set up a passkey when asked.
5. Top right → **Settings → MFA Devices → Enroll → Static tokens**: write the codes down and keep them offline. A second passkey (another device) is a good idea too.

### 6. System settings (UI, once)

Admin interface → **System → Settings** (not covered by blueprints):

1. **Avatars**: `initials` (the default also asks Gravatar, from your browser) → **Save**.
2. **Default token duration**, **Event retention**: defaults are fine.

### 7. First backup and check

```bash
kubectl -n authentik create job --from=cronjob/authentik-backup backup-first
kubectl -n authentik logs -f job/backup-first --all-containers   # dump, then restic
kubectl -n authentik create job --from=cronjob/authentik-backup-check check-first
kubectl -n authentik logs -f job/check-first                     # "no errors were found"
scripts/authentik-restic.sh snapshots
```

Next, test the bucket lock: in the dashboard, delete any object under `data/`. R2 must refuse.

The `…NeverRan` alerts clear after the first scheduled runs (01:15 UTC, and Sunday 05:15 UTC for the check).

## Apps

Each app is an OAuth2/OIDC provider + application in a blueprint (`lab-app-<slug>.yaml`), with its client ID in Git and its client secret in two SOPS files: Authentik's (`oidc.sops.yaml` → Secret `authentik-oidc`, read by the worker through `!Env`) and the app's. Access is by group: an application lets in only the groups bound to it. Every app keeps its own login as break-glass.

| App | Login | Admin from | Others allowed | Break-glass |
|---|---|---|---|---|
| Argo CD (`argocd`) | **Log in via Authentik**; CLI `argocd login argocd.lab.theloomlabs.org --sso --grpc-web` | `lab-admins` → `role:admin` (`policy.csv`) | nobody | `kubectl` (cluster admin); the local `admin` is off (`admin.enabled: "false"`) |
| Grafana (`grafana`) | **Sign in with Authentik** | `lab-admins` → Admin (`role_attribute_path`) | `lab-users` → Viewer | the admin form (`grafana-admin`) |
| Forgejo (`forgejo`) | **Sign in with authentik** (login source added with the CLI); first time: link to the existing account | `lab-admins` → site admin (`--admin-group`) | `lab-users` → users (accounts are linked, never auto-created) | local password (+2FA) and the CLI |
| Homepage (`homepage`) | **Sign in with Authentik**, the only login | no roles | `lab-users` | none: change the env back to a password if Authentik is down for long |
| Portainer (`portainer`, Swarm) | **Login with OAuth** (settings in its UI) | promoted once by hand (CE has no group mapping) | nobody | **Use internal authentication** (admin from SOPS) |
| Proxmox (`proxmox`) | Realm **Authentik SSO** (`pveum`) | `lab-admins-authentik` → Administrator on `/` | nobody | `root@pam` |

Issuer and discovery for an app: `https://auth.lab.theloomlabs.org/application/o/<slug>/.well-known/openid-configuration`.

### OIDC secrets: Argo CD and Grafana

One random client secret per app, written to Authentik's SOPS file and to the app's. Run from the repo root:

```bash
ARGOCD=$(openssl rand -hex 32)
GRAFANA=$(openssl rand -hex 32)
F=kubernetes/services/authentik/oidc.sops.yaml
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: authentik-oidc
  namespace: authentik
spec:
  secretTemplates:
    - name: authentik-oidc
      stringData:
        OIDC_ARGOCD_CLIENT_SECRET: "$ARGOCD"
        OIDC_GRAFANA_CLIENT_SECRET: "$GRAFANA"
EOF
F=kubernetes/platform/argocd/oidc.sops.yaml
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: argocd-oidc
  namespace: argocd
spec:
  secretTemplates:
    - name: argocd-oidc
      labels:
        app.kubernetes.io/part-of: argocd
      stringData:
        clientSecret: "$ARGOCD"
EOF
F=kubernetes/platform/monitoring/grafana-oidc.sops.yaml
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: grafana-oidc
  namespace: monitoring
spec:
  secretTemplates:
    - name: grafana-oidc
      stringData:
        GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET: "$GRAFANA"
EOF
unset ARGOCD GRAFANA F
for f in kubernetes/services/authentik/oidc.sops.yaml kubernetes/platform/argocd/oidc.sops.yaml \
  kubernetes/platform/monitoring/grafana-oidc.sops.yaml; do
  sops decrypt "$f" | yq '.spec.secretTemplates[].stringData | keys'
done
```

The loop lists the two `OIDC_*` keys, then `clientSecret`, then `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`.

### Check a login

1. Authentik, **Customization → Blueprints**: `Lab - App Argo CD` and `Lab - App Grafana` **successful**. **Applications → Applications**: both listed, each with its provider.
2. Discovery answers: `curl -s https://auth.lab.theloomlabs.org/application/o/argocd/.well-known/openid-configuration | jq .issuer`.
3. In a private window: https://argocd.lab.theloomlabs.org → **Log in via Authentik** → Authentik login (password, passkey) → back in Argo CD with every app visible. **User Info** shows your username and the group `lab-admins`.
4. https://grafana.lab.theloomlabs.org → **Sign in with Authentik** → back in Grafana; **Profile** shows role Admin.
5. Nothing dropped meanwhile: `hubble observe --verdict DROPPED --since 10m`.

### Forgejo, Homepage, Portainer, Proxmox

Four more client secrets. Homepage reads its own from its SOPS file; Forgejo, Portainer and Proxmox store theirs in their own configuration (set below from Authentik's SOPS file), so only Authentik's file holds those. The script also removes Homepage's password: with OIDC configured, Homepage has no password login. Run from the repo root:

```bash
F=kubernetes/services/authentik/oidc.sops.yaml
for app in FORGEJO HOMEPAGE PORTAINER PROXMOX; do
  sops set "$F" "[\"spec\"][\"secretTemplates\"][0][\"stringData\"][\"OIDC_${app}_CLIENT_SECRET\"]" \
    "\"$(openssl rand -hex 32)\""
done
H=kubernetes/services/homepage/secrets.sops.yaml
sops decrypt "$H" | yq '.spec.secretTemplates[0].name'      # homepage-env
sops set "$H" '["spec"]["secretTemplates"][0]["stringData"]["HOMEPAGE_OIDC_CLIENT_SECRET"]' \
  "\"$(sops decrypt "$F" | yq -r '.spec.secretTemplates[0].stringData.OIDC_HOMEPAGE_CLIENT_SECRET')\""
sops unset "$H" '["spec"]["secretTemplates"][0]["stringData"]["HOMEPAGE_AUTH_PASSWORD"]'
unset F H
sops decrypt kubernetes/services/authentik/oidc.sops.yaml | yq '.spec.secretTemplates[0].stringData | keys'
sops decrypt kubernetes/services/homepage/secrets.sops.yaml | yq '.spec.secretTemplates[0].stringData | keys'
```

The first listing has six `OIDC_*` keys. The second has `HOMEPAGE_OIDC_CLIENT_SECRET` and no `HOMEPAGE_AUTH_PASSWORD`. Delete the Homepage password from Vaultwarden once the OIDC login works.

After the push, restart the worker so it reads the four new keys, then check the blueprints (**Customization → Blueprints**: `Lab - App Forgejo`, `Homepage`, `Portainer`, `Proxmox` **successful**):

```bash
kubectl -n authentik get secret authentik-oidc -o json | jq '.data | keys'   # six keys
kubectl -n authentik rollout restart deploy/authentik-worker
kubectl -n authentik rollout status deploy/authentik-worker
```

A small helper prints one client secret for the commands below (it stays in your terminal):

```bash
oidc_secret() { sops decrypt kubernetes/services/authentik/oidc.sops.yaml | yq -r ".spec.secretTemplates[0].stringData.OIDC_$1_CLIENT_SECRET"; }
```

#### Forgejo

The login source is stored in Forgejo's database (not in `config.yaml`), so it is added once with the CLI. `--admin-group` makes members of `lab-admins` site admins at every login; `--skip-local-2fa` because Authentik already asked for a second factor.

```bash
kubectl -n forgejo exec deploy/forgejo -- forgejo admin auth add-oauth \
  --name authentik \
  --provider openidConnect \
  --key forgejo \
  --secret "$(oidc_secret FORGEJO)" \
  --auto-discover-url https://auth.lab.theloomlabs.org/application/o/forgejo/.well-known/openid-configuration \
  --scopes email --scopes profile \
  --group-claim-name groups \
  --admin-group lab-admins \
  --skip-local-2fa \
  --icon-url https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/authentik.svg
kubectl -n forgejo exec deploy/forgejo -- forgejo admin auth list
kubectl -n forgejo rollout restart deploy/forgejo       # loads the new source
kubectl -n forgejo rollout status deploy/forgejo
```

The name `authentik` is part of the callback URL (`/user/oauth2/authentik/callback`), so keep it. Then link your account: in a private window, https://git.lab.theloomlabs.org → **Sign in with authentik** → Authentik login → Forgejo's **Link account** page → sign in with your Forgejo username and password (and Forgejo 2FA, this once). From then on the Authentik button is enough. **Settings → Security → Manage linked accounts** shows the link. SSH keys and access tokens don't change.

To change the source later: `forgejo admin auth list` for its ID, then `forgejo admin auth update-oauth --id <ID> …` with the flags to change.

#### Homepage

Nothing to do after the push: the pod restarts with the OIDC settings. https://home.lab.theloomlabs.org shows **Sign in with Authentik**, and there is no password field any more.

#### Portainer (UI)

Portainer CE keeps OAuth settings in its database. As the local admin (break-glass, stays): **Settings → Authentication**:

1. **Authentication method**: **OAuth**.
2. **Automatic user provisioning**: on. (CE can't map groups to teams or roles: new SSO users are standard users.)
3. **Provider**: **Custom**, then:

   | Field | Value |
   |---|---|
   | Client ID | `portainer` |
   | Client secret | output of `oidc_secret PORTAINER` |
   | Authorization URL | `https://auth.lab.theloomlabs.org/application/o/authorize/` |
   | Access token URL | `https://auth.lab.theloomlabs.org/application/o/token/` |
   | Resource URL | `https://auth.lab.theloomlabs.org/application/o/userinfo/` |
   | Redirect URL | `https://portainer.lab.theloomlabs.org/` (with the trailing slash, as in the blueprint) |
   | Logout URL | `https://auth.lab.theloomlabs.org/application/o/portainer/end-session/` |
   | User identifier | `preferred_username` |
   | Scopes | `openid email profile` |
   | Auth Style | **In Params** |

4. **Save settings**.
5. In a private window: https://portainer.lab.theloomlabs.org → **Login with OAuth** → Authentik → back in Portainer, as a user without rights.
6. Back as the local admin (the login page keeps **Use internal authentication**): **Administration → User-related → Users** → your SSO user → **Administrator** on → **Update user**. Log in with Authentik again: full rights.

#### Proxmox (CLI on pve)

An OpenID realm `authentik`. Users are created at their first login (`<username>@authentik`), their Authentik groups become Proxmox groups `<group>-authentik`, and `lab-admins-authentik` gets **Administrator** on `/`. `root@pam` stays the break-glass login. First check that pve resolves and trusts Authentik:

```bash
ssh root@pve.lab.theloomlabs.org \
  'curl -s https://auth.lab.theloomlabs.org/application/o/proxmox/.well-known/openid-configuration | head -c 120; echo'
```

Then add the realm and the permission. The secret goes over SSH on the command line once; Proxmox stores it in `/etc/pve/priv/`.

```bash
ssh root@pve.lab.theloomlabs.org pveum realm add authentik --type openid \
  --issuer-url https://auth.lab.theloomlabs.org/application/o/proxmox/ \
  --client-id proxmox \
  --client-key "$(oidc_secret PROXMOX)" \
  --username-claim preferred_username \
  --scopes "'openid email profile'" \
  --autocreate 1 \
  --groups-claim groups --groups-autocreate 1 --groups-overwrite 1 \
  --comment "'Authentik SSO'"
ssh root@pve.lab.theloomlabs.org pveum group add lab-admins-authentik --comment "'Authentik lab-admins'"
ssh root@pve.lab.theloomlabs.org pveum acl modify / --groups lab-admins-authentik --roles Administrator
ssh root@pve.lab.theloomlabs.org pveum realm list
```

Log in at https://pve.lab.theloomlabs.org:8006 with **Realm: Authentik SSO** → **Login (OpenID redirect)** → Authentik → back in Proxmox with full rights. **Datacenter → Permissions → Users** lists `<you>@authentik`, in group `lab-admins-authentik`. Proxmox's own TOTP is not needed for this realm (Authentik asked).

Unset the helper when done: `unset -f oidc_secret`.

### Add an app

1. A blueprint `lab-app-<slug>.yaml` in `blueprints.yaml` (copy Grafana's): provider name and `client_id` = slug, `client_secret: !Env OIDC_<SLUG>_CLIENT_SECRET`, `grant_types` (an empty list allows no login), the app's redirect URIs, the groups it lets in.
2. A new random secret (`openssl rand -hex 32`): `sops edit kubernetes/services/authentik/oidc.sops.yaml` (add the key) and the same value in the app's own SopsSecret.
3. The app's OIDC settings, and egress from the app to `auth.lab` (copy Grafana's two rules in `kubernetes/platform/monitoring/networkpolicy.yaml`).
4. Push. The worker reads its environment only at start: `kubectl -n authentik rollout restart deploy/authentik-worker` once the Secret has the new key.

## Proxied UIs

Longhorn, Prometheus and Alertmanager have no login of their own. They are published behind Authentik's **embedded outpost** (part of the server pod, proxy mode): no extra deployment, and the UIs themselves are unchanged.

| UI | URL | Forwards to |
|---|---|---|
| Longhorn | https://longhorn.lab.theloomlabs.org | `longhorn-frontend.longhorn-system` :80 (pod 8000) |
| Prometheus | https://prometheus.lab.theloomlabs.org | `kube-prometheus-stack-prometheus.monitoring` :9090 |
| Alertmanager | https://alertmanager.lab.theloomlabs.org | `kube-prometheus-stack-alertmanager.monitoring` :9093 |

- **Path:** browser → Gateway (route `authentik-proxy`, three names, wildcard certificate, no DNS change) → `authentik-server` → the outpost checks the Authentik session (login and MFA as usual if there is none) → the UI. A request without a session is redirected to `auth.lab` and back to `<name>/outpost.goauthentik.io/callback`.
- **Who:** `lab-admins` only. Every one of these UIs can change things (delete a volume, silence an alert).
- **Config:** blueprint `lab-proxy.yaml`: a proxy provider + application per UI, and the embedded outpost (its provider list and `authentik_host`). Add a fourth UI there, not in a new blueprint: the outpost entry replaces the whole list.
- **Network:** the server has egress to the three pods; each pod lets in only the server (`longhorn-ui` in `kubernetes/platform/longhorn/networkpolicy.yaml`, the first Cilium policy in longhorn-system; `prometheus` and `alertmanager` in the monitoring policies). Before this, any pod could open the Longhorn UI.
- **Session:** 8 hours (`access_token_validity`), then a new Authentik login (usually silent while the Authentik session lasts). Sign out of one UI: `https://<name>/outpost.goauthentik.io/sign_out`.
- **Links:** Prometheus and Alertmanager have `externalUrl` set to their names, so alert source links and silence links in ntfy messages open through the proxy. Homepage cards link here.
- **Break-glass:** the port-forwards still work (`kubectl` bypasses the Gateway and the policies apply to pods, not to the API server's tunnel): [storage.md](storage.md#longhorn-ui), [monitoring.md](monitoring.md#check).

### Check the proxied UIs

1. **Customization → Blueprints**: `Lab - Proxied UIs` **successful**. **Applications → Outposts**: `authentik Embedded Outpost` lists Longhorn, Prometheus and Alertmanager, health green (last seen within a minute).
2. Route accepted: `kubectl -n authentik get httproute authentik-proxy -o jsonpath='{.status.parents[0].conditions[*].reason}'` → `Accepted ResolvedRefs`.
3. Without a session, each name answers with the outpost's start page (a HEAD request, `curl -I`, gets Authentik's own `/flows/…` instead; use GET):

   ```bash
   for h in longhorn prometheus alertmanager; do
     curl -s -o /dev/null -w "$h %{http_code} %{redirect_url}\n" https://$h.lab.theloomlabs.org/
   done
   # each: 302 https://<name>.lab.theloomlabs.org/outpost.goauthentik.io/start?rd=…
   ```
4. In a private window: https://longhorn.lab.theloomlabs.org → Authentik login → the Longhorn dashboard with volumes and nodes (live updates over websocket). Then https://prometheus.lab.theloomlabs.org/targets and https://alertmanager.lab.theloomlabs.org open without a second login.
5. A `lab-users` account gets *Permission denied* from Authentik, not the UI.
6. The UI is closed to other pods. Prometheus may reach the whole cluster, so it is a good test:

   ```bash
   POD=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name)
   kubectl -n monitoring exec $POD -c prometheus -- wget -q -T 5 -O /dev/null http://longhorn-frontend.longhorn-system
   # wget: download timed out
   ```

7. Nothing else dropped (Hubble needs its port-forward first):

   ```bash
   cilium hubble port-forward &
   sleep 3
   hubble observe --verdict DROPPED --since 10m
   kill %1
   ```

## Change the config (blueprints)

Edit `blueprints.yaml`, push. The kubelet updates the mounted files within a minute or two and the worker applies the changed blueprint. Check **Customization → Blueprints** (status, last applied) or the worker log. To apply one right away: **Customization → Blueprints → ⋮ → Apply**.

- Entries are matched by their `identifiers`; the `attrs` listed are set on every apply. Removing an entry doesn't delete the object: delete it in the UI too.
- Objects you create in the UI that aren't in a blueprint stay (users always do).
- The schema for editors: `https://auth.lab.theloomlabs.org/blueprints/schema.json` (or `blueprints/schema.json` in the Authentik repo at the deployed version).

## Break-glass

| Problem | Way in |
|---|---|
| Lost your passkey, recovery codes at hand | Log in with a recovery code instead of the second factor, then enroll a new device |
| Locked out of your account | Log in as `akadmin` (Vaultwarden), fix your user (**Directory → Users → your user → MFA devices**) |
| Locked out of every account | A recovery link (skips password and MFA): `kubectl -n authentik exec deploy/authentik-worker -- ak create_recovery_key 30 akadmin` prints a link (path), open it under `https://auth.lab.theloomlabs.org`. Valid for 30 minutes (the number is minutes) or until used |
| Authentik or Kubernetes down | Each app's own login (Grafana's admin form, Forgejo's CLI); Argo CD through `kubectl` |

## Backups

Three independent copies of the database:

| Copy | When | Where | Needs |
|---|---|---|---|
| Longhorn snapshot / backup of `postgresql-data` (crash-consistent) | hourly / daily 02:00 UTC | cluster / R2 | Longhorn key |
| Velero (objects + volume) | 03:00 UTC | R2 | SSE-C key |
| `pg_dump` + restic | 01:15 UTC | R2 `theloomlabs-authentik-backup` | restic password |

The dump is the clean one: a consistent, compressed `pg_dump --format=custom`, always at `/backup/authentik.dump` in the snapshot. Blueprints and settings in Git rebuild the config, not the users, their passkeys and sessions: those are only in the database. The Secret key (SOPS) is needed with any restore.

## Restore

| Problem | Restore |
|---|---|
| Wrong change in the UI | Blueprint objects: the next apply puts them back. Others: fix by hand |
| Bad upgrade, broken database | [From the dump](#from-the-dump) |
| Namespace or volume gone, cluster rebuilt | [Velero](backup.md#restore-a-namespace) (objects + volume), then check the login |
| Longhorn or Velero backups unusable (key lost) | Fresh deploy from Git, then [from the dump](#from-the-dump) |

### From the dump

Stop Argo CD from scaling Authentik back up, then stop server and worker (PostgreSQL keeps running):

```bash
kubectl -n argocd annotate app authentik argocd.argoproj.io/skip-reconcile=true
kubectl -n authentik scale deploy/authentik-server deploy/authentik-worker --replicas=0
kubectl -n authentik wait --for=delete pod -l app.kubernetes.io/name=authentik --timeout=120s
```

Get the dump onto the workstation and into the database pod (run from the repo root):

```bash
scripts/authentik-restic.sh snapshots
scripts/authentik-restic.sh restore latest --target /tmp/ak-restore
kubectl -n authentik cp /tmp/ak-restore/backup/authentik.dump postgresql-0:/tmp/authentik.dump
```

Replace the database. `authentik` is PostgreSQL's superuser here (the image creates `POSTGRES_USER` as one), and the commands use the local socket:

```bash
kubectl -n authentik exec postgresql-0 -- psql -U authentik -d postgres \
  -c 'DROP DATABASE authentik WITH (FORCE);' \
  -c 'CREATE DATABASE authentik OWNER authentik;'
kubectl -n authentik exec postgresql-0 -- pg_restore -U authentik -d authentik \
  --no-owner --exit-on-error /tmp/authentik.dump
kubectl -n authentik exec postgresql-0 -- rm /tmp/authentik.dump
rm -rf /tmp/ak-restore
```

Start Authentik again and log in:

```bash
kubectl -n argocd annotate app authentik argocd.argoproj.io/skip-reconcile-
kubectl -n authentik scale deploy/authentik-server deploy/authentik-worker --replicas=1   # Argo CD would do it too
```

## Maintenance (quarterly)

The jobs never delete. Remove old snapshots from the workstation, outside 01:15–01:45 UTC and Sunday 05:15 UTC:

1. Dashboard → `theloomlabs-authentik-backup` → **Settings → Bucket lock rules**: delete the five rules.
2. Forget and prune:

   ```bash
   scripts/authentik-restic.sh forget --host authentik \
     --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
   scripts/authentik-restic.sh check
   ```

3. Add the five rules again and repeat the delete test.

Also look at **Events → Logs** for failed logins and **System → System Tasks** for failed tasks.

## Upgrades

- **Authentik**: Renovate opens PRs for the chart `authentik` (the image follows its version). Read the [release notes](https://docs.goauthentik.io/releases/) of every version in between, go one minor release (`YYYY.M`) at a time, and merge after the night's backup. The database migrates at start (the startup probe allows 10 minutes). Afterwards log in to Authentik and to an app. To go back: the dump from before the upgrade.
- **PostgreSQL minor** (18.x): Renovate PRs, nothing to migrate; the pod restarts and Authentik reconnects.
- **PostgreSQL major** (19): a manual PR. Take a dump (create a backup job), scale Authentik to 0, delete the StatefulSet and the PVC's data (or use a new PVC), deploy the new major, restore [from the dump](#from-the-dump). Change the image in `postgresql.yaml` and `backup.yaml` and the Renovate rule together.

## Rotate

- **Database password**: `sops edit` the file, set the same new value in `AUTHENTIK_POSTGRESQL__PASSWORD` and `authentik-postgresql.password`. Push, wait for the Secrets (`kubectl -n authentik get secret authentik-postgresql -o jsonpath='{.metadata.resourceVersion}'` changes), then set it in the database and restart Authentik:

  ```bash
  kubectl -n authentik exec -it postgresql-0 -- psql -U authentik -d postgres -c '\password authentik'
  kubectl -n authentik rollout restart deploy/authentik-server deploy/authentik-worker
  ```

- **akadmin password**: in the UI (**Directory → Users → akadmin → Set password**); the bootstrap value in SOPS is ignored after the first start.
- **R2 token**: create a new token, `sops edit` the two `AWS_*` values, push, run a backup by hand, then delete the old token.
- **restic password**: `restic key add` / `key remove` need the `keys/` lock rule removed for a moment. Then update `RESTIC_PASSWORD` and the offline copy.
- **Never rotate `AUTHENTIK_SECRET_KEY`** ([Secrets](#2-secrets)).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Server/worker `CrashLoopBackOff`, log *connection refused* / *password authentication failed* | PostgreSQL not ready yet (it retries), or the two password values in SOPS differ |
| Log *Read-only file system* with a path | A path Authentik writes to that isn't mounted: add an emptyDir in `values.yaml` (`global.volumes`) |
| PostgreSQL `CrashLoopBackOff`, *Permission denied* on the data directory | Volume ownership: fsGroup 999. `kubectl -n authentik exec postgresql-0 -- ls -ld /var/lib/postgresql/18/docker` |
| Blueprint status **error** | Worker log (`kubectl -n authentik logs deploy/authentik-worker \| grep -i blueprint`): a typo, or a `!Find` that found nothing |
| A UI change keeps coming back | That attribute is in a blueprint: change it in Git |
| App: *invalid_request* (Argo CD) / *Login provider denied login request* (Grafana); server log *Invalid grant_type for provider* | The provider's `grant_types` is empty (the default for new providers): list `authorization_code` and `refresh_token` in the blueprint |
| App: *invalid_client* / *unauthorized_client* | The two copies of the client secret differ, or the worker started before the key existed (restart it) |
| App: *redirect_uri mismatch* | The app's callback URL isn't in the provider's `redirect_uris` (exact match) |
| App: login works but no admin rights | The `groups` claim: is your user in `lab-admins`? Argo CD **User Info** shows the groups |
| App: timeout after the Authentik login | The app's egress to `auth.lab` (Hubble `--verdict DROPPED`) |
| Login loop or *CSRF* error | The browser reached Authentik under another name than `auth.lab.theloomlabs.org`, or cookies blocked |
| Client addresses in **Events** are pod IPs | `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS` doesn't cover where the Gateway connects from |
| restic *unable to open config file* / *wrong password* | `authentik-restic` Secret: repository URL, keys, password |
| restic *Fatal: unable to save … 403* | The bucket lock refused an overwrite (interrupted upload, or a prune with the rules on); the next night succeeds |
| `AuthentikMetricsDown` | Pod down, or the scrape blocked (Hubble, `--verdict DROPPED`) |
| Proxied UI: *404* or Authentik's *Not Found* page | The outpost doesn't know the name: blueprint `Lab - Proxied UIs` not applied, or the provider missing from the embedded outpost (**Applications → Outposts**) |
| Proxied UI: redirect to `localhost` or an empty host after login | `authentik_host` missing from the embedded outpost's config (blueprint, outpost entry) |
| Proxied UI: *502 Bad Gateway* / timeout after the login | The server can't reach the UI: Hubble `--verdict DROPPED` (server egress or the UI's ingress), or the UI pod is down |
| Longhorn UI loads but stays empty / doesn't update | The websocket (`/v1/ws/…`) is cut: browser dev tools → Network → WS; Hubble for drops |
| Proxied UI: login loop | Cookies blocked for the name, or the clock off on a node |
