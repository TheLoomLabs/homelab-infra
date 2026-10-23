# Swarm volume backups (restic → R2)

The Swarm stacks keep their state in local Docker volumes on the node they are pinned to (SQLite for ntfy, healthchecks and Uptime Kuma, BoltDB for Portainer). Traefik has no volume: its certificate lives in the Swarm itself ([edge.md](edge.md)). Kubernetes data is covered by Longhorn and Velero ([backup.md](backup.md)); this covers the Swarm side.

Every night a systemd timer on each Swarm node backs up, with restic, the volumes whose service runs on that node, to the Cloudflare R2 bucket `theloomlabs-swarm-backup`. An R2 bucket lock with no expiry keeps every snapshot: the nodes can add backups but can't delete or overwrite them, even with their own token. Old snapshots are removed by hand once a quarter ([Maintenance](#maintenance-quarterly)).

| What | Where |
|---|---|
| Role, script, units | `ansible/roles/swarm_backup` (`make swarm-backup`, playbook `playbooks/swarm-backup.yml`) |
| What is backed up | `swarm_backup_groups` in `ansible/roles/swarm_backup/defaults/main.yml` |
| R2 token, restic password | `swarm/backup.sops.yaml` (SOPS) → `/etc/swarm-backup/` on the nodes (root only) |
| Alerts | group `swarm-backup` in `kubernetes/platform/monitoring/vms-prometheusrule.yaml` |
| restic from the workstation | `scripts/swarm-restic.sh <restic args>` (Devbox) |

## What is backed up

| Group | Volumes | Node | Stopped during backup |
|---|---|---|---|
| ntfy | `alerting_ntfy-data` | swarm-m-01 | yes |
| healthchecks | `alerting_healthchecks-data` | swarm-m-01 | yes |
| uptime | `uptime_kuma-data`, `uptime_autokuma-data` | swarm-m-03 | yes (AutoKuma, then Uptime Kuma) |
| portainer | `portainer_portainer-data` (includes the compose files of stacks deployed from Portainer) | swarm-m-02 | yes |

Not backed up: the stacks themselves (they are in Git and redeployed by Ansible), the Swarm secrets (from SOPS), Portainer's agent (no state), Traefik's certificate (a Swarm config and secret; edge-cert gets a new one if the Swarm is rebuilt). Snapshots of the old `alerting_traefik-acme` volume (before Swarm HA) stay in the repository until the quarterly prune.

## How it works

1. `swarm-backup.timer` starts `swarm-backup.service` at 03:30 UTC (after Longhorn at 02:00 and Velero at 03:00), on all three managers. A run missed while the VM was off starts at the next boot.
2. For each group, the node checks whether one of the group's services has a running container on it. Only that node backs the group up, so each volume is backed up once, from the live copy. A stale copy of a volume, left on a node the service moved away from, is skipped.
3. Groups with `stop: true`: the services are scaled to 0, the volumes backed up, and the services scaled back to their previous replica count, also when the backup fails. Each service is down for about 10–20 seconds. SQLite and BoltDB files are only consistent when nobody writes them, and Portainer's BoltDB is locked while it runs. Uptime Kuma needs three failed checks before it notifies, so the short outages don't page.
4. `restic backup /var/lib/docker/volumes/<volume>/_data --host <node> --tag swarm-backup --tag volume=<volume>`: one snapshot per volume and night. Data is encrypted by restic before it leaves the node (AES-256, key derived from the restic password) and deduplicated across nights.
5. The results go to the node_exporter textfile directory of Alloy (`swarm_backup.prom`) and from there to Prometheus.

Every Sunday at 05:00 UTC one node (`swarm_backup_check_host`, swarm-m-01) runs `restic check --read-data-subset 5%`: the repository structure plus a random 5% of the data, downloaded and verified. R2 has no egress fees.

Logs: `journalctl -u swarm-backup` and `journalctl -u swarm-backup-check` on the nodes, also in Loki (`{source="journal", unit="swarm-backup.service"}`).

### Why the bucket lock covers everything except `locks/`

restic deduplicates: last night's snapshot still uses pack files from the first night. A lock that expires after N days would leave exactly those old packs deletable, and with them every recent snapshot. So the lock has no expiry, on every prefix restic only ever writes once (`data/`, `index/`, `snapshots/`, `keys/`, `config`). `locks/` stays unlocked, because every restic run creates and removes its own lock file there.

