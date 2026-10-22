# Uptime Kuma (Swarm)

Uptime Kuma checks the lab the way a user sees it: web UIs answer, DNS resolves, the Kubernetes API is ready. It runs on Docker Swarm, so it keeps checking (and reporting through ntfy) while Kubernetes is down. Prometheus covers the inside view (docs/monitoring.md); this is the outside one.

The checks and the ntfy notification are code: **AutoKuma**, a second service in the stack, creates them in Uptime Kuma from `uptime_kuma_monitors` and deletes the ones removed from the list. Monitors added by hand in the UI are left alone.

| What | Where |
|---|---|
| Stack definition | `ansible/roles/uptime_kuma` (`templates/compose.yml.j2`), `ansible/playbooks/uptime.yml`, `make uptime` |
| Monitors, defaults, ntfy settings | `uptime_kuma_monitors`, `uptime_kuma_monitor_defaults`, `uptime_kuma_ntfy_*` in `ansible/roles/uptime_kuma/defaults/main.yml` |
| Images | `uptime_kuma_image` (slim, rootless), `uptime_kuma_autokuma_image` in the same file |
| DNS name | `status` in `dns/records.yml`, CNAME to the Swarm VIP like the other Swarm names ([edge.md](edge.md)) |
| ntfy token | `alerting_ntfy_token_uptime` in `swarm/alerting.sops.yaml` (ntfy reads its tokens from the alerting stack's config) |
| AutoKuma's login (the admin account) | `swarm/uptime.sops.yaml` |

| URL | Login |
|---|---|
| https://status.lab.theloomlabs.org | the admin account created at the first visit |

Monitors, history and settings are in SQLite in the local volume `uptime_kuma-data`; AutoKuma keeps the map from its IDs to Uptime Kuma's in `uptime_autokuma-data`. Both services run on one node: `uptime_kuma_node`, the last Swarm manager by default, labelled `homelab.uptime=true`. Its checks of the Swarm names (ntfy, hc, portainer) and its notifications reach Traefik over the `edge` network: those names are network aliases of Traefik there (`edge_hosts`, role edge). Only the `swarm-vip` monitor goes through the VIP, like a browser on the LAN.

Both volumes are backed up every night ([swarm-backup.md](swarm-backup.md)). Even without the backup, losing them costs the history only: a new admin account and `make uptime` bring back every monitor.

## Who watches whom

| Down | Noticed by |
|---|---|
| Kubernetes, the Gateway, an app UI | Uptime Kuma → ntfy |
| Prometheus or Alertmanager | healthchecks (Watchdog) → ntfy |
| Uptime Kuma or its node | Prometheus: `SwarmServiceReplicasMismatch` for `uptime/kuma`, `VMHostDown` for the node → Alertmanager → ntfy |
| The node with the alerting stack | Only as "connection lost" in the ntfy app (docs/alerting.md) |

## One-time setup

### 1. ntfy token and first deploy

Uptime Kuma publishes to the `alerts` topic as the `publisher` user with its own token. Add the token to the alerting secrets; the value never appears on screen:

```bash
TK_UK=tk_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 29)
sops set swarm/alerting.sops.yaml '["alerting_ntfy_token_uptime"]' "\"${TK_UK}\""
unset TK_UK
make sops-check
make alerting    # ntfy gets the new token (ntfy restarts)
make dns         # status CNAME
make uptime      # Uptime Kuma alone: AutoKuma waits for its login (step 3)
```

### 2. Admin account

AutoKuma logs in with the admin account, which exists only after the first visit. Without `swarm/uptime.sops.yaml`, `make uptime` deploys Uptime Kuma alone and says so. Do this right after the deploy: until an admin exists, anyone on the LAN who opens the page can create one.

1. Open https://status.lab.theloomlabs.org. The database step is skipped (SQLite is set in the stack).
2. **Create your admin account**: username and a password from the password manager → **Create**.
3. Optional, 2FA: profile icon (top right) → **Settings** → **Security** → **Two Factor Authentication** → **Setup 2FA** → **Show URI** and keep the `otpauth://…` text for step 3 → scan the QR code with the authenticator app, enter the code → **Enable 2FA**. AutoKuma logs in with the same account, so it needs that URI. If 2FA is already on and the URI is gone: **Disable 2FA**, then set it up again.

### 3. AutoKuma's login

The values go from prompts through Python (JSON-quoting any character in the password) straight into sops:

```bash
f=swarm/uptime.sops.yaml
read -rp 'Uptime Kuma username: ' UK_USER
read -rsp 'Uptime Kuma password: ' UK_PASS; echo
read -rsp '2FA URI (otpauth://..., empty without 2FA): ' UK_MFA; echo
export UK_USER UK_PASS UK_MFA
python3 -c 'import json, os; print(json.dumps({
  "uptime_kuma_username": os.environ["UK_USER"],
  "uptime_kuma_password": os.environ["UK_PASS"],
  "uptime_kuma_mfa_secret": os.environ["UK_MFA"]}))' \
| sops encrypt --filename-override "$f" --input-type json --output-type yaml /dev/stdin > "$f"
unset UK_USER UK_PASS UK_MFA
make sops-check
```

Changing the password (or 2FA) in Uptime Kuma later means updating this file (`sops edit swarm/uptime.sops.yaml`) and `make uptime`.

### 4. Deploy again, now with AutoKuma

```bash
make uptime
```

Check:

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org docker stack ps uptime
ssh ops@swarm-m-01.lab.theloomlabs.org docker service logs uptime_autokuma 2>&1 | tail -20
curl -sI https://status.lab.theloomlabs.org/ | head -1    # HTTP/2 302
```

Both tasks run on swarm-m-03 (`NODE` column). The AutoKuma log shows it logging in and creating the notification and each monitor. In the UI the monitors appear within a minute, tagged `AutoKuma`.

Test the notification: **Settings** → **Notifications** → **ntfy alerts** → **Edit** → **Test** — the phone gets a message. (Don't **Save** there: AutoKuma owns the entry and puts its own values back.)

### 5. Settings (UI, once)

AutoKuma doesn't manage Uptime Kuma's settings. **Settings** →

1. **General**: Display Timezone and Server Timezone `Europe/Zagreb`, Primary Base URL `https://status.lab.theloomlabs.org` → **Save**.
2. **Reverse Proxy**: **Trust Proxy** → **Yes** → **Save** (Traefik sets `X-Forwarded-For`; the login log shows real client addresses).

## Monitors

Defined in `uptime_kuma_monitors` (role defaults). Each key is AutoKuma's ID: renaming it deletes the monitor with its history and creates a new one. The fields are AutoKuma's entity types ([ENTITY_TYPES.md](https://github.com/BigBoot/AutoKuma/blob/master/ENTITY_TYPES.md)); `uptime_kuma_monitor_defaults` adds interval 60 s, two retries and the ntfy notification to each.

| ID | Type | Checks |
|---|---|---|
| `k8s-api` | keyword | `https://k8s-api…:6443/readyz` returns `ok` (TLS not verified: cluster CA) |
| `argocd` | http | `https://argocd…/healthz` |
| `grafana` | keyword | `https://grafana…/api/health` contains `ok` |
| `ntfy` | keyword | `https://ntfy…/v1/health` contains `"healthy":true` |
| `healthchecks` | http | `https://hc…/` (follows the redirect to the login page) |
| `dns-01`, `dns-02` | dns | each BIND server answers `k8s-api.lab.theloomlabs.org` |
| `swarm-vip` | keyword | `http://swarm…:8082/ping` returns `OK`: Traefik on whichever manager holds the Swarm VIP. When that is swarm-m-03, this also tests that a container can reach the VIP on its own node ([edge.md](edge.md)) |
| `portainer` | keyword | `https://portainer…/api/system/status` contains `"Version"` (no login) |
| `vaultwarden` | http | `https://vault…/alive` (Kubernetes, through the Gateway) |
| `forgejo` | http | `https://git…/api/healthz` (Kubernetes, through the Gateway) |
| `forgejo-ssh` | port | `git-ssh…:22` (Kubernetes, LoadBalancer .62) |
| `authentik` | http | `https://auth…/-/health/live/` (Kubernetes, through the Gateway) |
| `homepage` | http | `https://home…/api/healthcheck` (Kubernetes, through the Gateway; the one path without login) |
| `proxmox` | http | `https://pve…:8006/` (TLS not verified: self-signed) |

The `/readyz` probe is anonymous on purpose: kubeadm allows it, and the audit policy drops health checks, so it doesn't trip `KubeAuditAnonymousRequest`.

Add a monitor for every new app in Phase 3: a new key in `uptime_kuma_monitors`, then `make uptime`. To find a field name, create the monitor once in the UI and look at it with the `kuma` CLI (part of AutoKuma), or read ENTITY_TYPES.md.

## Day to day

- Upgrade: bump `uptime_kuma_image` or `uptime_kuma_autokuma_image` in `ansible/roles/uptime_kuma/defaults/main.yml`, `make uptime`. Renovate opens the PRs. Read the release notes first: Uptime Kuma major versions migrate the database, which can take a while on start, and AutoKuma has separate images for Uptime Kuma v1 (`uptime-kuma-v1-` tags).
- AutoKuma's log: `docker service logs uptime_autokuma` (or Loki, `{source="docker", stack="uptime", service="autokuma"}`). A login error means `swarm/uptime.sops.yaml` doesn't match the account.
- Logs: in Grafana (Loki, `{source="docker", stack="uptime"}`) or `docker service logs uptime_kuma` on a manager.
- Forgot the password or lost the 2FA device, on the node that runs it (swarm-m-03):

```bash
ssh -t ops@swarm-m-03.lab.theloomlabs.org 'docker exec -it $(docker ps -q -f name=uptime_kuma) npm run reset-password'
ssh -t ops@swarm-m-03.lab.theloomlabs.org 'docker exec -it $(docker ps -q -f name=uptime_kuma) npm run remove-2fa'
```

## Move to another node

1. Set `uptime_kuma_node` (for example in `ansible/inventory/group_vars/swarm.yml`) to a node that is not the alerting node. Remove the old label: `docker node update --label-rm homelab.uptime <old node>`.
2. `make uptime`. The new node starts with empty volumes: create the admin account again (step 2, same username and password as in `swarm/uptime.sops.yaml`), and AutoKuma recreates the monitors. Redo step 5.

## Troubleshooting

- **404 from Traefik:** the service is not on the `edge` network or its labels are wrong: `docker service inspect uptime_kuma --format '{{json .Spec.Labels}}'`.
- **Notification test fails with 403:** ntfy doesn't know `alerting_ntfy_token_uptime` yet: `make alerting`.
- **A monitor changed in the UI changes back:** AutoKuma owns it. Change it in `uptime_kuma_monitors` instead, or create your own monitor in the UI.
- **Monitors missing:** `docker service logs uptime_autokuma`. A file it can't parse is logged with its name (`/run/secrets/<id>.json`) and skipped.
- **ntfy/hc/portainer checks fail but the sites work from the workstation:** the name is missing from `edge_hosts` (role edge), so Uptime Kuma goes through the VIP instead of the `edge` network. `make edge` after adding it.
- **`swarm-vip` fails only while swarm-m-03 holds the VIP:** UFW drops containers calling their own node (`edge_container_cidrs`, "Hairpin" in [edge.md](edge.md)).
- **Task stays `Pending`:** no node has the `homelab.uptime=true` label: `docker node ls --filter node.label=homelab.uptime=true`.
