#!/usr/bin/env python3
"""Render every Argo CD app the way Argo CD would and validate the result.

For each Application in kubernetes/apps/:
  - Helm sources: `helm template` with the chart, version, repo and values
    files the Application names (charts are pulled once into a cache)
  - directory sources in this repo: the YAML files Argo CD would sync, read
    from the working tree (not from main), with directory.include/exclude
  - Git sources in other repos: a sparse clone at targetRevision, built with
    `kubectl kustomize` when the path holds a kustomization.yaml

Then:
  1. JSON schemas are generated from every CustomResourceDefinition found:
     the rendered output plus the CRDs Ansible installs (Gateway API, Cilium),
     at the versions pinned in ansible/inventory/group_vars/k8s.yml. So the
     schemas always match the pinned versions; nothing comes from a catalog.
  2. kubeconform -strict validates all rendered resources (built-in kinds
     against the upstream schemas for the cluster's Kubernetes version).
  3. promtool check rules runs on the spec of every PrometheusRule.
  4. Every app stays inside its Argo CD AppProject (kubernetes/apps/projects.yaml):
     source repos, destination, and the namespace/kind of each rendered
     resource. Argo CD would refuse those at sync time.
  5. Pod Security labels: every Namespace rendered carries valid
     pod-security.kubernetes.io/* labels with at least audit and warn, and
     every app's destination namespace is one of them (docs/pod-security.md).
  6. Network policies: every app's destination namespace (except the ones in
     NETPOL_EXEMPT) has a CiliumNetworkPolicy `default-deny` selecting all its
     pods (docs/network-policy.md).
  7. lokitool rules lint parses every Loki rule file (the data of each
     ConfigMap named loki-rules-*, docs/audit-logging.md): rule structure and
     LogQL. A broken file would stop Loki's ruler from loading any rule.

Output lands in .artifacts/manifests/ (git-ignored): rendered/ is what was
validated, cache/ holds pulled charts and clones between runs.

Needs: python3 (stdlib only), helm, kubectl, kubeconform, promtool, lokitool,
git and mikefarah yq v4. All of them are in devbox.json; CI installs the same.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS_DIR = REPO / "kubernetes" / "apps"
K8S_VARS = REPO / "ansible" / "inventory" / "group_vars" / "k8s.yml"
OUT = REPO / ".artifacts" / "manifests"
CACHE = OUT / "cache"
RENDERED = OUT / "rendered"
SCHEMAS = OUT / "schemas"
RULES = OUT / "rules"
LOKI_RULES = OUT / "loki-rules"
# ConfigMaps with this name prefix hold Loki ruler files (tenant "fake").
LOKI_RULES_PREFIX = "loki-rules-"

# This repository, as Argo CD sees it. Sources pointing here are read from the
# working tree, so a branch is checked before it reaches main.
SELF_REPOS = {
    "https://github.com/TheLoomLabs/homelab-infra.git",
    "https://github.com/TheLoomLabs/homelab-infra",
}

# Argo CD's directory source picks up these extensions.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")

# Keys this script understands. Anything else in an Application source fails
# loudly instead of being rendered differently from Argo CD.
SOURCE_KEYS = {"repoURL", "targetRevision", "chart", "path", "ref", "helm", "directory"}
HELM_KEYS = {"releaseName", "valueFiles"}
DIRECTORY_KEYS = {"include", "exclude", "recurse"}


class CheckError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CheckError(f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


# --- YAML (through yq, so the script needs no Python packages) ---------------

def check_yq() -> None:
    try:
        version = run(["yq", "--version"])
    except (CheckError, FileNotFoundError) as exc:
        raise CheckError("yq not found (needs mikefarah yq v4, devbox package yq-go)") from exc
    if "mikefarah" not in version:
        raise CheckError(f"wrong yq: {version.strip()} (needs mikefarah yq v4, devbox package yq-go)")


def check_tools(names: list[str]) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise CheckError(f"not in PATH: {', '.join(missing)} (run inside the devbox shell; see docs/ci.md)")


def load_yaml_docs(text: str, source: str) -> list[dict]:
    """All documents of a YAML stream as Python objects (empty ones dropped)."""
    proc = subprocess.run(
        ["yq", "eval-all", "--output-format=json", "--indent=0", "[.]", "-"],
        input=text, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CheckError(f"{source}: invalid YAML\n{proc.stderr.strip()}")
    docs = json.loads(proc.stdout) if proc.stdout.strip() else []
    return [d for d in docs if d is not None]


def load_yaml_file(path: Path) -> list[dict]:
    return load_yaml_docs(path.read_text(), str(path.relative_to(REPO)))


def write_docs(path: Path, docs: list[dict]) -> None:
    """One JSON document per YAML document: valid YAML for kubeconform/promtool."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"---\n{json.dumps(d, sort_keys=False)}\n" for d in docs))


