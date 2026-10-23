# Forgejo Actions runner

Forgejo Actions jobs run on their own VM, **runner-01** (`192.168.1.45`), not on Kubernetes or the Swarm. The VM runs Docker and `forgejo-runner` as a systemd service. Each job runs in a fresh, unprivileged container. A hijacked job lands on a VM that holds nothing but the runner token, with no Docker socket inside the job. The reasons for a separate VM are in [ADR 0020](adr/0020-forgejo.md). Forgejo itself is in [forgejo.md](forgejo.md).

| What | Where |
|---|---|
| VM (4 vCPU, 8 GB, 60 GB, VMID 251) | `runner-01` in the `nodes` map, `terraform/envs/homelab/variables.tf` (group `ci_runners`) |
| Playbook, Make target | `ansible/playbooks/runner.yml`, `make runner` (roles `docker`, `forgejo_runner`) |
| Version, checksum, labels, capacity | `ansible/roles/forgejo_runner/defaults/main.yml` |
| Docker address pool, UFW | `ansible/inventory/group_vars/ci_runners.yml` |
| Runner UUID and token | `ci/runner.sops.yaml` (SOPS, every value encrypted) |
| Logs (journal) and host metrics | role `alloy` (`make logs`); `scrapeconfig-vms.yaml` (`group: ci`) |
| DNS name | `runner-01.lab.theloomlabs.org` (from the inventory, `make dns`) |

## How it runs

