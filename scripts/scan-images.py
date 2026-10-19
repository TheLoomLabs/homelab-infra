#!/usr/bin/env python3
"""Scan every container image the lab runs with Trivy and write one SARIF file.

Images come from:
  - the rendered Argo CD apps in .artifacts/manifests/rendered, without Helm
    test hooks (Argo CD never runs them) (run
    `scripts/check-manifests.py --render-only` first; `make images` does both):
    `image:` fields anywhere (pods, Prometheus/Alertmanager resources, ...)
    plus image references in container args and env (Longhorn passes its
    engine and CSI images that way)
  - Cilium, which Ansible installs: its chart rendered with the role's values
    at cilium_version
  - Ansible role defaults (`*_image:`, the Swarm stacks) and `image:` lines in
    role templates (kube-vip), with the version variables filled in

Each image is scanned for HIGH and CRITICAL vulnerabilities that have a fix
(--ignore-unfixed). Results land in .artifacts/images/:
  images.txt   every image and where it comes from
  json/        Trivy's raw output per image
  trivy.sarif  one SARIF run for GitHub code scanning. Each finding points at
               the file that pins the image (the Application, the role
               defaults), so the alert shows where to bump it.

Report only: vulnerabilities never fail the run, scan errors do.
Needs: python3, helm, git, mikefarah yq v4, trivy (devbox). --list-only skips
Trivy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ next to the scripts
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("check_manifests", HERE / "check-manifests.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)  # type: ignore[union-attr]

REPO = cm.REPO
OUT = REPO / ".artifacts" / "images"
ROLES = REPO / "ansible" / "roles"
CILIUM_VALUES = ROLES / "cilium" / "templates" / "values.yaml.j2"
CILIUM_REPO = "https://helm.cilium.io"

SEVERITIES = "HIGH,CRITICAL"
# GitHub sorts and filters code scanning alerts by this (CVSS-like, 0-10).
SECURITY_SEVERITY = {"CRITICAL": "9.5", "HIGH": "8.0"}

# repo[:tag][@digest]; an optional registry host in front.
IMAGE_RE = re.compile(
    r"^(?:(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/)?"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*"
    r"(?::[\w][\w.-]{0,127})?(?:@sha256:[0-9a-f]{64})?$"
)


def log(msg: str) -> None:
    print(msg, flush=True)


# --- Collecting images --------------------------------------------------------

def looks_like_image(value: str, need_host: bool) -> bool:
    """need_host: for free-form strings (args, env), only accept refs with a
    registry host and a tag/digest, so ordinary words never match."""
    if not isinstance(value, str) or "{{" in value or " " in value:
        return False
    m = IMAGE_RE.match(value)
    if not m:
        return False
    if need_host:
        return bool(m.group("host")) and (":" in value.split("/", 1)[1] or "@" in value)
    return True


def images_in(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "image" and isinstance(val, str) and looks_like_image(val, need_host=False):
                out.add(val)
            elif key in ("args", "command") and isinstance(val, list):
                for arg in val:
                    if isinstance(arg, str):
                        cand = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
                        if looks_like_image(cand, need_host=True):
                            out.add(cand)
            elif key == "env" and isinstance(val, list):
                for e in val:
                    if isinstance(e, dict) and looks_like_image(e.get("value", ""), need_host=True):
                        out.add(e["value"])
            images_in(val, out)
    elif isinstance(node, list):
        for item in node:
            images_in(item, out)


def deployed(docs: list[dict]) -> list[dict]:
    """Drop Helm test hooks: Argo CD doesn't support them, so they never run."""
    out = []
    for d in docs:
        hook = ((d.get("metadata") or {}).get("annotations") or {}).get("helm.sh/hook", "")
        if not any(h.strip().startswith("test") for h in hook.split(",") if h):
            out.append(d)
    return out


def rendered_images(sources: dict[str, set[str]]) -> None:
    if not cm.RENDERED.is_dir() or not any(cm.RENDERED.iterdir()):
        raise cm.CheckError(f"{cm.RENDERED.relative_to(REPO)} is empty: run "
                            "`python3 scripts/check-manifests.py --render-only` first (make images)")
    app_files = {a["metadata"]["name"]: a["_file"] for a in cm.load_apps()}
    for app_dir in sorted(p for p in cm.RENDERED.iterdir() if p.is_dir()):
        found: set[str] = set()
        for f in sorted(app_dir.glob("*.yaml")):
            images_in(deployed(cm.load_yaml_file(f)), found)
        src = app_files.get(app_dir.name, f"kubernetes/apps/{app_dir.name}.yaml")
        for img in found:
            sources.setdefault(img, set()).add(src)


