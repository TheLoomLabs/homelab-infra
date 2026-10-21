# Alerting stack (Swarm): ntfy, healthchecks

Alertmanager in Kubernetes sends every alert to **ntfy**, and the ntfy Android app shows it on the phone. **healthchecks** is the dead man's switch: Alertmanager's always-firing `Watchdog` pings it every minute, and when the pings stop, healthchecks alerts through ntfy. Both run on Docker Swarm, outside Kubernetes, so a dead cluster can still be reported (ADR 0011). TLS and the way in are the **edge** stack: Traefik on every Swarm manager behind the Swarm VIP ([edge.md](edge.md)).

| What | Where |
|---|---|
| Stack definition | `ansible/roles/alerting` (`templates/compose.yml.j2`, `templates/ntfy-server.yml.j2`), `ansible/playbooks/alerting.yml`, `make alerting` |
| Secrets (ntfy users and tokens, healthchecks key) | `swarm/alerting.sops.yaml` (SOPS, loaded by the playbook) → Swarm secrets `alerting_*` |
| DNS names | `ntfy` and `hc` in `dns/records.yml`, CNAMEs to the Swarm VIP (`swarm`); also network aliases of Traefik on the `edge` network (`edge_hosts`) |
| Alertmanager side (routing, ntfy token, ping URL) | `kubernetes/platform/monitoring/values.yaml`, `notify.sops.yaml` (docs/monitoring.md) |

| Service | URL | Login |
|---|---|---|
| ntfy | https://ntfy.lab.theloomlabs.org | your admin user (`alerting_ntfy_admin_user` in `swarm/alerting.sops.yaml`) |
| healthchecks | https://hc.lab.theloomlabs.org | the superuser created below |

The services keep their state in local Docker volumes (`alerting_ntfy-data`, `alerting_healthchecks-data`), so the whole stack runs on one node: `alerting_node`, the first Swarm manager by default, labelled `homelab.alerting=true`. If that node is down, alerting is down; the ntfy app then shows the connection as lost. Traefik and the VIP survive the loss of any one manager ([edge.md](edge.md)), so the other Swarm UIs stay reachable.

## What is reported, and what isn't

