# CI and local checks

Every push to `main` and every pull request runs `.github/workflows/lint.yml`. `make lint` runs the same checks in the devbox shell; pre-commit runs the fast ones on each commit.

| Workflow | When | What |
|---|---|---|
| `lint.yml` | push to `main`, pull requests | The checks below; required status checks on `main` |
| `security.yml` | push to `main` (Kubernetes/Ansible changes), weekly, manual | [Image scan](#image-scan) with Trivy, report only |
| `renovate.yml` | weekly, manual | [Renovate](#dependency-updates-renovate) opens update PRs |

| Job | Checks |
|---|---|
| `terraform` | `terraform fmt -check`, `terraform validate`, tflint |
| `ansible` | yamllint (whole repo), ansible-lint (production profile) |
| `secrets` | gitleaks over the full history, `*.sops.yaml` files are encrypted |
| `manifests` | Every Argo CD app rendered and validated: kubeconform (strict), `promtool check rules`, `lokitool rules lint`, AppProject limits, Pod Security labels, default-deny policies |

## Manifest checks

`make manifests` runs `scripts/check-manifests.py`. It renders each Application in `kubernetes/apps/` the way Argo CD would, then validates the result. Argo CD itself only finds a broken manifest after the push, when a sync fails; this finds it before the commit.

What gets rendered, per source type:

| Source in the Application | Rendered with |
|---|---|
| Helm chart (`chart:`) | `helm template` with the chart, version, repo, release name, destination namespace and `$values/...` files from the Application |
| Path in this repo | The YAML files Argo CD would sync, with `directory.include`/`exclude`, from the **working tree** (so a branch is checked, not `main`) |
| Path in another repo | Sparse clone at `targetRevision`; `kubectl kustomize` when the path has a `kustomization.yaml` |
| `ref: values` | Nothing (only provides `$values`) |

Source keys the script doesn't know (inline Helm `values`, plugins, ...) fail the run instead of being rendered differently from Argo CD. Add support in the script when an app needs one.

### Schemas

- **Built-in kinds** are validated against the upstream Kubernetes schemas for the cluster version (`kubernetes_minor_version` in `ansible/inventory/group_vars/k8s.yml`), downloaded by kubeconform.
- **Custom resources** (Application, SopsSecret, ServiceMonitor, HTTPRoute, Certificate, RecurringJob, Cilium pools, ...) are validated against schemas **generated from the CRDs at the pinned versions**, never from a third-party catalog:
  - CRDs that charts or apps ship come out of the rendering itself (`helm template --include-crds`), so bumping a chart version also bumps its schemas.
  - CRDs that Ansible installs come from the same pins: Gateway API (`gateway_api_version`, checked against `gateway_api_crds_sha256`) and Cilium (`cilium_version`, from the release tag).
- Schemas are strict: an unknown field (a typo like `parentRef` in an HTTPRoute) fails. CRDs themselves are skipped by kubeconform (no upstream schema), but each one is parsed into a schema, which fails on a malformed CRD.
- SopsSecrets are validated without their top-level `sops:` block. Only `spec.secretTemplates` is encrypted, so the rest of the structure is checked as usual.

Rendering runs twice. The first pass collects every CRD; the second passes their API versions to Helm (`--api-versions`), as Argo CD does with the cluster's, so charts that check `.Capabilities` render their ServiceMonitors and rules.

### Argo CD projects

Every Application names an AppProject from `kubernetes/apps/projects.yaml` (`default` is locked). The script checks what Argo CD would refuse at sync time:

- the project exists, every source `repoURL` is in its `sourceRepos`, and the destination is in its `destinations`
- every rendered **namespaced** resource lands in a permitted namespace (its own `metadata.namespace`, or the destination's) and its kind isn't blacklisted
- every **cluster-scoped** resource (built-in kinds from a fixed list, custom kinds by the scope in their CRD) is in `clusterResourceWhitelist`
- every `Namespace` object's name is in `destinations` (Argo CD checks that too)

A new platform component needs its chart repo and namespace added to the `platform` project in the same commit; a Phase 3 app goes into `apps`, which allows namespaced resources only (no `Namespace`, see below). Its namespace goes into the destinations of both projects: `apps` for the app, `platform` for the namespaces app that creates it.

### Pod Security labels

Namespaces get their Pod Security level in Git ([pod-security.md](pod-security.md)). The script checks:

- every rendered `Namespace` has only known `pod-security.kubernetes.io/*` labels, levels `privileged`, `baseline` or `restricted`, versions `latest` or `v1.<minor>`
- every namespace that isn't `enforce: privileged` has `audit` and `warn` labels
- every app's destination namespace is one of those `Namespace` objects, so a new namespace can't slip in without a level

### Network policies

Every app's destination namespace, except kube-system and longhorn-system (`NETPOL_EXEMPT`), needs a CiliumNetworkPolicy named `default-deny` with `endpointSelector: {}` ([network-policy.md](network-policy.md)). The policies themselves go through kubeconform like everything else, against the schema of the pinned Cilium CRDs. In those schemas, `anyOf`/`oneOf` branches only say which fields must be set, so the strict mode leaves them open (it would otherwise reject every policy).

### Prometheus rules

The `spec` of every PrometheusRule (the repo's own and the ones charts render) goes through `promtool check rules`: PromQL syntax and label/annotation templates fail the run; duplicate rules are reported as warnings. `promtool` matches the Prometheus version that kube-prometheus-stack deploys. `--no-promtool` skips this step.

### Loki rules

Every key of a ConfigMap named `loki-rules-*` is a Loki ruler file ([audit-logging.md](audit-logging.md#alerts)). They go through `lokitool rules lint --dry-run`: rule structure (unknown fields fail) and LogQL syntax. This matters more than for Prometheus: when one file doesn't parse, Loki's ruler loads none of the changes. `lokitool` matches the Loki version the chart deploys. `--no-lokitool` skips this step.

### Output

Everything lands in `.artifacts/manifests/` (git-ignored):

| Path | Content |
|---|---|
| `rendered/<app>/` | What was validated, one file per source |
| `schemas/<group>/` | Generated JSON schemas |
| `rules/` | Extracted rule files given to promtool |
| `loki-rules/` | Extracted Loki rule files given to lokitool |
| `cache/` | Pulled charts, clones, kubeconform's schema cache; kept between runs, safe to delete |

`rendered/` is also handy for reviewing what a values change does to a chart: render before and after, then `diff -r`.

### Tool versions

| Tool | Devbox | CI |
|---|---|---|
| helm | `kubernetes-helm` | `azure/setup-helm`, same version as `helm_version` in `group_vars/k8s.yml` |
| kubeconform | `kubeconform` | GitHub release, SHA-256 pinned in the workflow |
| promtool | `prometheus.cli` from a pinned nixpkgs commit (devbox links only the default output of `prometheus`, and promtool lives in `cli`) | GitHub release, SHA-256 pinned in the workflow |
| lokitool | `grafana-loki` from the same pinned nixpkgs commit (also brings `loki` and `logcli`) | GitHub release, SHA-256 pinned in the workflow |
| yq (mikefarah v4) | `yq-go` | preinstalled on the runner |
| kubectl | `kubectl` | preinstalled on the runner |
| python3 | `python` | preinstalled on the runner (stdlib only, no packages) |

When kube-prometheus-stack moves to a new Prometheus version, bump `PROMETHEUS_VERSION` and its checksum in the workflow, and the nixpkgs commit of `prometheus.cli` in `devbox.json` (one that ships the same version). Same for the loki chart and `LOKITOOL_VERSION` / `grafana-loki`.

## Supply chain

- **Actions are pinned to full commit SHAs** (`uses: owner/action@<sha> # vX.Y.Z`); the repo setting *Require actions to be pinned to a full-length commit SHA* enforces it. Renovate bumps SHA and comment together.
- **Downloaded tools** (gitleaks, kubeconform, promtool, lokitool, Trivy) come from GitHub releases and are checked against a SHA-256 next to the version in the workflow. Trivy runs from its release binary, not a third-party action.
- `actions/checkout` runs with `persist-credentials: false`: no job pushes, so the token isn't left in `.git/config`.
- Workflows default to `permissions: contents: read`; only the Trivy job gets `security-events: write` (SARIF upload).

## Dependency updates (Renovate)

Self-hosted: `.github/workflows/renovate.yml` runs the Renovate action every Monday with a fine-grained token (`RENOVATE_TOKEN` secret). Config: `renovate.json`. No Mend app, no Dependabot version updates (Dependabot *alerts* stay on).

| What | Where | Manager |
|---|---|---|
| Helm charts and external Git sources of the Argo CD apps | `kubernetes/apps/*.yaml` | `argocd` |
| Images in Helm values | `kubernetes/platform/**/values*.yaml` | `helm-values` |
| Images in plain manifests of the Phase 3 apps | `kubernetes/services/**` | `kubernetes` |
| GitHub Actions (SHA + version comment) | `.github/workflows/` | `github-actions` |
| Terraform providers (+ `.terraform.lock.hcl`) | `terraform/` | `terraform` |
| Versions with a `# renovate:` comment on the line above (Cilium, cilium-cli, kube-vip, Helm, Gateway API, Alloy, tool versions in the workflows) | `ansible/inventory/group_vars/`, `ansible/roles/*/defaults/`, `.github/workflows/` | regex |
| `*_image: repo:tag` (Swarm stacks) | `ansible/roles/*/defaults/main.yml` | regex |

Policy:

- **Nothing merges itself.** Argo CD deploys whatever reaches `main`, so every PR is reviewed and merged by hand (squash merge, so the commit on `main` is signed by GitHub). The `lint` checks run on every PR; `manifests` renders the bumped chart and validates it.
- PRs are created Mondays before 08:00 (Europe/Zagreb), at most 10 open; a release must be **3 days old** before Renovate proposes it.
- The **Dependency Dashboard** issue lists everything pending, rate-limited or failing.
- Grouped: non-major GitHub Actions (one PR), kube-prometheus-stack with its CRD chart, the Alloy chart with the VM Alloy version, the Swarm alerting images, restic on the Swarm nodes with the restic image of the Vaultwarden and Forgejo backups. Forgejo is held to its LTS line (15.x). Majors always get their own PR.
- PR notes tell what to do besides merging: run the playbook for things Ansible installs (Cilium, kube-vip, Helm on the first control-plane node, Swarm stacks), update the SHA-256 for checksum-pinned assets (CI fails until it matches), keep promtool at kube-prometheus-stack's Prometheus.

To add a version Renovate should track in Ansible vars or a workflow, put a comment on the line above it:

```yaml
# renovate: datasource=github-releases depName=owner/repo extractVersion=^v(?<version>.+)$
some_version: 1.2.3
```

`extractVersion` strips the `v` when the tag has one and the value doesn't. `datasource=docker` for images.

## Image scan

`make images` (and the `security` workflow) scans every image the lab runs with Trivy: **HIGH and CRITICAL vulnerabilities that have a fix** (`--ignore-unfixed`). Report only: findings never fail the run; an image that can't be pulled or scanned does.

`scripts/scan-images.py` collects the images from:

| Source | How |
|---|---|
| Argo CD apps | `.artifacts/manifests/rendered` (from `check-manifests.py --render-only`): every `image:` field, plus image references in container args and env (Longhorn's engine and CSI images). Helm test hooks are skipped: Argo CD doesn't run them |
| Cilium (installed by Ansible) | its chart rendered at `cilium_version` with the role's values |
| Ansible | `*_image:` in role defaults (Swarm stacks), `image:` lines in role templates (kube-vip) with the version variables filled in |

Output in `.artifacts/images/`: `images.txt` (every image and the file that pins it), `json/` (Trivy's report per image), `trivy.sarif`. In CI the SARIF goes to **Security → Code scanning** (category `trivy-images`) and a table to the job summary. Each alert points at the file that pins the image, so the fix is a bump there; an alert closes when a scan no longer finds it.

`--list-only` only collects the images (no Trivy), `--no-cilium` skips the Cilium chart.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not find schema for <Kind>` | The CRD for that kind isn't rendered by any app nor installed by Ansible. Add its source to the script (like Gateway API and Cilium) or check the apiVersion |
| `additional properties '<x>' not allowed` | Unknown field: typo, wrong indentation, or a field newer than the pinned CRD version |
| `unsupported source keys` / `unsupported helm keys` | The Application uses a feature the script doesn't render yet |
| `wrong yq` | Another `yq` (the Python one) is first in `PATH`; use the devbox shell |
| `helm pull` fails | Chart version or repo URL wrong in the Application, or no network |
| `not permitted by project` | The app's repo, namespace or a resource kind is outside its AppProject: add it to `kubernetes/apps/projects.yaml` (or fix the namespace) |
| `destination namespace '<ns>' has no Namespace with Pod Security labels` | Add the namespace to `kubernetes/platform/namespaces/namespaces.yaml` (and to the project's `destinations`; an app namespace also to `platform`'s) |
| `no CiliumNetworkPolicy default-deny` | Add a `networkpolicy.yaml` with a `default-deny` to the app's namespace (copy one from `kubernetes/platform/`) |
| `is not a level` / `unknown label` | Typo in a `pod-security.kubernetes.io/*` label |
| `lokitool`: `could not parse expression for alert` / `field … not found in type rulefmt.Rule` | LogQL syntax error or a misspelled rule key in a `loki-rules-*` ConfigMap; try the expression in Grafana → Explore |
| `scan-images.py`: `rendered is empty` | Run `python3 scripts/check-manifests.py --render-only` first (`make images` does) |
| Trivy `TOOMANYREQUESTS` / `Data limit exceeded` | Registry rate limit on the shared runner IP. The script retries, and for `ecr-public.aws.com/docker/library/*` scans the same image from Docker Hub. If it still fails, re-run the workflow later |
| Trivy `unable to find the specified image` | Tag doesn't exist (typo, or a value the collector misread); check `images.txt` |