# --- Pins from the Ansible inventory -------------------------------------------

def k8s_vars() -> dict:
    docs = load_yaml_file(K8S_VARS)
    if not docs:
        raise CheckError(f"{K8S_VARS} is empty")
    v = docs[0]
    for key in ("kubernetes_minor_version", "cilium_version",
                "gateway_api_version", "gateway_api_crds_sha256"):
        if key not in v:
            raise CheckError(f"{K8S_VARS.relative_to(REPO)}: {key} missing")
    return v


# --- Sources ---------------------------------------------------------------

def expand_braces(pattern: str) -> list[str]:
    """'{a,b}.yaml' -> ['a.yaml', 'b.yaml'] (one level, as used in the apps)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[: m.start()] + alt + pattern[m.end():]))
    return out


def glob_match(rel: str, pattern: str | None) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in expand_braces(pattern or ""))


def directory_files(base: Path, directory: dict) -> list[Path]:
    unknown = set(directory) - DIRECTORY_KEYS
    if unknown:
        raise CheckError(f"unsupported directory keys: {sorted(unknown)}")
    walker = base.rglob("*") if directory.get("recurse") else base.glob("*")
    files = []
    for f in sorted(walker):
        if not f.is_file() or f.suffix not in MANIFEST_SUFFIXES:
            continue
        rel = str(f.relative_to(base))
        if directory.get("include") and not glob_match(rel, directory["include"]):
            continue
        if directory.get("exclude") and glob_match(rel, directory["exclude"]):
            continue
        files.append(f)
    return files


def strip_sops(doc: dict) -> dict:
    """SopsSecrets keep their structure (only *Templates is encrypted); the
    top-level sops metadata block is not part of the resource."""
    if isinstance(doc, dict) and "sops" in doc:
        doc = {k: v for k, v in doc.items() if k != "sops"}
    return doc


def render_path(base: Path, directory: dict) -> list[dict]:
    if not base.is_dir():
        raise CheckError(f"path not found: {base}")
    if (base / "kustomization.yaml").exists() or (base / "kustomization.yml").exists():
        if directory:
            raise CheckError(f"{base}: directory options on a kustomize path")
        return load_yaml_docs(run(["kubectl", "kustomize", str(base)]), str(base))
    docs = []
    for f in directory_files(base, directory or {}):
        docs.extend(strip_sops(d) for d in load_yaml_file(f))
    return docs


def git_checkout(repo_url: str, revision: str, path: str) -> Path:
    """Sparse, shallow clone of repo_url at revision (a tag or branch)."""
    key = hashlib.sha256(f"{repo_url}@{revision}".encode()).hexdigest()[:16]
    dest = CACHE / "git" / key
    if not dest.exists():
        log(f"  clone {repo_url} {revision}")
        tmp = dest.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1",
             "--branch", revision, "--filter=blob:none", "--sparse", repo_url, str(tmp)])
        tmp.rename(dest)
    run(["git", "-C", str(dest), "sparse-checkout", "add", path])
    return dest / path


def pull_chart(repo_url: str, chart: str, version: str) -> Path:
    dest = CACHE / "charts" / f"{chart}-{version}-{hashlib.sha256(repo_url.encode()).hexdigest()[:8]}"
    if not (dest / chart / "Chart.yaml").exists():
        log(f"  pull {chart} {version} from {repo_url}")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        run(["helm", "pull", chart, "--repo", repo_url, "--version", version,
             "--untar", "--untardir", str(dest)])
    return dest / chart


def helm_template(app: dict, src: dict, kube_version: str, api_versions: list[str]) -> list[dict]:
    helm = src.get("helm") or {}
    unknown = set(helm) - HELM_KEYS
    if unknown:
        raise CheckError(f"unsupported helm keys: {sorted(unknown)}")
    chart_dir = pull_chart(src["repoURL"], src["chart"], src["targetRevision"])
    release = helm.get("releaseName") or app["metadata"]["name"]
    namespace = app["spec"]["destination"].get("namespace", "default")
    cmd = ["helm", "template", release, str(chart_dir), "--namespace", namespace,
           "--kube-version", kube_version, "--include-crds"]
    for v in api_versions:
        cmd += ["--api-versions", v]
    for vf in helm.get("valueFiles", []):
        if not vf.startswith("$values/"):
            raise CheckError(f"valueFiles entry not under $values/: {vf}")
        path = REPO / vf.removeprefix("$values/")
        if not path.is_file():
            raise CheckError(f"values file not found: {vf}")
        cmd += ["--values", str(path)]
    return load_yaml_docs(run(cmd), f"helm template {release}")


def load_apps() -> list[dict]:
    apps = []
    for f in sorted(APPS_DIR.glob("*.yaml")):
        for doc in load_yaml_file(f):
            if doc.get("kind") == "Application":
                doc["_file"] = str(f.relative_to(REPO))
                apps.append(doc)
    if not apps:
        raise CheckError(f"no Applications in {APPS_DIR}")
    return apps


def app_sources(app: dict) -> list[dict]:
    spec = app["spec"]
    sources = spec.get("sources") or ([spec["source"]] if "source" in spec else [])
    for s in sources:
        unknown = set(s) - SOURCE_KEYS
        if unknown:
            raise CheckError(f"{app['_file']}: unsupported source keys {sorted(unknown)}")
    return sources


def render_app(app: dict, kube_version: str, api_versions: list[str]) -> dict[str, list[dict]]:
    """Rendered documents per source label."""
    out: dict[str, list[dict]] = {}
    name = app["metadata"]["name"]
    for i, src in enumerate(app_sources(app)):
        try:
            if "ref" in src and "path" not in src and "chart" not in src:
                continue  # only provides $values
            if "chart" in src:
                label = (src.get("helm") or {}).get("releaseName") or name
                out[f"{i:02d}-helm-{label}"] = helm_template(app, src, kube_version, api_versions)
            elif "path" in src:
                if src["repoURL"] in SELF_REPOS:
                    base = REPO / src["path"]
                else:
                    base = git_checkout(src["repoURL"], src["targetRevision"], src["path"])
                label = src["path"].strip("/").replace("/", "_")
                out[f"{i:02d}-path-{label}"] = render_path(base, src.get("directory") or {})
            else:
                raise CheckError("source has neither chart nor path")
        except CheckError as exc:
            raise CheckError(f"{app['_file']} source {i}: {exc}") from None
    return out


# --- CRDs installed outside Argo CD (Ansible role cilium) -------------------

def gateway_api_crds(v: dict) -> list[dict]:
    version, sha = v["gateway_api_version"], v["gateway_api_crds_sha256"]
    dest = CACHE / f"gateway-api-{version}.yaml"
    if not dest.exists():
        url = (f"https://github.com/kubernetes-sigs/gateway-api/releases/download/"
               f"{version}/standard-install.yaml")
        log(f"  download Gateway API {version} CRDs")
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            raise CheckError(f"Gateway API {version}: sha256 {got}, expected {sha} (group_vars)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return load_yaml_file(dest)


def cilium_crds(v: dict) -> list[dict]:
    # The Cilium operator creates its CRDs at runtime; the chart doesn't ship
    # them. These are the same files, from the pinned release tag.
    path = "pkg/k8s/apis/cilium.io/client/crds"
    base = git_checkout("https://github.com/cilium/cilium.git", f"v{v['cilium_version']}", path)
    docs = []
    for f in sorted(base.rglob("*.yaml")):
        docs.extend(load_yaml_file(f))
    return docs


# --- CRD -> JSON schema (strict, like kubeconform's openapi2jsonschema) ------

# Inside these, a structural schema only adds checks on fields declared next
# to them (Cilium: "spec has ingress or egress"); their `properties` are no
# field list, so they must not become strict.
COMBINATORS = {"anyOf", "oneOf", "allOf", "not"}


def strictify(node, in_combinator: bool = False):
    if isinstance(node, list):
        return [strictify(n, in_combinator) for n in node]
    if not isinstance(node, dict):
        return node
    node = {k: strictify(v, in_combinator or k in COMBINATORS) for k, v in node.items()}
    if node.pop("x-kubernetes-int-or-string", False) or node.get("format") == "int-or-string":
        node.pop("type", None)
        node.pop("format", None)
        node["oneOf"] = [{"type": "string"}, {"type": "integer"}]
    preserve = node.get("x-kubernetes-preserve-unknown-fields")
    if "properties" in node and "additionalProperties" not in node and not preserve and not in_combinator:
        node["additionalProperties"] = False
    return node


def write_schemas(crds: list[dict]) -> int:
    count = 0
    for crd in crds:
        spec = crd["spec"]
        group, kind = spec["group"], spec["names"]["kind"].lower()
        for ver in spec.get("versions", []):
            schema = (ver.get("schema") or {}).get("openAPIV3Schema")
            if not ver.get("served", True) or not schema:
                continue
            schema = copy.deepcopy(schema)
            props = schema.setdefault("properties", {})
            props.setdefault("apiVersion", {"type": "string"})
            props.setdefault("kind", {"type": "string"})
            props.setdefault("metadata", {"type": "object"})
            path = SCHEMAS / group / f"{kind}_{ver['name']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(strictify(schema), indent=1))
            count += 1
    return count


def api_versions_of(crds: list[dict]) -> list[str]:
    """What Argo CD passes to Helm as .Capabilities.APIVersions for these CRDs."""
    out = set()
    for crd in crds:
        spec = crd["spec"]
        for ver in spec.get("versions", []):
            if ver.get("served", True):
                gv = f"{spec['group']}/{ver['name']}"
                out.add(gv)
                out.add(f"{gv}/{spec['names']['kind']}")
    return sorted(out)


def crds_in(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "CustomResourceDefinition"
            and d.get("apiVersion", "").startswith("apiextensions.k8s.io/")]


def dedupe_crds(crds: list[dict]) -> list[dict]:
    by_name: dict[str, dict] = {}
    for c in crds:
        by_name[c["metadata"]["name"]] = c
    return list(by_name.values())


# --- Argo CD projects ----------------------------------------------------------

# Built-in kinds that are not namespaced. Custom kinds take their scope from
# the CRDs collected in pass 1.
CLUSTER_SCOPED_BUILTIN = {
    ("", "Namespace"), ("", "Node"), ("", "PersistentVolume"), ("", "ComponentStatus"),
    ("rbac.authorization.k8s.io", "ClusterRole"), ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("apiregistration.k8s.io", "APIService"),
    ("storage.k8s.io", "StorageClass"), ("storage.k8s.io", "CSIDriver"), ("storage.k8s.io", "CSINode"),
    ("storage.k8s.io", "VolumeAttachment"), ("storage.k8s.io", "VolumeAttributesClass"),
    ("scheduling.k8s.io", "PriorityClass"), ("node.k8s.io", "RuntimeClass"),
    ("networking.k8s.io", "IngressClass"), ("networking.k8s.io", "IPAddress"),
    ("networking.k8s.io", "ServiceCIDR"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
    ("admissionregistration.k8s.io", "MutatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "MutatingAdmissionPolicyBinding"),
    ("certificates.k8s.io", "CertificateSigningRequest"), ("certificates.k8s.io", "ClusterTrustBundle"),
    ("flowcontrol.apiserver.k8s.io", "FlowSchema"),
    ("flowcontrol.apiserver.k8s.io", "PriorityLevelConfiguration"),
    ("resource.k8s.io", "DeviceClass"), ("resource.k8s.io", "ResourceSlice"),
}


def load_projects() -> dict[str, dict]:
    projects = {}
    for f in sorted(APPS_DIR.glob("*.yaml")):
        for doc in load_yaml_file(f):
            if doc.get("kind") == "AppProject":
                projects[doc["metadata"]["name"]] = doc.get("spec", {})
    return projects


def gk_of(doc: dict) -> tuple[str, str]:
    api = doc.get("apiVersion", "")
    return (api.split("/")[0] if "/" in api else "", doc.get("kind", ""))


def gk_listed(gk: tuple[str, str], entries: list[dict] | None) -> bool:
    return any(fnmatch.fnmatchcase(gk[0], e.get("group", "")) and fnmatch.fnmatchcase(gk[1], e.get("kind", ""))
               for e in entries or [])


def destination_ok(spec: dict, server: str, namespace: str) -> bool:
    """Argo CD destination matching: globs, `!` entries deny."""
    allowed = False
    for d in spec.get("destinations") or []:
        pat_ns = d.get("namespace", "")
        deny = pat_ns.startswith("!")
        if not fnmatch.fnmatchcase(server, d.get("server", "*")):
            continue
        if fnmatch.fnmatchcase(namespace, pat_ns.lstrip("!")):
            if deny:
                return False
            allowed = True
    return allowed


def check_projects(apps: list[dict], rendered: dict[str, list[dict]], crds: list[dict]) -> list[str]:
    """What Argo CD would refuse to sync: sources, destinations and resource
    kinds each AppProject doesn't permit."""
    projects = load_projects()
    scope = {(c["spec"]["group"], c["spec"]["names"]["kind"]): c["spec"].get("scope", "Namespaced") for c in crds}
    errors = []
    for app in apps:
        name = app["metadata"]["name"]
        pname = app["spec"].get("project", "default")
        spec = projects.get(pname)
        if spec is None:
            errors.append(f"{app['_file']}: project {pname!r} is not defined in {APPS_DIR.relative_to(REPO)}")
            continue
        for src in app_sources(app):
            if not any(fnmatch.fnmatchcase(src["repoURL"], pat) for pat in spec.get("sourceRepos") or []):
                errors.append(f"{name}: source {src['repoURL']} not in project {pname} sourceRepos")
        dest = app["spec"].get("destination", {})
        server = dest.get("server", "")
        dest_ns = dest.get("namespace", "")
        if not destination_ok(spec, server, dest_ns):
            errors.append(f"{name}: destination {server} namespace {dest_ns!r} not permitted by project {pname}")
        seen = set()
        for doc in rendered.get(name, []):
            gk = gk_of(doc)
            cluster = gk in CLUSTER_SCOPED_BUILTIN or scope.get(gk) == "Cluster"
            if cluster:
                ok = (gk_listed(gk, spec.get("clusterResourceWhitelist"))
                      and not gk_listed(gk, spec.get("clusterResourceBlacklist")))
                what = f"cluster-scoped {gk[1]}.{gk[0] or 'core'}"
                # Argo CD also checks a Namespace's name against destinations.
                ns_name = doc.get("metadata", {}).get("name", "")
                if ok and gk == ("", "Namespace") and not destination_ok(spec, server, ns_name):
                    ok, what = False, f"Namespace {ns_name!r} (not a destination)"
            else:
                ns = doc.get("metadata", {}).get("namespace") or dest_ns
                wl = spec.get("namespaceResourceWhitelist")
                ok = ((wl is None or gk_listed(gk, wl))
                      and not gk_listed(gk, spec.get("namespaceResourceBlacklist"))
                      and destination_ok(spec, server, ns))
                what = f"{gk[1]}.{gk[0] or 'core'} in namespace {ns!r}"
            if not ok and what not in seen:
                seen.add(what)
                errors.append(f"{name}: {what} not permitted by project {pname}")
    return errors