def ansible_vars() -> dict[str, str]:
    """Plain `name: value` scalars from group_vars and role defaults."""
    out: dict[str, str] = {}
    files = list((REPO / "ansible" / "inventory" / "group_vars").glob("*.yml"))
    files += list(ROLES.glob("*/defaults/main.yml"))
    for f in files:
        for line in f.read_text().splitlines():
            m = re.match(r"^([a-z0-9_]+):\s*\"?([^\s\"#]+)\"?", line)
            if m and "{{" not in m.group(2):
                out[m.group(1)] = m.group(2)
    return out


def fill(template: str, v: dict[str, str]) -> str:
    return re.sub(r"\{\{\s*([a-z0-9_]+)\s*\}\}", lambda m: v.get(m.group(1), m.group(0)), template)


def ansible_images(sources: dict[str, set[str]], v: dict[str, str]) -> None:
    for f in sorted(ROLES.glob("*/defaults/main.yml")):
        for line in f.read_text().splitlines():
            m = re.match(r"^[a-z0-9_]+_image:\s*\"?([^\s\"#]+)\"?", line)
            if m and looks_like_image(m.group(1), need_host=False):
                sources.setdefault(m.group(1), set()).add(str(f.relative_to(REPO)))
    for f in sorted(ROLES.glob("*/templates/*.j2")):
        for line in f.read_text().splitlines():
            m = re.match(r"^\s*-?\s*image:\s*(.+?)\s*$", line)
            if not m:
                continue
            img = fill(m.group(1).strip("\"'"), v)
            if looks_like_image(img, need_host=False):
                sources.setdefault(img, set()).add(str(f.relative_to(REPO)))
            elif "{{ alerting_" not in m.group(1):  # role defaults, collected above
                log(f"  skipped unresolved image in {f.relative_to(REPO)}: {m.group(1)}")


def cilium_images(sources: dict[str, set[str]], v: dict[str, str]) -> None:
    version = v.get("cilium_version")
    if not version:
        raise cm.CheckError("cilium_version not found in group_vars")
    # The values only reference addresses and names; images don't depend on them.
    values = fill(CILIUM_VALUES.read_text(), {**v, "k8s_api_vip": "192.0.2.10", "cluster_name": "homelab"})
    values = re.sub(r"\{\{.*?\}\}", "", values)
    vf = OUT / "cilium-values.yaml"
    vf.write_text(values)
    chart = cm.pull_chart(CILIUM_REPO, "cilium", version)
    text = cm.run(["helm", "template", "cilium", str(chart), "--namespace", "kube-system",
                   "--kube-version", f"{str(v.get('kubernetes_minor_version', '1.37')).strip()}.0",
                   "-f", str(vf)])
    found: set[str] = set()
    images_in(deployed(cm.load_yaml_docs(text, "cilium chart")), found)
    # Where to bump it: cilium_version.
    src = str(cm.K8S_VARS.relative_to(REPO))
    for img in found:
        sources.setdefault(img, set()).add(src)


# --- Scanning ------------------------------------------------------------------

def safe_name(image: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", image)[:150] + "-" + hashlib.sha256(image.encode()).hexdigest()[:8]


def scan(image: str) -> dict | None:
    out = OUT / "json" / f"{safe_name(image)}.json"
    proc = subprocess.run([
        "trivy", "image", "--quiet", "--skip-db-update", "--image-src", "remote",
        "--platform", "linux/amd64", "--scanners", "vuln",
        "--severity", SEVERITIES, "--ignore-unfixed",
        "--format", "json", "--output", str(out), image,
    ], capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"  ERROR {image}: {(proc.stderr.strip().splitlines() or ['trivy failed'])[-1]}")
        return None
    return json.loads(out.read_text())


def line_of(path: str, image: str) -> int:
    """Line in the pinning file that names the image (or its chart/var)."""
    try:
        lines = (REPO / path).read_text().splitlines()
    except OSError:
        return 1
    repo_name = image.split("@")[0].rsplit(":", 1)[0]
    for needle in (image, repo_name, "targetRevision", "_version:"):
        for i, line in enumerate(lines, 1):
            if needle in line:
                return i
    return 1


def to_sarif(results: dict[str, dict], sources: dict[str, set[str]]) -> dict:
    rules: dict[str, dict] = {}
    sarif_results = []
    for image, report in sorted(results.items()):
        src = sorted(sources[image])[0]
        line = line_of(src, image)
        repo_name = image.split("@")[0].rsplit(":", 1)[0]
        for target in report.get("Results") or []:
            for vuln in target.get("Vulnerabilities") or []:
                vid = vuln["VulnerabilityID"]
                sev = vuln.get("Severity", "HIGH")
                if vid not in rules:
                    rules[vid] = {
                        "id": vid,
                        "name": "OsPackageVulnerability" if target.get("Class") == "os-pkgs" else "LanguageSpecificPackageVulnerability",
                        "shortDescription": {"text": (vuln.get("Title") or vid)[:1000]},
                        "fullDescription": {"text": (vuln.get("Description") or vuln.get("Title") or vid)[:4000]},
                        "helpUri": vuln.get("PrimaryURL") or f"https://avd.aquasec.com/nvd/{vid.lower()}",
                        "help": {"text": f"{vid} ({sev}). Fixed in a newer package version; bump the image."},
                        "properties": {"security-severity": SECURITY_SEVERITY.get(sev, "8.0"),
                                       "tags": ["security", "vulnerability", sev]},
                    }
                pkg = vuln.get("PkgName", "?")
                # Without the tag: the alert stays the same across bumps that
                # don't fix it and closes when one does.
                fp = hashlib.sha256(f"{repo_name}|{target.get('Target', '')}|{pkg}|{vid}".encode()).hexdigest()
                sarif_results.append({
                    "ruleId": vid,
                    "level": "error",
                    "message": {"text": f"{image}: {pkg} {vuln.get('InstalledVersion', '?')} has {vid} ({sev}), "
                                        f"fixed in {vuln.get('FixedVersion', '?')}. Target: {target.get('Target', '')}. "
                                        f"Pinned in {', '.join(sorted(sources[image]))}."},
                    "locations": [{"physicalLocation": {
                        "artifactLocation": {"uri": src, "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": line},
                    }}],
                    "partialFingerprints": {"primaryLocationLineHash": fp},
                })
    rule_list = sorted(rules.values(), key=lambda r: r["id"])
    index = {r["id"]: i for i, r in enumerate(rule_list)}
    for r in sarif_results:
        r["ruleIndex"] = index[r["ruleId"]]
    version = subprocess.run(["trivy", "--version"], capture_output=True, text=True).stdout.split()
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Trivy", "informationUri": "https://github.com/aquasecurity/trivy",
                                "version": version[1] if len(version) > 1 else "unknown",
                                "rules": rule_list}},
            "automationDetails": {"id": "trivy-images/"},
            "results": sarif_results,
        }],
    }