| Failure | Reported? |
|---|---|
| Backups, volumes, pods, nodes | Alertmanager → ntfy |
| Prometheus, Alertmanager, the Kubernetes cluster | healthchecks misses the Watchdog pings → ntfy |
| The Swarm node with this stack | Only as "connection lost" in the ntfy app |
| No manager holds the Swarm VIP (every Traefik down) | Only as "connection lost" in the ntfy app (SwarmVipDown can't get through either) |
| A lab service that is up but not answering (web UI, DNS, API VIP) | Uptime Kuma → ntfy (docs/uptime.md) |
| The whole Proxmox host, power, Starlink | No. Accepted until a second machine runs this stack |

Away from home, the phone reaches ntfy only over Tailscale (Phase 4). ntfy keeps messages for 72 hours, so they arrive when the phone reconnects.

## One-time setup

### 1. ntfy password hashes

ntfy stores bcrypt hashes in its config. Usernames and hashes stay in the encrypted secrets file, not in Git or the docs. Create one hash for your login and one for the `publisher` user that Alertmanager and healthchecks use (its password is never used; type anything long):

```bash
ssh -t ops@swarm-m-01.lab.theloomlabs.org docker run --rm -it binwiederhier/ntfy:v2.31.1 user hash   # your phone password
ssh -t ops@swarm-m-01.lab.theloomlabs.org docker run --rm -it binwiederhier/ntfy:v2.31.1 user hash   # publisher
```

Each prints a hash starting with `$2a$10$`.

### 2. The secrets file

Pipe everything straight into sops. The ntfy tokens (`tk_` + 29 characters) and the healthchecks key are generated here:

```bash
f=swarm/alerting.sops.yaml
mkdir -p swarm
read -rp 'ntfy admin username: ' NTFY_ADMIN_USER
read -rp 'ntfy hash for that user: ' NTFY_ADMIN_HASH
read -rp 'ntfy hash for publisher: ' NTFY_PUB_HASH
TK_AM=tk_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 29)
TK_HC=tk_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 29)
TK_UK=tk_$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 29)
sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
alerting_ntfy_admin_user: "${NTFY_ADMIN_USER}"
alerting_ntfy_admin_hash: '${NTFY_ADMIN_HASH}'
alerting_ntfy_publisher_hash: '${NTFY_PUB_HASH}'
alerting_ntfy_token_alertmanager: "${TK_AM}"
alerting_ntfy_token_healthchecks: "${TK_HC}"
alerting_ntfy_token_uptime: "${TK_UK}"
alerting_healthchecks_secret_key: "$(openssl rand -base64 48)"
YAML
unset NTFY_ADMIN_USER NTFY_ADMIN_HASH NTFY_PUB_HASH TK_AM TK_HC TK_UK
make sops-check
```

### 3. Deploy

```bash
make dns         # ntfy and hc CNAMEs to the Swarm VIP
make edge        # Traefik, VIP, certificate, edge network (docs/edge.md), once
make alerting    # Swarm secrets + stack
```

Check:

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org docker stack ps alerting --no-trunc
curl -s https://ntfy.lab.theloomlabs.org/v1/health      # {"healthy":true}
curl -sI https://hc.lab.theloomlabs.org/ | head -1       # HTTP/2 302 (login page)
```

### 4. healthchecks: admin, ntfy integration, Watchdog check

Create the admin account (the container has no e-mail setup, so sign-up is closed):

```bash
ssh -t ops@swarm-m-01.lab.theloomlabs.org 'docker exec -it $(docker ps -q -f name=alerting_healthchecks) /opt/healthchecks/manage.py createsuperuser'
```

Then on https://hc.lab.theloomlabs.org, signed in:

1. **Integrations** → **ntfy** → **Add Integration**.
   - Topic: `alerts`
   - Server URL: `https://ntfy.lab.theloomlabs.org`
   - Access token: `sops decrypt --extract '["alerting_ntfy_token_healthchecks"]' swarm/alerting.sops.yaml`
   - Priority when a check goes down: **max**; when it comes back up: **default**.
   - **Save integration**, then **Test!** — the phone (step 5) should get a message.
2. **Checks** → **Add Check**. Name `k8s-alertmanager-watchdog`, Schedule: **Simple**, Period **1 minute**, Grace **5 minutes**. Make sure the ntfy integration is enabled for it (Integrations section of the check).
3. Copy the check's **ping URL** (`https://hc.lab.theloomlabs.org/ping/<uuid>`). It goes into `kubernetes/platform/monitoring/notify.sops.yaml` (docs/monitoring.md).

### 5. The phone

1. Install **ntfy** from F-Droid or Google Play.
2. Settings → **Manage users** → **Add user**: Service URL `https://ntfy.lab.theloomlabs.org`, your admin user and password.
3. **+** (subscribe) → topic `alerts` → **Use another server** → `https://ntfy.lab.theloomlabs.org` → keep **Instant delivery** on → **Subscribe**.
4. Allow the app to run unrestricted in the background (Android battery settings), or it misses messages while the phone sleeps.

## Day to day

- Change the stack or rotate a secret: edit the role or `sops edit swarm/alerting.sops.yaml`, then `make alerting`. Changed secrets become new Swarm secrets (the name carries a hash of the content) and the services restart; unused old ones are removed on the next run.
- Upgrade: bump the image in `ansible/roles/alerting/defaults/main.yml`, `make alerting`.
- Test ntfy from the workstation:

```bash
curl -s -H "Authorization: Bearer $(sops decrypt --extract '["alerting_ntfy_token_alertmanager"]' swarm/alerting.sops.yaml)" -d 'Test from docs/alerting.md' https://ntfy.lab.theloomlabs.org/alerts
```

## Rebuild or move to another node

The state is small: ntfy's message cache, healthchecks' check and integration. The DNS names point at the Swarm VIP, so they don't change. To move the stack (for example to a future machine outside `pve`, or after losing its node):

1. Set `alerting_node` (for example in `ansible/inventory/group_vars/swarm.yml`), then `make alerting`. The new node starts with empty volumes.
2. Restore the volumes from the nightly backup ([swarm-backup.md](swarm-backup.md), "Restore"), or start empty: redo step 4 (healthchecks admin, integration, check) and put the new ping URL into `notify.sops.yaml`. ntfy users and tokens come from the config and need nothing.

## Troubleshooting

- **Service stays at 0/1:** `docker service ps alerting_<name> --no-trunc` shows why (often the placement label: `docker node inspect swarm-m-01 --format '{{ .Spec.Labels }}'`).
- **Certificate or TLS errors, 404 from Traefik, a name that doesn't answer:** the edge stack, see "Troubleshooting" in [edge.md](edge.md).
- **ntfy returns 403 to Alertmanager:** the token in `notify.sops.yaml` doesn't match `alerting_ntfy_token_alertmanager`, or the ACL doesn't allow writing to `alerts`.
- **healthchecks can't send to ntfy:** `INTEGRATIONS_ALLOW_PRIVATE_IPS` must be `True` (ntfy has a private address); check the healthchecks log: `docker service logs alerting_healthchecks`.
