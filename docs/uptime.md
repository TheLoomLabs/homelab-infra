# Uptime Kuma (Swarm)

Uptime Kuma checks the lab the way a user sees it: web UIs answer, DNS resolves, the Kubernetes API is ready. It runs on Docker Swarm, so it keeps checking (and reporting through ntfy) while Kubernetes is down. Prometheus covers the inside view (docs/monitoring.md); this is the outside one.

| What | Where |
|---|---|
| Stack definition | `ansible/roles/uptime_kuma` (`templates/compose.yml.j2`), `ansible/playbooks/uptime.yml`, `make uptime` |
| Image | `uptime_kuma_image` in `ansible/roles/uptime_kuma/defaults/main.yml` (slim, rootless) |
| DNS name | `status` in `dns/records.yml`, CNAME like the alerting names |
| ntfy token | `alerting_ntfy_token_uptime` in `swarm/alerting.sops.yaml` (ntfy reads its tokens from the alerting stack's config) |
| Checks and notification | Uptime Kuma's own database, set up in its UI (below) |

| URL | Login |
|---|---|
| https://status.lab.theloomlabs.org | the admin account created at the first visit |

Monitors, history and settings are in SQLite in the local volume `uptime_kuma-data`, so the service runs on one node: `uptime_kuma_node`, the last Swarm manager by default, labelled `homelab.uptime=true`. It must not be the node the `ntfy`/`hc` CNAMEs point at: a container can't reach its own node's published ports, so its checks of ntfy and hc (and its notifications) would fail there.

There is no backup of the volume yet (Swarm stack backups come later in Phase 3). Losing it means redoing the setup below; the checks are listed here for that reason.

## Who watches whom

| Down | Noticed by |
|---|---|
| Kubernetes, the Gateway, an app UI | Uptime Kuma → ntfy |
| Prometheus or Alertmanager | healthchecks (Watchdog) → ntfy |
| Uptime Kuma or its node | Prometheus: `SwarmServiceReplicasMismatch` for `uptime/kuma`, `VMHostDown` for the node → Alertmanager → ntfy |
| The node with the alerting stack | Only as "connection lost" in the ntfy app (docs/alerting.md) |

## One-time setup

### 1. ntfy token

Uptime Kuma publishes to the `alerts` topic as the `publisher` user with its own token. Add the token to the alerting secrets; the value never appears on screen:

```bash
TK_UK=tk_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 29)
sops set swarm/alerting.sops.yaml '["alerting_ntfy_token_uptime"]' "\"${TK_UK}\""
unset TK_UK
make sops-check
```

### 2. Deploy

```bash
make alerting    # ntfy gets the new token (ntfy restarts)
make dns         # status CNAME
make uptime      # the stack
```

Check:

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org docker stack ps uptime --no-trunc
ssh ops@swarm-m-01.lab.theloomlabs.org docker service logs uptime_kuma 2>&1 | tail -20
curl -sI https://status.lab.theloomlabs.org/ | head -1    # HTTP/2 302 (to /setup or /dashboard)
```

The task runs on swarm-m-03 (`NODE` column). The image's healthcheck allows a 3-minute start; until then `docker stack ps` shows the task as `Running` with `health: starting`.

### 3. Admin account (do this right after the deploy)

Until an admin exists, anyone on the LAN who opens the page can create one.

1. Open https://status.lab.theloomlabs.org. The database step is skipped (SQLite is set in the stack).
2. **Create your admin account**: username and a password from the password manager → **Create**.
3. Profile icon (top right) → **Settings** → **Security** → **Two Factor Authentication** → **Setup 2FA** → scan the QR code with the authenticator app, enter the code → **Enable 2FA**.

### 4. Settings

**Settings** →

1. **General**: Display Timezone `Europe/Zagreb`, Server Timezone `Europe/Zagreb`, Primary Base URL `https://status.lab.theloomlabs.org` → **Save**.
2. **Reverse Proxy**: **Trust Proxy** → **Yes** → **Save** (Traefik sets `X-Forwarded-For`; the login log shows real client addresses).
3. **Monitor History**: keep 180 days → **Save**.

### 5. ntfy notification

**Settings** → **Notifications** → **Setup Notification**:

- Notification Type: **ntfy**
- Friendly Name: `ntfy alerts`
- Server URL: `https://ntfy.lab.theloomlabs.org`
- Topic: `alerts`
- Priority: `5`
- Authentication Method: **Access Token**, Access Token: output of `sops decrypt --extract '["alerting_ntfy_token_uptime"]' swarm/alerting.sops.yaml`
- **Default enabled** on (every new monitor gets it), **Apply on all existing monitors** on
- **Test** — the phone gets a message → **Save**

### 6. Monitors

**Add New Monitor** for each row. Common settings for all: Heartbeat Interval `60`, Retries `2`, Heartbeat Retry Interval `60`, Resend Notification if Down X times consecutively `0`, notification **ntfy alerts** ticked (default). Retries 2 means a service must fail three checks in a row (about three minutes) before the phone hears about it.

| Friendly Name | Monitor Type | Target | Extra |
|---|---|---|---|
| Kubernetes API | HTTP(s) - Keyword | URL `https://k8s-api.lab.theloomlabs.org:6443/readyz` | Keyword `ok`; Advanced → **Ignore TLS/SSL errors** on (cluster CA) |
| Argo CD | HTTP(s) | URL `https://argocd.lab.theloomlabs.org/healthz` | |
| Grafana | HTTP(s) - Keyword | URL `https://grafana.lab.theloomlabs.org/api/health` | Keyword `ok` |
| ntfy | HTTP(s) - Keyword | URL `https://ntfy.lab.theloomlabs.org/v1/health` | Keyword `"healthy":true` |
| healthchecks | HTTP(s) | URL `https://hc.lab.theloomlabs.org/` | (follows the redirect to the login page) |
| DNS dns-01 | DNS | Hostname `k8s-api.lab.theloomlabs.org`, Resolver Server `192.168.1.11`, Port `53` | Resource Record Type `A` |
| DNS dns-02 | DNS | Hostname `k8s-api.lab.theloomlabs.org`, Resolver Server `192.168.1.12`, Port `53` | Resource Record Type `A` |
| Proxmox | HTTP(s) | URL `https://pve.lab.theloomlabs.org:8006/` | Advanced → **Ignore TLS/SSL errors** on (self-signed) |

The `/readyz` probe is anonymous on purpose: kubeadm allows it, and the audit policy drops health checks, so it doesn't trip `KubeAuditAnonymousRequest`.

Add a row here (and the monitor in the UI) for every new app in Phase 3.

Optional: **Status Pages** → **New Status Page** `lab` → add the monitors → **Save**. It is readable without login on the LAN; skip it if that's not wanted.

## Day to day

- Upgrade: bump `uptime_kuma_image` in `ansible/roles/uptime_kuma/defaults/main.yml`, `make uptime`. Renovate opens the PR. Read the release notes first: major versions migrate the database, which can take a while on start.
- Logs: in Grafana (Loki, `{source="docker", stack="uptime"}`) or `docker service logs uptime_kuma` on a manager.
- Forgot the password or lost the 2FA device, on the node that runs it (swarm-m-03):

```bash
ssh -t ops@swarm-m-03.lab.theloomlabs.org 'docker exec -it $(docker ps -q -f name=uptime_kuma) npm run reset-password'
ssh -t ops@swarm-m-03.lab.theloomlabs.org 'docker exec -it $(docker ps -q -f name=uptime_kuma) npm run remove-2fa'
```

## Move to another node

1. Set `uptime_kuma_node` (for example in `ansible/inventory/group_vars/swarm.yml`) to a node that is not the alerting node. Remove the old label: `docker node update --label-rm homelab.uptime <old node>`.
2. `make uptime`. The new node starts with an empty volume: redo steps 3–6.

## Troubleshooting

- **404 from Traefik:** the service is not on the `edge` network or its labels are wrong: `docker service inspect uptime_kuma --format '{{json .Spec.Labels}}'`.
- **Notification test fails with 403:** the token in Uptime Kuma doesn't match `alerting_ntfy_token_uptime`, or `make alerting` wasn't run after adding it.
- **ntfy/hc checks fail but the sites work from the workstation:** the service runs on the node the CNAMEs point at (hairpin). Check the `NODE` column of `docker stack ps uptime`.
- **Task stays `Pending`:** no node has the `homelab.uptime=true` label: `docker node ls --filter node.label=homelab.uptime=true`.
