#!/usr/bin/env bash
# restic against the Authentik backup repository, from the workstation
# (Devbox): credentials from kubernetes/services/authentik/secrets.sops.yaml
# (Secret authentik-restic), never written to disk.
#
#   scripts/authentik-restic.sh snapshots
#   scripts/authentik-restic.sh restore latest --target /tmp/authentik
#
# See docs/sso.md (restore, quarterly maintenance).
set -euo pipefail

cd "$(dirname "$0")/.."
secrets=kubernetes/services/authentik/secrets.sops.yaml

plain=$(sops decrypt "$secrets")
value() { yq -r ".spec.secretTemplates[] | select(.name == \"authentik-restic\") | .stringData.$1" <<<"$plain"; }

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