def counts(report: dict) -> tuple[int, int]:
    c = h = 0
    for target in report.get("Results") or []:
        for vuln in target.get("Vulnerabilities") or []:
            c += vuln.get("Severity") == "CRITICAL"
            h += vuln.get("Severity") == "HIGH"
    return c, h


def summary(results: dict[str, dict], errors: list[str]) -> str:
    rows = sorted(((counts(r), img) for img, r in results.items()), key=lambda x: (-x[0][0], -x[0][1], x[1]))
    lines = ["## Trivy: HIGH/CRITICAL with a fix", "",
             f"{len(results)} images scanned, {len(errors)} errors.", "",
             "| Image | Critical | High |", "|---|---:|---:|"]
    lines += [f"| `{img}` | {c} | {h} |" for (c, h), img in rows if c or h]
    if not any(c or h for (c, h), _ in rows):
        lines.append("| (none) | 0 | 0 |")
    if errors:
        lines += ["", "Scan errors:", ""] + [f"- `{e}`" for e in errors]
    return "\n".join(lines) + "\n"


# --- Main ------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list-only", action="store_true", help="collect images, don't run Trivy")
    ap.add_argument("--no-cilium", action="store_true", help="skip rendering the Cilium chart")
    args = ap.parse_args()

    try:
        cm.check_tools(["helm", "git"] + ([] if args.list_only else ["trivy"]))
        cm.check_yq()
        (OUT / "json").mkdir(parents=True, exist_ok=True)
        for old in (OUT / "json").glob("*.json"):
            old.unlink()
        v = ansible_vars()
        sources: dict[str, set[str]] = {}
        log("Collecting images")
        rendered_images(sources)
        ansible_images(sources, v)
        if not args.no_cilium:
            cilium_images(sources, v)
    except cm.CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    with open(OUT / "images.txt", "w") as fh:
        for img in sorted(sources):
            fh.write(f"{img}\t{', '.join(sorted(sources[img]))}\n")
    log(f"  {len(sources)} images -> {(OUT / 'images.txt').relative_to(REPO)}")
    if args.list_only:
        return 0

    log("Updating the Trivy vulnerability DB")
    proc = subprocess.run(["trivy", "image", "--quiet", "--download-db-only"])
    if proc.returncode != 0:
        print("ERROR: trivy DB download failed", file=sys.stderr)
        return 1

    results: dict[str, dict] = {}
    errors: list[str] = []
    for i, img in enumerate(sorted(sources), 1):
        log(f"[{i}/{len(sources)}] {img}")
        report = scan(img)
        if report is None:
            errors.append(img)
        else:
            results[img] = report

    sarif = to_sarif(results, sources)
    (OUT / "trivy.sarif").write_text(json.dumps(sarif, indent=1))
    text = summary(results, errors)
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    log(f"{len(sarif['runs'][0]['results'])} findings -> {(OUT / 'trivy.sarif').relative_to(REPO)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
