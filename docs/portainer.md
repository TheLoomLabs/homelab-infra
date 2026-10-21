# Portainer (Swarm)

Portainer CE is the web UI for the swarm: services, tasks, containers, logs, a shell in a container, volumes, networks, secrets. An **agent** runs on every node, so the UI sees all three managers, not just the node the server runs on. Portainer also deploys stacks of its own, for tests and short-lived apps, next to the stacks Ansible deploys.

| What | Where |
|---|---|
| Stack definition | `ansible/roles/portainer` (`templates/compose.yml.j2`), `ansible/playbooks/portainer.yml`, `make portainer` |
| Images, node | `portainer_image`, `portainer_agent_image`, `portainer_node` in `ansible/roles/portainer/defaults/main.yml` |
| DNS name | `portainer` in `dns/records.yml`, CNAME like the other Swarm names |
| Admin password | `portainer_admin_password` in `swarm/portainer.sops.yaml` |
| Uptime check | `portainer` in `uptime_kuma_monitors` (role `uptime_kuma`): `/api/system/status` |

| URL | Login |
|---|---|
| https://portainer.lab.theloomlabs.org | `admin`, password from `swarm/portainer.sops.yaml` |

How it is put together:

- **Server** (`portainer_portainer`): one replica on `portainer_node` (swarm-m-02 by default, label `homelab.portainer=true`). Its BoltDB database (settings, users, the stacks deployed from the UI and their compose files) is in the local volume `portainer_portainer-data`. That is why it is pinned: Swarm can't move a local volume, so on another node it would start empty. The agents give it the whole swarm, so the server's node doesn't matter for what it sees.
- **Agent** (`portainer_agent`): global, one per node. Mounts the Docker socket and `/var/lib/docker/volumes` (volume browser); no host filesystem. It listens on 9001 on the stack's own overlay network only; nothing is published on the nodes.
- **Traefik** terminates TLS and talks plain HTTP to the server on 9000 over the `edge` network. Port 9443 (Portainer's self-signed HTTPS) and 8000 (Edge agents, not used) are not published.
- **Admin**: `--admin-password-file` creates `admin` at the very first start, so there is no setup page waiting for the first visitor and no setup token in the logs (Loki).

Portainer is root on every Swarm node (through the agents and the Docker socket). It is reachable only on the LAN (and later through Tailscale), and its admin password belongs in the password manager.

## Who watches it

| Down | Noticed by |
|---|---|
| The UI | Uptime Kuma (`portainer` monitor) → ntfy |
| Server or an agent task | Prometheus: `SwarmServiceReplicasMismatch` for `portainer/portainer` or `portainer/agent` |
| Its node | `VMHostDown` |

## One-time setup

### 1. Admin password

Portainer needs at least 12 characters. Generate the password in the password manager, then put it in SOPS; it never appears on screen or in the shell history:

```bash
f=swarm/portainer.sops.yaml
read -rsp 'Portainer admin password: ' PT_PASS; echo
export PT_PASS
python3 -c 'import json, os; print(json.dumps({"portainer_admin_password": os.environ["PT_PASS"]}))' \
| sops encrypt --filename-override "$f" --input-type json --output-type yaml /dev/stdin > "$f"
unset PT_PASS
make sops-check
```

Without this file `make portainer` (and `make site`) skip Portainer and say so.

### 2. Deploy

```bash
make dns          # portainer CNAME
make portainer
make uptime       # the new monitor
```

Check:

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org docker stack ps portainer
curl -s https://portainer.lab.theloomlabs.org/api/system/status    # {"Version":"2.45.1",...}
```

`docker stack ps` shows one `agent` task per node and `portainer` on swarm-m-02.

### 3. Settings (UI, once)

Log in as `admin`. The environment `primary` (the swarm through the agents) is already there. Then **Settings** →

1. **General**: untick **Allow the collection of anonymous statistics** → **Save application settings**.
2. **Authentication**: session lifetime as you like (default 8 hours). SSO (OAuth) comes with the SSO step of Phase 3.

Changing the admin password later is done in the UI (**My account**). `swarm/portainer.sops.yaml` is read only when the database is created, so keep it in step by hand (`sops edit swarm/portainer.sops.yaml`): it is the password a rebuilt Portainer starts with.

## Stacks: Ansible's and Portainer's

| Stack | Deployed by | Change it with |
|---|---|---|
| `alerting`, `uptime`, `portainer`, later Mattermost | Ansible (`make …`) | Ansible only. Portainer shows them as **Limited** (it didn't create them); stop, restart, logs and exec work, editing doesn't |
| Anything created in **Stacks** → **Add stack** | Portainer | Portainer |

For a stack in Portainer:

- Prefer **Repository** (Git) over the web editor: the compose file lives in Git and Portainer can redeploy on changes (polling or webhook). Web-editor stacks exist only in Portainer's database.
- Names must not collide with Ansible's stacks.
- Behind Traefik: join the external network `edge` and add the Traefik labels under `deploy.labels`, like `ansible/roles/uptime_kuma/templates/compose.yml.j2`. A new name needs a CNAME to `swarm` in `dns/records.yml` (or uses the Kubernetes wildcard by mistake), and for calls from other containers an entry in `edge_hosts` (role edge, [edge.md](edge.md)).
- A stack with state in a local volume needs a placement constraint, or it starts empty on another node after a restart.
- Renovate doesn't see these images. Anything that turns out to be kept becomes an Ansible role (or moves to Kubernetes).
- Their compose files and settings are in `portainer_portainer-data`: Swarm stack backups (Phase 3, `swarm_backup`) include that volume.

## Day to day

- Upgrade: bump `portainer_image` and `portainer_agent_image` together in `ansible/roles/portainer/defaults/main.yml`, `make portainer`. Renovate opens one PR for both. CE stays on 2.x: 2.45 is the last 2.x LTS and gets fixes only; 3.x is the Business Edition, so Renovate is held below 3.
- Logs: Grafana (Loki, `{source="docker", stack="portainer"}`) or `docker service logs portainer_portainer` on a manager.
- Forgotten password: put a new one in `swarm/portainer.sops.yaml`, then reset with Portainer's helper on the server's node (the server must be stopped while it runs):

```bash
ssh ops@swarm-m-01.lab.theloomlabs.org docker service scale portainer_portainer=0
ssh -t ops@swarm-m-02.lab.theloomlabs.org 'docker run --rm -v portainer_portainer-data:/data portainer/helper-reset-password'
ssh ops@swarm-m-01.lab.theloomlabs.org docker service scale portainer_portainer=1
```

The helper prints a new random password for `admin`; log in with it and change it to the one in the file.

## Move to another node

1. Set `portainer_node` (for example in `ansible/inventory/group_vars/swarm.yml`) and remove the old label: `docker node update --label-rm homelab.portainer <old node>`.
2. Move the data, or start empty: `make portainer` on its own starts with an empty database and the admin from `swarm/portainer.sops.yaml`; Portainer's own stacks and settings are then gone (restore the volume from the Swarm stack backup).

## Troubleshooting

- **404 from Traefik:** the server is not on the `edge` network or its labels are wrong: `docker service inspect portainer_portainer --format '{{json .Spec.Labels}}'`.
- **Environment `primary` down / "unable to connect to the agent":** `docker service ps portainer_agent` (one running task per node) and `docker service logs portainer_agent`. Server and agent versions must match.
- **Login fails right after the first deploy:** the password in the file is shorter than 12 characters (the role refuses that) or the volume already existed from an earlier deploy with another password: the file applies to a new database only.
- **Task stays `Pending`:** no node has the `homelab.portainer=true` label: `docker node ls --filter node.label=homelab.portainer=true`.