The price: restic on the nodes never runs `forget` or `prune` (they would fail anyway), and the repository grows a little every night. The volumes are small (megabytes) and change little, so this stays far below R2's free 10 GB for a long time. Removing old snapshots needs the Cloudflare account (passkey), not just a token: a stolen node token can add data but never destroy any.

### Metrics and alerts

| Metric | Meaning |
|---|---|
| `swarm_backup_volume_last_success_timestamp_seconds{volume}` | Last successful backup of the volume from this node |
| `swarm_backup_volume_size_bytes{volume}`, `…_added_bytes`, `…_duration_seconds` | Size, new data uploaded and duration of that backup |
| `swarm_backup_last_run_success`, `…_last_run_timestamp_seconds`, `…_last_run_duration_seconds` | Last run on this node (1 = every group OK) |
| `swarm_backup_volume_configured{volume,group}` | Written by Ansible: the volumes that should be backed up |
| `swarm_backup_check_last_success_timestamp_seconds`, `…_check_last_run_success` | Weekly `restic check` |

| Alert | Fires when |
|---|---|
| `SwarmBackupFailed` | A node's last run had a failed group, or a service couldn't be started again |
| `SwarmBackupStale` | A volume's newest backup is older than 26 hours (for 1h) |
| `SwarmBackupNeverRan` | A configured volume has no backup at all (for 26h): typo in a volume or service name, or its service runs nowhere |
| `SwarmBackupCheckStale` | No passing `restic check` in 8 days, or the last one failed |

A service left scaled down also fires `SwarmServiceReplicasMismatch` after 5 minutes.

## Set up (once)

### 1. R2 bucket, lock and token

In the Cloudflare dashboard:

1. **R2 Object Storage → Create bucket**: name `theloomlabs-swarm-backup`, location Automatic, Standard storage class.
2. In the bucket: **Settings → Bucket lock rules → Add rule**, five times, each with retention **Indefinite**: prefixes `data/`, `index/`, `snapshots/`, `keys/` and `config`. No rule for `locks/`, no rule without a prefix.
3. **R2 Object Storage → Manage API tokens → Create API token**: name `swarm-backup`, permission **Object Read & Write**, **Apply to specific buckets only** → `theloomlabs-swarm-backup`. Note the Access Key ID and Secret Access Key (shown once) and the account ID from the S3 endpoint.

### 2. Secrets file

Values are read from the terminal and only written encrypted. The restic password is generated.

```bash
F=swarm/backup.sops.yaml
read -rp 'R2 account ID: ' R2_ACCOUNT
read -rp 'R2 Access Key ID: ' R2_KEY_ID
read -rsp 'R2 Secret Access Key: ' R2_SECRET; echo
RESTIC_PW=$(openssl rand -base64 36)
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
swarm_backup_r2_account_id: "$R2_ACCOUNT"
swarm_backup_r2_access_key_id: "$R2_KEY_ID"
swarm_backup_r2_secret_access_key: "$R2_SECRET"
swarm_backup_restic_password: "$RESTIC_PW"
EOF
unset R2_ACCOUNT R2_KEY_ID R2_SECRET RESTIC_PW
```

**Keep the restic password offline**, next to the age key, the SSE-C key and the Longhorn key. Without it the backups can't be read, and it can't be reset.

```bash
sops decrypt swarm/backup.sops.yaml | yq -r .swarm_backup_restic_password
```

### 3. Deploy and test

```bash
make swarm-backup        # restic on the nodes, repository init (first run), timers
```

Then, on each node, a first backup and the first check (as root):

```bash
cd ansible
ansible swarm_managers -b -m ansible.builtin.command -a 'systemctl start swarm-backup.service'
ansible swarm-m-01 -b -m ansible.builtin.command -a 'systemctl start swarm-backup-check.service'
ansible swarm_managers -b -m ansible.builtin.shell -a 'journalctl -u swarm-backup -n 20 --no-pager'
```

From the workstation:

```bash
scripts/swarm-restic.sh snapshots     # one snapshot per volume, host = the node
```

