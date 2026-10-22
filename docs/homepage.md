# Homepage (lab dashboard)

[Homepage](https://gethomepage.dev) is the lab's start page at **https://home.lab.theloomlabs.org**: every app with a status dot, cluster and node load, and a few read-only widgets. LAN and Tailscale only, like every lab name. It runs on Kubernetes and stores nothing: the configuration is in Git. Why it's built this way: [ADR 0019](adr/0019-homepage-dashboard.md).

| What | Where |
|---|---|
| Argo CD app (project `apps`) | `kubernetes/apps/homepage.yaml` |
| Manifests | `kubernetes/services/homepage/` (kustomize, for the ConfigMap only) |
| Cards, widgets, settings | `config/services.yaml`, `config/widgets.yaml`, `config/settings.yaml`, `config/bookmarks.yaml` |
| OIDC client secret, cookie secret, widget credentials | `secrets.sops.yaml` (SOPS) → Secret `homepage-env` |
| Deployment, ServiceAccount, Service, HTTPRoute | `homepage.yaml` |
| Network policies | `networkpolicy.yaml`; in the targets' namespaces: `server-from-homepage` (argocd), the `prometheus` policy (monitoring) |
| Read access to the cluster | `kubernetes/platform/app-rbac/homepage.yaml` (app `app-rbac`, project `platform`) |
| CPU and memory numbers | metrics-server (`kubernetes/apps/metrics-server.yaml`, [monitoring.md](monitoring.md#resource-metrics-api-metrics-server)) |
| Argo CD account for the widget | `accounts.homepage` and `role:homepage` in `kubernetes/platform/argocd/values.yaml` |
| Namespace + Pod Security level | `kubernetes/platform/namespaces/namespaces.yaml` (`restricted`) |
| Uptime monitor | `homepage` in `uptime_kuma_monitors` (`make uptime`) |

## How it runs

- Look: "control room" style in `custom.css` (dark background with a grid, cyan and magenta glow, Orbitron / JetBrains Mono / Exo 2 fonts loaded by the browser from Google Fonts), page at full window width (`settings.yaml`). The first row, **Status**, holds the five cards that say whether anything needs attention: alerts, Argo CD, Uptime Kuma, backups, storage. `custom.css` loads without login; it only uses Homepage's class names, so an upgrade that renames them loses styling, nothing else.
- One pod, image `ghcr.io/gethomepage/homepage`, as UID 1000 on port 3000, read-only root filesystem. Writable: an emptyDir under `/app/config` (Homepage copies a default file there for any file it doesn't find; the files from Git are mounted on top), `/app/.next/cache` and `/tmp`.
- **Configuration from Git.** `config/*` becomes the ConfigMap `homepage-config-<hash>`; any change gives it a new name, so the pod restarts with the new files. Nothing is edited in the pod.
- **Login.** With Authentik (`HOMEPAGE_AUTH_ENABLED` + `HOMEPAGE_OIDC_*`, [sso.md](sso.md#homepage)): members of `lab-admins` and `lab-users`. Every page and API call needs a session, except `/api/healthcheck` (probes, Uptime Kuma). With OIDC set, Homepage has no password login; while Authentik is down, the dashboard is locked.
- **Host check.** Only `Host: home.lab.theloomlabs.org` (and the pod address, for the probes) is answered (`HOMEPAGE_ALLOWED_HOSTS`).
- **What it reads, and how:**

| Card / widget | Source | Access |
|---|---|---|
| Kubernetes (header): nodes, CPU, memory | API server + metrics-server | ClusterRole `homepage-nodes` |
| Status and CPU/memory of Vaultwarden, Forgejo, Authentik, Argo CD, Grafana, Alerts, Prometheus, Longhorn | pods in their namespaces | ClusterRole `homepage-pods` through a RoleBinding per namespace (argocd, monitoring, longhorn-system, vaultwarden, forgejo, authentik) |
| Argo CD: apps, synced, out of sync, degraded | `argocd-server` API | account `homepage`, `role:homepage` (get Applications only) |
| Alerts: critical and warning alerts firing | Prometheus (`ALERTS`) | none (in-cluster) |
| Prometheus: targets up/down | Prometheus | none |
| Longhorn: used, capacity, volumes not healthy | Prometheus (Longhorn's metrics) | none |
| Portainer: Swarm services running / total | Prometheus (Alloy's replica metrics) | none |
| Workloads: pods running / pending / failed, restarts 24h | Prometheus (kube-state-metrics) | none |
| Grafana: dashboards, log lines/s, log volume 24h | Prometheus (Grafana's and Loki's metrics) | none |
| Swarm managers, DNS: nodes up, CPU, memory | Prometheus (Alloy on the VMs) | none |
| Forgejo runner: VM up, CPU, memory, disk (runner-01; the link opens Forgejo's runner list) | Prometheus (Alloy on the VM) | none |
| Traefik: instances up, requests/s, 5xx/s | Prometheus | none |
| ntfy: failed notifications 24h | Prometheus (Alertmanager) | none |
| Vaultwarden, Forgejo, Authentik, Backups: last successful backup (Velero, Vaultwarden, Forgejo, Authentik, oldest Swarm volume); Forgejo repo count (its /metrics) | Prometheus | none |
| Uptime Kuma: up, down, uptime | status page `lab` | none (public page, LAN only) |
| healthchecks: up, down | healthchecks API | read-only API key |
| Proxmox: VMs, CPU, memory | Proxmox API | token `homepage@pve!dashboard`, role PVEAuditor |
| Swarm and Proxmox status dots | `siteMonitor`: HTTP request from the pod | egress per host |

- **What it doesn't get.** No Secrets or ConfigMaps of other apps, no Ingress/HTTPRoute discovery, no Docker socket. Longhorn, Grafana and Portainer have native widgets, but they need Longhorn's unauthenticated API (which can delete volumes), Grafana's server-admin login, and a Portainer token that can deploy containers. Their numbers come from Prometheus instead.
- **Network.** Default deny. In: the Gateway, port 3000. Out: the API server, `argocd-server`:8080, Prometheus:9090, `status`/`portainer`/`ntfy`/`hc.lab.theloomlabs.org`:443 (Swarm VIP), `pve.lab.theloomlabs.org`:8006. No internet: the update check is off, and the browser loads the service icons from jsDelivr (dashboard-icons).
- **TLS to widgets isn't verified.** Homepage skips certificate checks for every widget and status call. So in-cluster widgets use plain service addresses, and the outside ones go only to the lab names above.

## Set up (once)

Each step is a few clicks in an app, then one secret file. The Argo CD token needs the `homepage` account, which only exists after the first push, so it goes into the secret afterwards ([step 6](#6-argo-cd-token)).

### 1. Proxmox token (read only)

In the Proxmox UI: **pve → Shell** (or `ssh root@pve.lab.theloomlabs.org`):

```bash
pveum user add homepage@pve --comment "Homepage dashboard, read only"
pveum aclmod / -user homepage@pve -role PVEAuditor
pveum user token add homepage@pve dashboard --privsep 1
pveum aclmod / -token 'homepage@pve!dashboard' -role PVEAuditor
```

The third command prints the token's `value` once: that is the token secret. The token ID is `homepage@pve!dashboard`. The user has no password and can't log in; with privilege separation the token gets only what both the user and the token have (PVEAuditor: read only).

### 2. healthchecks read-only API key

`https://hc.lab.theloomlabs.org` → log in → your project → **Settings** → **API Access** → **Create API Keys**. Use the **API key (read-only)**, not the read-write key.

### 3. Uptime Kuma status page

The widget reads a status page (Uptime Kuma has no read API).

1. `https://status.lab.theloomlabs.org` → **Status Pages** → **New Status Page**.
2. Name `Lab`, slug `lab` → **Next**.
3. **Add Group** (name it `Lab`) → **Add a monitor** → add every monitor → **Save**.

The page is public on the LAN at `/status/lab`, like the rest of the lab.

### 4. Secrets

Historical first setup (the login was a password then; since the SSO step the file holds `HOMEPAGE_OIDC_CLIENT_SECRET` instead of `HOMEPAGE_AUTH_PASSWORD`, see [sso.md](sso.md#forgejo-homepage-portainer-proxmox)). The login password and the cookie secret are generated; the widget credentials are read from the terminal. Leave the Argo CD token empty for now. Everything is written to the file encrypted.

```bash
F=kubernetes/services/homepage/secrets.sops.yaml
read -rp 'Proxmox token ID [homepage@pve!dashboard]: ' PVE_ID
PVE_ID=${PVE_ID:-'homepage@pve!dashboard'}
read -rsp 'Proxmox token secret: ' PVE_SECRET; echo
read -rsp 'healthchecks read-only API key: ' HC_KEY; echo
read -rsp 'Argo CD token (empty for now): ' ARGOCD_TOKEN; echo
AUTH_PW=$(openssl rand -base64 24)
AUTH_SECRET=$(openssl rand -base64 32)
export PVE_ID PVE_SECRET HC_KEY ARGOCD_TOKEN AUTH_PW AUTH_SECRET
python3 -c 'import json, os; e = os.environ; print(json.dumps({
  "apiVersion": "isindir.github.com/v1alpha3",
  "kind": "SopsSecret",
  "metadata": {"name": "homepage", "namespace": "homepage"},
  "spec": {"secretTemplates": [{"name": "homepage-env", "stringData": {
    "HOMEPAGE_AUTH_SECRET": e["AUTH_SECRET"],
    "HOMEPAGE_AUTH_PASSWORD": e["AUTH_PW"],
    "HOMEPAGE_VAR_ARGOCD_TOKEN": e["ARGOCD_TOKEN"],
    "HOMEPAGE_VAR_PROXMOX_TOKEN_ID": e["PVE_ID"],
    "HOMEPAGE_VAR_PROXMOX_TOKEN_SECRET": e["PVE_SECRET"],
    "HOMEPAGE_VAR_HEALTHCHECKS_KEY": e["HC_KEY"]}}]}}))' \
| sops encrypt --filename-override "$F" --input-type json --output-type yaml /dev/stdin > "$F"
echo "Homepage password (save it in Vaultwarden): $AUTH_PW"
unset PVE_ID PVE_SECRET HC_KEY ARGOCD_TOKEN AUTH_PW AUTH_SECRET
```

Check: `sops decrypt "$F" | yq '.spec.secretTemplates[0].stringData | keys'` lists the six keys, and `grep -c ENC "$F"` is not 0.

### 5. Deploy and check

Run `make manifests`, commit and push. The root app picks up `metrics-server`, `app-rbac` and `homepage`; the namespaces app creates the `homepage` namespace (the app retries until it exists).

```bash
kubectl -n argocd get app metrics-server app-rbac homepage   # Synced, Healthy
kubectl get apiservice v1beta1.metrics.k8s.io                # AVAILABLE True
kubectl top nodes                                            # five nodes with CPU/memory
kubectl -n homepage get pods,httproute,cnp                   # Running; CNPs VALID True
kubectl auth can-i list pods -n vaultwarden \
  --as=system:serviceaccount:homepage:homepage               # yes
kubectl auth can-i list pods -n kube-system \
  --as=system:serviceaccount:homepage:homepage               # no
kubectl auth can-i get secrets -n homepage \
  --as=system:serviceaccount:homepage:homepage               # no
curl -s -o /dev/null -w '%{http_code}\n' \
  https://home.lab.theloomlabs.org/api/healthcheck           # 200
make uptime                                                  # monitor "Homepage"
```

Watch for policy drops while it starts (a new app can't wait for audit mode, [network-policy.md](network-policy.md#adding-a-component-or-an-app)). Start `hubble observe` only after the port-forward prints "Hubble Relay is available":

```bash
cilium hubble port-forward &
hubble observe --namespace homepage --verdict DROPPED --verdict AUDIT --follow
```

Then open `https://home.lab.theloomlabs.org` and log in with the password from step 4. Every card should have a green dot and numbers. The Argo CD card shows an error until step 6.

### 6. Argo CD token

1. `https://argocd.lab.theloomlabs.org` → **Log in via Authentik** (lab-admins) → **Settings** → **Accounts** → **homepage** → **Tokens** → **Generate New** (no expiry, or a date you'll rotate at) → copy the token.
2. Put it into the secret and restart the pod (it reads its environment at start):

```bash
F=kubernetes/services/homepage/secrets.sops.yaml
read -rsp 'Argo CD token: ' T; echo
sops set "$F" '["spec"]["secretTemplates"][0]["stringData"]["HOMEPAGE_VAR_ARGOCD_TOKEN"]' \
  "$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$T")"
unset T
git add "$F"
git commit -m "chore(homepage): Argo CD token"
git push
```

3. After Argo CD has synced the SopsSecret (`kubectl -n homepage get secret homepage-env -o jsonpath='{.metadata.resourceVersion}'` changes): `kubectl -n homepage rollout restart deploy/homepage`.

## Add a card

1. Add it to `config/services.yaml` under its group (a new group also goes into `layout` in `settings.yaml`).
2. Kubernetes app: `namespace` + `app` (the pods' `app.kubernetes.io/name`) or `podSelector`, and a RoleBinding `homepage-pods` in that namespace in `kubernetes/platform/app-rbac/homepage.yaml`.
3. Anything Homepage itself calls (`siteMonitor`, a widget `url`): an egress rule in `networkpolicy.yaml`, and an ingress rule on the target's side if it's in the cluster. A widget that needs a credential gets a `HOMEPAGE_VAR_*` key in the secret (`sops set` as in step 6).
4. Icons: a name from [dashboard-icons](https://github.com/homarr-labs/dashboard-icons) (`<name>.svg`), or `mdi-<name>`.
5. Commit and push; the new ConfigMap restarts the pod.

## Upgrades

Renovate opens a PR for `ghcr.io/gethomepage/homepage`. Read the release notes: settings and widget fields change between versions, and a new config file shows up as a default copy in the emptyDir (harmless, but add it to `config/` if you use it). Nothing is stored, so going back is reverting the PR.

## Rotate

- **OIDC client secret**: a new value in both `kubernetes/services/authentik/oidc.sops.yaml` (`OIDC_HOMEPAGE_CLIENT_SECRET`) and here, push, restart the Authentik worker and Homepage.
- **Cookie secret**: `sops edit kubernetes/services/homepage/secrets.sops.yaml` (new values from `openssl rand -base64 24` / `32`), push, `kubectl -n homepage rollout restart deploy/homepage`. A new cookie secret logs everyone out.
- **Argo CD token**: generate a new one ([step 6](#6-argo-cd-token)), then delete the old one on the same page.
- **Proxmox token**: `pveum user token remove homepage@pve dashboard`, then the `token add` and `-token` lines of step 1 again, and `sops set` the new secret (same command as step 6, key `HOMEPAGE_VAR_PROXMOX_TOKEN_SECRET`).
- **healthchecks key**: **Revoke** on the project's Settings page, create a new one, `sops set` `HOMEPAGE_VAR_HEALTHCHECKS_KEY`.

After any `sops set`: commit, push, wait for the sync, restart the pod.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Page says the host is not allowed | The browser's `Host` isn't in `HOMEPAGE_ALLOWED_HOSTS` (homepage.yaml); the pod log shows the host it got |
| Login page comes back after logging in | `HOMEPAGE_EXTERNAL_URL` must be exactly `https://home.lab.theloomlabs.org` (cookies are `Secure`) |
| Pod `CrashLoopBackOff`, log says `EROFS` or *read-only file system* with a path | A new version writes somewhere new: add an emptyDir for that path in homepage.yaml |
| Pod `CreateContainerConfigError` | Secret `homepage-env` missing: `kubectl -n homepage get sopssecret,secret`, sops-secrets-operator logs |
| A widget shows *API Error* | Wrong credential (`sops decrypt`, then the steps above), or the call is dropped: `hubble observe --namespace homepage --verdict DROPPED` |
| Argo CD widget *403* | Token of another account, or `role:homepage` not applied: `kubectl -n argocd get cm argocd-rbac-cm -o yaml` |
| Kubernetes widget empty, log mentions metrics-server | `kubectl get apiservice v1beta1.metrics.k8s.io` not Available: [monitoring.md](monitoring.md#resource-metrics-api-metrics-server) |
| An app card's dot is grey / *not found* | No pods match `namespace` + `app` (check the label), or no RoleBinding in that namespace (log: *forbidden*) |
| A Swarm or Proxmox dot is red but the site works | Egress rule missing for that host, or the name doesn't resolve in the cluster |
| Uptime Kuma widget *404* | No status page with slug `lab` ([step 3](#3-uptime-kuma-status-page)) |
| Icons missing | The browser can't reach `cdn.jsdelivr.net` (off-line or blocked); the cards work without them |
| Plain fonts, no glow after a change | Browser cache: `Ctrl+Shift+R`. Fonts need `fonts.googleapis.com` / `fonts.gstatic.com` from the browser |