# --- Pod Security labels --------------------------------------------------------

PSA_PREFIX = "pod-security.kubernetes.io/"
PSA_LEVELS = {"privileged", "baseline", "restricted"}
PSA_VERSION = re.compile(r"^(latest|v1\.\d+)$")


def check_pod_security(apps: list[dict], rendered: dict[str, list[dict]]) -> list[str]:
    """Namespaces get their level in Git (kubernetes/platform/namespaces/), so
    a typo in a label or an app in an unlabelled namespace fails here, not
    silently in the cluster (an unknown label value is ignored by the API
    server's cluster defaults)."""
    errors = []
    declared = set()
    for app_name, docs in rendered.items():
        for doc in docs:
            if gk_of(doc) != ("", "Namespace"):
                continue
            ns = doc.get("metadata", {}).get("name", "")
            declared.add(ns)
            labels = {k[len(PSA_PREFIX):]: v for k, v in (doc.get("metadata", {}).get("labels") or {}).items()
                      if k.startswith(PSA_PREFIX)}
            for mode in ("enforce", "audit", "warn"):
                if mode in labels and labels[mode] not in PSA_LEVELS:
                    errors.append(f"{app_name}: namespace {ns}: {PSA_PREFIX}{mode}={labels[mode]!r} is not a level")
                if f"{mode}-version" in labels and not PSA_VERSION.match(str(labels[f"{mode}-version"])):
                    errors.append(f"{app_name}: namespace {ns}: {PSA_PREFIX}{mode}-version="
                                  f"{labels[f'{mode}-version']!r} is not latest or v1.<minor>")
            unknown = set(labels) - {m + v for m in ("enforce", "audit", "warn") for v in ("", "-version")}
            for k in sorted(unknown):
                errors.append(f"{app_name}: namespace {ns}: unknown label {PSA_PREFIX}{k}")
            # privileged namespaces may skip audit/warn (nothing to report)
            if labels.get("enforce") != "privileged":
                for mode in ("audit", "warn"):
                    if mode not in labels:
                        errors.append(f"{app_name}: namespace {ns}: no {PSA_PREFIX}{mode} label")
    for app in apps:
        dest_ns = app["spec"].get("destination", {}).get("namespace", "")
        if dest_ns and dest_ns not in declared:
            errors.append(f"{app['metadata']['name']}: destination namespace {dest_ns!r} has no Namespace with "
                          f"Pod Security labels (kubernetes/platform/namespaces/)")
    return errors


