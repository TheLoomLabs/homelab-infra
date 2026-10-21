#!/usr/bin/env bash
# restic against the Swarm backup repository, from the workstation (Devbox):
# credentials from swarm/backup.sops.yaml, never written to disk.
#
#   scripts/swarm-restic.sh snapshots
#   scripts/swarm-restic.sh restore latest --tag volume=uptime_kuma-data --target /tmp/r
#
# See docs/swarm-backup.md (restores, quarterly maintenance).
set -euo pipefail

cd "$(dirname "$0")/.."
secrets=swarm/backup.sops.yaml
bucket=$(yq -r '.swarm_backup_bucket' ansible/roles/swarm_backup/defaults/main.yml)

plain=$(sops decrypt "$secrets")
value() { yq -r ".$1" <<<"$plain"; }

RESTIC_REPOSITORY="s3:https://$(value swarm_backup_r2_account_id).r2.cloudflarestorage.com/$bucket"
AWS_ACCESS_KEY_ID="$(value swarm_backup_r2_access_key_id)"
AWS_SECRET_ACCESS_KEY="$(value swarm_backup_r2_secret_access_key)"
AWS_DEFAULT_REGION=auto
export RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
# Only in this process's environment, which restic replaces (exec).
RESTIC_PASSWORD="$(value swarm_backup_restic_password)"
export RESTIC_PASSWORD
unset plain

exec restic "$@"