- **Binary from the release**, pinned by version and SHA-256 (`forgejo_runner_version`, `forgejo_runner_sha256`). The checksum comes from a GPG-verified download on the workstation ([Upgrade](#upgrade)). Versions live in `/usr/local/lib/forgejo-runner/`, and `/usr/local/bin/forgejo-runner` links to the current one.
- **Service** `forgejo-runner.service` runs as the system user `forgejo-runner` (home `/var/lib/forgejo-runner`, config `/etc/forgejo-runner/config.yml`, mode 0640). The user is in the docker group, so it is root-equivalent on this VM. That is why nothing else runs there.
- **Offline registration.** Forgejo learns the runner from `forgejo forgejo-cli actions register` with a 40-hex secret. The runner gets the same secret and the printed UUID in `server.connections` of its config. There is no `.runner` file and no registration token in the UI flow. Both values are in `ci/runner.sops.yaml`.
- **Instance-wide runner** (no `--scope`): every repository on the instance can use it. All accounts are created by hand (self-registration is off), so every job is code from someone with an account.
- **Labels:** `docker` and `ubuntu-latest`, both `node:22-bookworm` from Docker Hub. Workflows pick one with `runs-on: docker`. There is no `host` label, so nothing runs directly on the VM.
- **Capacity 2** jobs at a time, **1 h** timeout per job.
- **Jobs don't get Docker.** `container.docker_host: "-"` keeps the socket out of job containers, `privileged: false`, `valid_volumes: []` (no host mounts). A job that needs to build an image needs a different setup (rootless BuildKit or Kaniko in the job); that is not set up.
- **Networks.** Every job gets its own bridge network from `172.18.0.0/16` (`default-address-pools`, `/24` each). The pool is set explicitly, so Docker never picks a `192.168.x` range that overlaps the LAN. Jobs reach the internet (actions from `data.forgejo.org`, images, packages) and the LAN (Forgejo at `git.lab.theloomlabs.org` through the Gateway). Routed traffic is allowed (role `hardening`).
- **Cache server** for `actions/cache` runs inside the runner on the VM address. UFW trusts `172.17.0.0/16` and `172.18.0.0/16` on this VM so job containers can reach it. Without that, `actions/cache` hangs until it times out.
- **Clean-up:** `forgejo-runner-prune.timer` (Sundays 04:30) removes images and build cache unused for a week, then unused anonymous volumes. Job images are never re-pulled while they exist, so the weekly clean-up is also what brings in a newer `node:22-bookworm`.
- **Graceful stop:** on a stop or restart (`make runner`, `make reboot`, a Docker update) the runner takes no new job and waits up to 15 minutes for running ones (`forgejo_runner_shutdown_minutes`, `runner.shutdown_timeout` in the config). Jobs still running then are cancelled. systemd waits one minute longer (`TimeoutStopSec`) before it kills the daemon.
- **Observability:** Alloy ships the journal (the runner's own log is there: `unit="forgejo-runner.service"`) and serves host metrics. Container logs and cAdvisor are off on this VM, because every job container would be a new log stream and a new set of series. Job logs live in Forgejo (**Actions** tab), and their retention is Forgejo's (`LOG_RETENTION_DAYS`, default 365). `VMHostDown` and the node alerts cover the VM; the Docker Swarm dashboard shows it under Hosts. Two alerts cover the runner itself, which has no metrics of its own: `ForgejoRunnerDown` when `forgejo-runner.service` isn't active for 20 minutes (Alloy's systemd collector reports the unit's state, `alloy_systemd_units` in role `alloy`), and `ForgejoRunnerErrors` when the service runs but keeps logging errors, usually because it can't fetch jobs from Forgejo (Loki rule, `kubernetes/platform/loki/vms-rules.yaml`).

### Accepted risks

- A job can reach every LAN address. The same person could reach them from their own laptop, and the jobs are their own code. A public or shared instance would need egress rules (`DOCKER-USER` chain) and ephemeral runners.
- Actions from `data.forgejo.org` (`uses: actions/checkout@v4`) are fetched at job start, by tag. Pin full commit SHAs in workflows that matter, as in this repo's GitHub workflows ([ci.md](ci.md)).

## Set up (once)

Prerequisites: Forgejo is running ([forgejo.md](forgejo.md)), the `Makefile` has the `runner` target and `ci/*.sops.yaml` in `sops-check`.

### 1. Checksum of the release

On the workstation, outside the repo. Check the latest version first; when it is newer than `forgejo_runner_version`, use it and change the version too.

```bash
curl -fsS https://code.forgejo.org/api/v1/repos/forgejo/runner/releases/latest | jq -r .tag_name
V=13.2.0
U=https://code.forgejo.org/forgejo/runner/releases/download/v$V/forgejo-runner-$V-linux-amd64
cd "$(mktemp -d)" && curl -fsSLO "$U" && curl -fsSLO "$U.asc"
gpg --keyserver hkps://keys.openpgp.org --recv EB114F5E6C0DC2BCDD183550A4B61A2DC5923710
gpg --verify forgejo-runner-$V-linux-amd64.asc forgejo-runner-$V-linux-amd64   # "Good signature from ... Forgejo"
sha256sum forgejo-runner-$V-linux-amd64
```

Put the hash into `forgejo_runner_sha256` in `ansible/roles/forgejo_runner/defaults/main.yml`.

### 2. Register the runner and write the secret

```bash
F=ci/runner.sops.yaml
mkdir -p ci
SECRET=$(openssl rand -hex 20)
UUID=$(kubectl -n forgejo exec deploy/forgejo -- forgejo forgejo-cli actions register --name runner-01 --secret "$SECRET" | tail -n 1)
echo "$UUID"   # 8-4-4-4-12 hex, e.g. 37633331-3539-...
sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F" <<EOF
forgejo_runner_uuid: "$UUID"
forgejo_runner_token: "$SECRET"
EOF
unset SECRET UUID
```

**Site administration → Actions → Runners** now lists `runner-01` as offline. The command is idempotent: run it again with the same secret and nothing changes.

### 3. VM, base, DNS, logs, runner

```bash
make tf-plan     # 1 to add (module.vm["runner-01"]...), the inventory file changes
make tf-apply
cd ansible && ansible-playbook playbooks/base.yml --limit runner-01 && cd ..
make dns         # A and PTR record for runner-01
cd ansible && ansible-playbook playbooks/logs.yml --limit runner-01 && cd ..
make runner
```

### 4. Check

```bash
ssh ops@runner-01.lab.theloomlabs.org systemctl is-active forgejo-runner           # active
ssh ops@runner-01.lab.theloomlabs.org sudo journalctl -u forgejo-runner -n 20 --no-pager
ssh ops@runner-01.lab.theloomlabs.org docker info --format '{{.ServerVersion}}'
```

The runner shows as **Idle** under **Site administration → Actions → Runners**, with the labels `docker` and `ubuntu-latest`.

Prometheus: the targets `node-exporter` and `alloy-vm` for `runner-01` are UP once Argo CD has synced the monitoring app. Grafana → Explore → Loki: `{host="runner-01", unit="forgejo-runner.service"}`.

### 5. Test workflow

In a repository (e.g. `idabic/test`), make sure Actions is enabled in the repository settings (units), then push `.forgejo/workflows/test.yml`:

```yaml
name: test
on: [push, workflow_dispatch]
jobs:
  hello:
    runs-on: docker
    steps:
      - uses: actions/checkout@v4
      - name: Where am I
        run: |
          echo "job container: $(hostname)"; node --version; ls -la
          test ! -S /var/run/docker.sock && echo "no Docker socket: ok"
      - name: Cache
        uses: actions/cache@v4
        with:
          path: ~/.cache/runner-test
          key: runner-test-v1
      - run: mkdir -p ~/.cache/runner-test && date >> ~/.cache/runner-test/runs && cat ~/.cache/runner-test/runs
```

Expected: the job is green; the first run shows one line in `runs` and the cache step saves; a second run (**Actions → test → Run workflow**) restores the cache and shows two lines.

## Operation

### Upgrade

Renovate opens a PR when a new runner release comes out (`forgejo-releases` datasource). Read the release notes; majors change workflow behaviour and config keys. Then do step 1 for the new version, put the hash into the PR (`forgejo_runner_sha256`), merge, and run `make runner`. The restart waits up to 15 minutes for running jobs, then cancels the rest. A Forgejo LTS move may need a newer runner first ([forgejo.md](forgejo.md#upgrades)).

### Rotate the secret

The first 16 hex characters identify the runner, and the last 24 are the secret. Keep the first part and generate a new rest:

```bash
OLD=$(sops decrypt ci/runner.sops.yaml | yq -r .forgejo_runner_token)
SECRET=${OLD:0:16}$(openssl rand -hex 12)
kubectl -n forgejo exec deploy/forgejo -- forgejo forgejo-cli actions register --name runner-01 --secret "$SECRET"
sops set ci/runner.sops.yaml '["forgejo_runner_token"]' "\"$SECRET\""
unset OLD SECRET
make runner
```

The UUID stays the same.

### Labels, capacity, a second runner

Change `forgejo_runner_labels` or `forgejo_runner_capacity` in the role defaults and run `make runner`. A second runner is another `ci_runners` node in Terraform plus its own secret. `ci/runner.sops.yaml` holds one runner today; per-host values would move into `host_vars`.

### Reboot

`make reboot` reboots the runner last (after the Swarm managers). It stops the service first, which waits up to 15 minutes for running jobs, then reboots and waits for the service. Jobs still running after the 15 minutes fail. Check **Site administration → Actions → Runners** for an idle runner first to skip the wait.

### Remove the runner

**Site administration → Actions → Runners → runner-01 → Delete**, then remove the node from Terraform (`make tf-plan`, `make tf-apply`), the VM targets from `scrapeconfig-vms.yaml`, the address from the `prometheus` network policy, and the `ci-runner` rule groups from `vms-prometheusrule.yaml` and `kubernetes/platform/loki/vms-rules.yaml`.

## Troubleshooting

| Symptom | Cause, fix |
|---|---|
| Runner offline in Forgejo, journal shows `unauthenticated` or `runner not found` | UUID or token doesn't match what Forgejo has. Run the register command again with the token from `ci/runner.sops.yaml`; compare the printed UUID with `forgejo_runner_uuid` |
| `ForgejoRunnerDown` fires while the service is active | No `node_systemd_unit_state` series for the runner: the systemd collector is off. Run `make logs`; then `journalctl -u alloy` on runner-01 shows D-Bus errors if Alloy can't read the unit state |
| Journal shows TLS or DNS errors for `git.lab.theloomlabs.org` | The VM resolves through dns-01/dns-02 (`resolvectl status`). The certificate is the Gateway's wildcard certificate |
| Job waits forever in *Waiting* | No runner has the job's `runs-on` label (`docker` or `ubuntu-latest`), or both slots are busy |
| `actions/cache` hangs, then warns that the cache service is unreachable | UFW on the VM doesn't trust the Docker ranges: `sudo ufw status \| grep 172.1`. Run `cd ansible && ansible-playbook playbooks/base.yml --limit runner-01` |
| `Cannot connect to the Docker daemon` inside a job | Intended: jobs don't get the socket |
| Disk filling up | `ssh ops@runner-01.lab.theloomlabs.org docker system df`; run the clean-up now: `sudo systemctl start forgejo-runner-prune` |
| Job fails after `forgejo-runner` was upgraded | Read the release notes. v13 removed `set-output`/`set-env`/`add-path` and turns some warnings into errors |