# --- Network policies ------------------------------------------------------------

# Node agents, the CNI and Longhorn (which ships its own policies).
NETPOL_EXEMPT = {"kube-system", "longhorn-system"}


def check_network_policies(apps: list[dict], rendered: dict[str, list[dict]]) -> list[str]:
    """A namespace without a default-deny policy is wide open, and nothing in
    the cluster says so. Every app's destination namespace needs one."""
    denied = set()
    for docs in rendered.values():
        for doc in docs:
            meta = doc.get("metadata", {})
            if (doc.get("kind") == "CiliumNetworkPolicy" and doc.get("apiVersion", "").startswith("cilium.io/")
                    and meta.get("name") == "default-deny"
                    and (doc.get("spec") or {}).get("endpointSelector") == {}):
                denied.add(meta.get("namespace", ""))
    errors = []
    for ns in sorted({a["spec"].get("destination", {}).get("namespace", "") for a in apps} - NETPOL_EXEMPT - {""}):
        if ns not in denied:
            users = sorted(a["metadata"]["name"] for a in apps
                           if a["spec"].get("destination", {}).get("namespace") == ns)
            errors.append(f"namespace {ns!r} ({', '.join(users)}): no CiliumNetworkPolicy default-deny "
                          f"with endpointSelector {{}}")
    return errors