**Check the lock works:** in the dashboard, open the bucket, go into `data/`, pick any object and try to delete it. R2 must refuse it. (A user reported in August 2026 that a lock rule didn't block deletes for them; check it rather than trust it.) Deleting an object under `locks/`, if one exists right then, must work.

## Restore a volume

Same steps for every group. Example: Uptime Kuma (`uptime_kuma-data`, group `uptime`, node swarm-m-03).

1. Find the snapshot (from the workstation or, as root, on the node after `set -a; . /etc/swarm-backup/env; set +a`):

   ```bash
   scripts/swarm-restic.sh snapshots --tag volume=uptime_kuma-data
   ```

2. On the node that runs the service, stop it (the whole group, in the order of `services`):

   ```bash
   docker service scale --detach=false uptime_autokuma=0 uptime_kuma=0
   ```

3. Keep the current data aside and restore into an empty directory. The snapshot path is the volume's `_data` directory; restic restores owners and permissions.

   ```bash
   V=/var/lib/docker/volumes/uptime_kuma-data
   SNAP=<snapshot ID>        # from step 1
   set -a; . /etc/swarm-backup/env; set +a
   mv $V/_data $V/_data.before-restore && mkdir $V/_data
   restic restore "$SNAP:$V/_data" --target $V/_data
   ```

4. Start the services again, in reverse order, and check the app:

   ```bash
   docker service scale --detach=false uptime_kuma=1 uptime_autokuma=1
   ```

5. When the app is fine, `rm -rf $V/_data.before-restore`.

A single file (for example one stack's compose file from Portainer) can be restored to any directory with `--include` and looked at first: `restic restore "$SNAP:$V/_data" --target /tmp/r --include /compose`.

## Disaster recovery

**A node is lost** (VM rebuilt with Terraform + `make base swarm logs swarm-backup`): its stacks' services start again on the rebuilt node with empty volumes. Before redeploying the stack, or with its services scaled to 0:

1. `docker volume create <volume>` on the node (if the stack hasn't created it), for every volume of the node's groups.
2. Restore each volume as above (steps 3–4), with `--host <old node name>` if the name changed.
3. `make alerting` / `make uptime` / `make portainer`.

**All Swarm VMs are lost**: rebuild the swarm (docs/bootstrap.md), `make logs swarm-backup`, then restore the volumes of each node before `make alerting uptime portainer`. The repository needs only the R2 token and the restic password, both in `swarm/backup.sops.yaml` (age key) and, for the password, offline.

## Maintenance (quarterly)

The nodes never delete. Every few months, or when the bucket grows (R2 dashboard → bucket → Metrics), remove old snapshots from the workstation:

1. Dashboard → bucket → **Settings → Bucket lock rules**: delete the five rules. Do it outside 03:30–04:00 UTC and Sunday 05:00 UTC.
2. Remove old snapshots and the data only they used:

   ```bash
   scripts/swarm-restic.sh forget --tag swarm-backup --group-by host,tags \
     --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
   scripts/swarm-restic.sh check
   ```

3. Add the five rules again (step 1 of [Set up](#1-r2-bucket-lock-and-token)) and try the delete test once more.

Changing the restic password (`restic key passwd`) also needs the `keys/` rule removed for a moment.

## Add a stack

A new stateful Swarm stack (Mattermost later) adds an entry to `swarm_backup_groups`: its services (in stop order), its volumes, `stop`. Then `make swarm-backup`, push (nothing in Kubernetes changes), and a manual run on its node. `SwarmBackupNeverRan` fires if a name is wrong. PostgreSQL will get an online `pg_dump` instead of `stop`.

## Repository errors

`restic check` errors mean missing or damaged objects in R2. `scripts/swarm-restic.sh check --read-data` reads everything and names the broken packs. The repairs (`restic repair index`, then `restic repair snapshots`) replace index and snapshot files, so they need the lock rules removed for a moment, like the maintenance. After `repair index` the next nightly backup uploads the missing data again, as long as it is still in the volumes. Follow restic's [troubleshooting guide](https://restic.readthedocs.io/en/stable/077_troubleshooting.html) step by step.

Stale locks after a crash: `scripts/swarm-restic.sh unlock`.

## Limitations

- Only stacks pinned to a node are covered, and only on managers (the script scales services). Swarm HA keeps the stateful stacks pinned.
- A backup costs each stopped service 10–20 seconds of downtime every night.
- If an upload is interrupted after R2 stored the object, restic's retry of the same object is refused by the lock and that night's backup of the volume fails; the next night succeeds.
