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

Output lands in .artifacts/manifests/ (git-ignored): rendered/ is what was
validated, cache/ holds pulled charts and clones between runs.

Needs: python3 (stdlib only), helm, kubectl, kubeconform, promtool, git and
mikefarah yq v4. All of them are in devbox.json; CI installs the same.
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

def strictify(node):
    if isinstance(node, list):
        return [strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = {k: strictify(v) for k, v in node.items()}
    if node.pop("x-kubernetes-int-or-string", False) or node.get("format") == "int-or-string":
        node.pop("type", None)
        node.pop("format", None)
        node["oneOf"] = [{"type": "string"}, {"type": "integer"}]
    preserve = node.get("x-kubernetes-preserve-unknown-fields")
    if "properties" in node and "additionalProperties" not in node and not preserve:
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


# --- Main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-promtool", action="store_true", help="skip promtool check rules")
    args = ap.parse_args()

    try:
        check_tools(["helm", "kubectl", "kubeconform", "git"] + ([] if args.no_promtool else ["promtool"]))
        check_yq()
        v = k8s_vars()
        kube_version = f"{str(v['kubernetes_minor_version']).strip()}.0"
        for d in (RENDERED, SCHEMAS, RULES):
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
        for app in apps:
            name = app["metadata"]["name"]
            for label, docs in render_app(app, kube_version, api_versions).items():
                write_docs(RENDERED / name / f"{label}.yaml", docs)
                resources.extend(docs)
                log(f"  {name}/{label}: {len(docs)} resources")
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rc = 0
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

    log("OK" if rc == 0 else "FAILED")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
