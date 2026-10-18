# CI and local checks

Every push to `main` and every pull request runs `.github/workflows/lint.yml`. `make lint` runs the same checks in the devbox shell; pre-commit runs the fast ones on each commit.

| Job | Checks |
|---|---|
| `terraform` | `terraform fmt -check`, `terraform validate`, tflint |
| `ansible` | yamllint (whole repo), ansible-lint (production profile) |
| `secrets` | gitleaks over the full history, `*.sops.yaml` files are encrypted |
| `manifests` | Every Argo CD app rendered and validated: kubeconform (strict), `promtool check rules` |

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

### Prometheus rules

The `spec` of every PrometheusRule (the repo's own and the ones charts render) goes through `promtool check rules`: PromQL syntax and label/annotation templates fail the run; duplicate rules are reported as warnings. `promtool` matches the Prometheus version that kube-prometheus-stack deploys. `--no-promtool` skips this step.

### Output

Everything lands in `.artifacts/manifests/` (git-ignored):

| Path | Content |
|---|---|
| `rendered/<app>/` | What was validated, one file per source |
| `schemas/<group>/` | Generated JSON schemas |
| `rules/` | Extracted rule files given to promtool |
| `cache/` | Pulled charts, clones, kubeconform's schema cache; kept between runs, safe to delete |

`rendered/` is also handy for reviewing what a values change does to a chart: render before and after, then `diff -r`.

### Tool versions

| Tool | Devbox | CI |
|---|---|---|
| helm | `kubernetes-helm` | `azure/setup-helm`, same version as `helm_version` in `group_vars/k8s.yml` |
| kubeconform | `kubeconform` | GitHub release, SHA-256 pinned in the workflow |
| promtool | `prometheus.cli` from a pinned nixpkgs commit (devbox links only the default output of `prometheus`, and promtool lives in `cli`) | GitHub release, SHA-256 pinned in the workflow |
| yq (mikefarah v4) | `yq-go` | preinstalled on the runner |
| kubectl | `kubectl` | preinstalled on the runner |
| python3 | `python` | preinstalled on the runner (stdlib only, no packages) |

When kube-prometheus-stack moves to a new Prometheus version, bump `PROMETHEUS_VERSION` and its checksum in the workflow, and the nixpkgs commit of `prometheus.cli` in `devbox.json` (one that ships the same version).

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not find schema for <Kind>` | The CRD for that kind isn't rendered by any app nor installed by Ansible. Add its source to the script (like Gateway API and Cilium) or check the apiVersion |
| `additional properties '<x>' not allowed` | Unknown field: typo, wrong indentation, or a field newer than the pinned CRD version |
| `unsupported source keys` / `unsupported helm keys` | The Application uses a feature the script doesn't render yet |
| `wrong yq` | Another `yq` (the Python one) is first in `PATH`; use the devbox shell |
| `helm pull` fails | Chart version or repo URL wrong in the Application, or no network |
