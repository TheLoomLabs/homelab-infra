#!/usr/bin/env bash
# restic against the Vaultwarden backup repository, from the workstation
# (Devbox): credentials from kubernetes/services/vaultwarden/secrets.sops.yaml
# (Secret vaultwarden-restic), never written to disk.
#
#   scripts/vaultwarden-restic.sh snapshots
#   scripts/vaultwarden-restic.sh restore latest --target /tmp/vw
#
# See docs/vaultwarden.md (restore, quarterly maintenance).
set -euo pipefail

cd "$(dirname "$0")/.."
secrets=kubernetes/services/vaultwarden/secrets.sops.yaml

plain=$(sops decrypt "$secrets")
value() { yq -r ".spec.secretTemplates[] | select(.name == \"vaultwarden-restic\") | .stringData.$1" <<<"$plain"; }

RESTIC_REPOSITORY="$(value RESTIC_REPOSITORY)"
AWS_ACCESS_KEY_ID="$(value AWS_ACCESS_KEY_ID)"
AWS_SECRET_ACCESS_KEY="$(value AWS_SECRET_ACCESS_KEY)"
AWS_DEFAULT_REGION=auto
export RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
# Only in this process's environment, which restic replaces (exec).
RESTIC_PASSWORD="$(value RESTIC_PASSWORD)"
export RESTIC_PASSWORD
unset plain

exec restic "$@"