# --- Main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-promtool", action="store_true", help="skip promtool check rules")
    ap.add_argument("--no-lokitool", action="store_true", help="skip lokitool rules lint (Loki rules)")
    ap.add_argument("--render-only", action="store_true",
                    help="only render into .artifacts/manifests/rendered (no kubeconform, no promtool, no lokitool)")
    args = ap.parse_args()
    if args.render_only:
        args.no_promtool = True
        args.no_lokitool = True

    try:
        check_tools(["helm", "kubectl", "git"] + ([] if args.render_only else ["kubeconform"])
                    + ([] if args.no_promtool else ["promtool"])
                    + ([] if args.no_lokitool else ["lokitool"]))
        check_yq()
        v = k8s_vars()
        kube_version = f"{str(v['kubernetes_minor_version']).strip()}.0"
        for d in (RENDERED, SCHEMAS, RULES, LOKI_RULES):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)

        apps = load_apps()

        # Pass 1: collect every CRD (rendered + Ansible-installed) so pass 2
        # can hand Helm the same API versions the cluster has.
        log(f"Pass 1: render {len(apps)} apps to collect CRDs")
        crds: list[dict] = []
        for app in apps:
            for docs in render_app(app, kube_version, []).values():
                crds.extend(crds_in(docs))
        crds.extend(crds_in(gateway_api_crds(v)))
        crds.extend(crds_in(cilium_crds(v)))
        crds = dedupe_crds(crds)
        log(f"  {len(crds)} CRDs, {write_schemas(crds)} schemas")

        log("Pass 2: render with the cluster's API versions")
        api_versions = api_versions_of(crds)
        resources: list[dict] = []
        by_app: dict[str, list[dict]] = {}
        for app in apps:
            name = app["metadata"]["name"]
            for label, docs in render_app(app, kube_version, api_versions).items():
                write_docs(RENDERED / name / f"{label}.yaml", docs)
                resources.extend(docs)
                by_app.setdefault(name, []).extend(docs)
                log(f"  {name}/{label}: {len(docs)} resources")
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.render_only:
        log(f"Rendered {len(resources)} resources into {RENDERED.relative_to(REPO)}")
        return 0

    rc = 0
    log("Argo CD projects (sources, destinations, resource kinds)")
    try:
        problems = check_projects(apps, by_app, crds)
    except CheckError as exc:
        problems = [str(exc)]
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    if problems:
        print(f"projects: FAILED ({len(problems)})", file=sys.stderr)
        rc = 1

    log("Pod Security labels (namespaces)")
    problems = check_pod_security(apps, by_app)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    if problems:
        print(f"pod security: FAILED ({len(problems)})", file=sys.stderr)
        rc = 1

    log("Network policies (default deny per namespace)")
    problems = check_network_policies(apps, by_app)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    if problems:
        print(f"network policies: FAILED ({len(problems)})", file=sys.stderr)
        rc = 1

    log(f"kubeconform (Kubernetes {kube_version}, strict)")
    schema_cache = CACHE / "kubeconform"
    schema_cache.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([
        "kubeconform", "-strict", "-summary", "-output", "text",
        "-kubernetes-version", kube_version,
        # No upstream schema for CRDs themselves; each one is still parsed
        # into a schema above, which fails on a malformed CRD.
        "-skip", "CustomResourceDefinition",
        "-cache", str(schema_cache),
        "-schema-location", "default",
        "-schema-location", str(SCHEMAS) + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
        str(RENDERED),
    ])
    rc |= proc.returncode

    if not args.no_promtool:
        rules = [d for d in resources
                 if d.get("kind") == "PrometheusRule" and d.get("apiVersion", "").startswith("monitoring.coreos.com/")]
        files = []
        for d in rules:
            meta = d.get("metadata", {})
            path = RULES / f"{meta.get('namespace', '_')}__{meta['name']}.yaml"
            write_docs(path, [{"groups": d.get("spec", {}).get("groups", [])}])
            files.append(str(path))
        log(f"promtool check rules ({len(files)} PrometheusRules)")
        if files:
            proc = subprocess.run(["promtool", "check", "rules", *files], capture_output=True, text=True)
            # promtool prints a SUCCESS line per file; show only problems.
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.strip() and not line.startswith(("Checking ", "  SUCCESS")):
                    print(line)
            if proc.returncode != 0:
                print("promtool: FAILED", file=sys.stderr)
            rc |= proc.returncode

    if not args.no_lokitool:
        files = []
        for d in resources:
            meta = d.get("metadata", {})
            if d.get("kind") != "ConfigMap" or not meta.get("name", "").startswith(LOKI_RULES_PREFIX):
                continue
            for key, text in sorted((d.get("data") or {}).items()):
                path = LOKI_RULES / f"{meta.get('namespace', '_')}__{meta['name']}__{key}"
                path.write_text(text)
                files.append(str(path))
        log(f"lokitool rules lint ({len(files)} Loki rule files)")
        if files:
            # --dry-run: parse and format in memory, leave the files alone.
            proc = subprocess.run(["lokitool", "rules", "lint", "--dry-run", *files],
                                  capture_output=True, text=True)
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.strip() and "level=info" not in line:
                    print(line)
            if proc.returncode != 0:
                print("lokitool: FAILED", file=sys.stderr)
            rc |= proc.returncode

    log("OK" if rc == 0 else "FAILED")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
