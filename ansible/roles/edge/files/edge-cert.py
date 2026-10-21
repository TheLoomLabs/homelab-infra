#!/usr/bin/env python3
"""Keep one wildcard certificate for Traefik in the Swarm (role edge).

Runs on every Swarm manager from a systemd timer (docs/edge.md). Only the Raft
leader acts, so there is one issuer whichever managers are up, and no shared
storage: the certificate is a Swarm config (public, readable for the expiry
check) and its key a Swarm secret, both named after a hash of the certificate
(<prefix>_crt_<sha10>, <prefix>_key_<sha10>). Raft replicates them to every
manager.

When the newest certificate in the Swarm has less than renew_fraction of its
lifetime left, or doesn't cover the configured domains, lego gets a new one
(DNS-01 through Cloudflare), the files go into the Swarm, lego's local copies
are deleted, and the Traefik service is updated to the new pair (a rolling
restart, one node at a time). Pairs no service uses are removed.

Results go to a node_exporter textfile for Alloy (EdgeCertRenewalFailing).

Usage: edge-cert CONFIG run [--here] [--force]
         --here:  act even if this node is not the Raft leader (Ansible's
                  first run, manual runs)
         --force: get a new certificate even if the current one is fine
       edge-cert CONFIG current
         print the names of the pair in use as JSON (Ansible renders them
         into the stack file)
"""

import base64
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

STATE_FILE = "/var/lib/edge-cert/state.json"
LABEL = "homelab.edge.cert"
CERT_NAME = "edge"  # lego --cert.name: certificates/edge.crt, edge.key


def log(message: str) -> None:
    print(message, flush=True)


def run(*args: str, stdin: str | None = None, env: dict | None = None, timeout: int = 300) -> str:
    return subprocess.run(
        list(args), input=stdin, env=env, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def docker(*args: str, stdin: str | None = None, timeout: int = 300) -> str:
    return run("docker", *args, stdin=stdin, timeout=timeout)


def is_leader() -> bool:
    return docker("node", "inspect", "self", "--format", "{{.ManagerStatus.Leader}}").strip() == "true"


def parse_cert(pem: str) -> dict:
    """notBefore, notAfter (epoch seconds) and DNS names of the first certificate."""
    out = run("openssl", "x509", "-noout", "-startdate", "-enddate", "-ext", "subjectAltName", stdin=pem)
    fmt = "%b %d %H:%M:%S %Y %Z"
    info = {"names": set()}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("notBefore="):
            info["not_before"] = datetime.datetime.strptime(line[10:], fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
        elif line.startswith("notAfter="):
            info["not_after"] = datetime.datetime.strptime(line[9:], fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
        elif line.startswith("DNS:"):
            info["names"] |= {n.strip()[4:] for n in line.split(",") if n.strip().startswith("DNS:")}
    return info


def stored_certs(prefix: str) -> list:
    """Every certificate config this script created, most recently issued first."""
    names = docker("config", "ls", "--filter", f"label={LABEL}", "--format", "{{.Name}}").split()
    certs = []
    for name in names:
        if not name.startswith(f"{prefix}_crt_"):
            continue
        spec = json.loads(docker("config", "inspect", name, "--format", "{{json .Spec}}"))
        pem = base64.b64decode(spec["Data"]).decode()
        info = parse_cert(pem)
        info["crt"] = name
        info["key"] = name.replace(f"{prefix}_crt_", f"{prefix}_key_", 1)
        certs.append(info)
    # By issue date, not expiry: a newer certificate may have a shorter lifetime.
    return sorted(certs, key=lambda c: c["not_before"], reverse=True)


def current_cert(config: dict) -> dict | None:
    """The newest stored certificate that covers exactly the configured domains."""
    wanted = set(config["domains"])
    for cert in stored_certs(config["prefix"]):
        if cert["names"] == wanted:
            return cert
    return None


def due(cert: dict | None, config: dict) -> str:
    """Why a new certificate is needed, or "" if not."""
    if cert is None:
        return "no certificate for " + ", ".join(config["domains"]) + " in the Swarm"
    lifetime = cert["not_after"] - cert["not_before"]
    renew_at = cert["not_after"] - lifetime * config["renew_fraction"]
    if time.time() >= renew_at:
        days = (cert["not_after"] - time.time()) / 86400
        return f"{cert['crt']} expires in {days:.1f} days"
    return ""


def issue(config: dict) -> dict:
    """Get a certificate with lego and store it in the Swarm."""
    path = config["lego_path"]
    # In the environment of lego only (readable by root alone), never in argv.
    with open(config["cloudflare_token_file"]) as f:
        env = dict(os.environ, CF_DNS_API_TOKEN=f.read().strip())
    args = [
        "lego", "run",
        "--path", path,
        "--accept-tos",
        "--server", config["acme_server"],
        "--dns", "cloudflare",
        "--cert.name", CERT_NAME,
        "--key-type", "EC256",
        # The decision to renew is made above, from the copy in the Swarm.
        "--renew-force",
        "--no-random-sleep",
        "--log.format", "text",
    ]
    for resolver in config["resolvers"]:
        args += ["--dns.resolvers", resolver]
    for domain in config["domains"]:
        args += ["--domains", domain]
    try:
        out = subprocess.run(args, env=env, check=True, capture_output=True, text=True, timeout=900)
        log(out.stderr.strip() or out.stdout.strip())
        cert_dir = os.path.join(path, "certificates")
        with open(os.path.join(cert_dir, f"{CERT_NAME}.crt")) as f:
            crt = f.read()
        with open(os.path.join(cert_dir, f"{CERT_NAME}.key")) as f:
            key = f.read()
    finally:
        # The key lives only in the Swarm (encrypted Raft log); lego keeps
        # just its account here.
        for sub in ("certificates", "archives"):
            shutil.rmtree(os.path.join(path, sub), ignore_errors=True)

    info = parse_cert(crt)
    digest = hashlib.sha256(crt.encode()).hexdigest()[:10]
    info["crt"] = f"{config['prefix']}_crt_{digest}"
    info["key"] = f"{config['prefix']}_key_{digest}"
    labels = ["--label", f"{LABEL}=true", "--label", f"{LABEL}.domains={','.join(sorted(info['names']))}"]
    docker("config", "create", *labels, info["crt"], "-", stdin=crt)
    docker("secret", "create", *labels, info["key"], "-", stdin=key)
    days = (info["not_after"] - time.time()) / 86400
    log(f"stored {info['crt']} and {info['key']}, valid for {days:.1f} days")
    return info


def service_spec(service: str) -> dict | None:
    try:
        return json.loads(docker("service", "inspect", service, "--format", "{{json .Spec.TaskTemplate.ContainerSpec}}"))
    except subprocess.CalledProcessError:
        return None  # not deployed yet (Ansible deploys it with this pair)


def roll_out(cert: dict, config: dict) -> None:
    """Point the Traefik service at the pair; a rolling restart."""
    spec = service_spec(config["service"])
    if spec is None:
        log(f"{config['service']} not deployed yet: nothing to update")
        return
    prefix = config["prefix"]
    old_configs = [c["ConfigName"] for c in spec.get("Configs") or [] if c["ConfigName"].startswith(f"{prefix}_crt_")]
    old_secrets = [s["SecretName"] for s in spec.get("Secrets") or [] if s["SecretName"].startswith(f"{prefix}_key_")]
    if old_configs == [cert["crt"]] and old_secrets == [cert["key"]]:
        return
    args = ["service", "update", "--detach=false", "--quiet"]
    for name in old_configs:
        args += ["--config-rm", name]
    for name in old_secrets:
        args += ["--secret-rm", name]
    args += [
        # Same targets and (default) modes as the stack file, so a later
        # Ansible deploy with this pair changes nothing.
        "--config-add", f"source={cert['crt']},target={config['crt_target']}",
        "--secret-add", f"source={cert['key']},target={config['key_target']}",
        config["service"],
    ]
    log(f"updating {config['service']} to {cert['crt']} (one node at a time)")
    docker(*args, timeout=1800)


def prune(cert: dict, config: dict) -> None:
    """Remove the pairs no service uses (in-use ones fail and stay)."""
    prefix = config["prefix"]
    for kind in ("config", "secret"):
        keep = cert["crt"] if kind == "config" else cert["key"]
        names = docker(kind, "ls", "--filter", f"label={LABEL}", "--format", "{{.Name}}").split()
        for name in names:
            if name != keep and name.startswith(f"{prefix}_"):
                try:
                    docker(kind, "rm", name)
                    log(f"removed {kind} {name}")
                except subprocess.CalledProcessError:
                    pass


def write_textfile(path: str, lines: list) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".edge_cert.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), mode=0o700, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def cmd_run(config: dict, here: bool, force: bool) -> int:
    if not here and not is_leader():
        log("not the Raft leader: nothing to do")
        return 0
    state = load_state()
    cert = None
    ok = True
    try:
        cert = current_cert(config)
        reason = "--force" if force else due(cert, config)
        if reason:
            log(f"new certificate needed: {reason}")
            cert = issue(config)
        else:
            days = (cert["not_after"] - time.time()) / 86400
            log(f"{cert['crt']} is fine: {days:.1f} days left")
        roll_out(cert, config)
        prune(cert, config)
        state["last_success"] = time.time()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        detail = getattr(error, "stderr", "") or ""
        log(f"FAILED: {error} {detail.strip()}")
        ok = False
    state["last_run"] = time.time()
    state["last_run_success"] = ok
    save_state(state)

    lines = [
        "# HELP edge_cert_last_run_timestamp_seconds When edge-cert last acted on this node (Raft leader).",
        "# TYPE edge_cert_last_run_timestamp_seconds gauge",
        f"edge_cert_last_run_timestamp_seconds {state['last_run']:.0f}",
        "# HELP edge_cert_last_run_success 1 if the last run on this node succeeded.",
        "# TYPE edge_cert_last_run_success gauge",
        f"edge_cert_last_run_success {int(ok)}",
        "# HELP edge_cert_last_success_timestamp_seconds When a run on this node last succeeded.",
        "# TYPE edge_cert_last_success_timestamp_seconds gauge",
        f"edge_cert_last_success_timestamp_seconds {state.get('last_success', 0):.0f}",
    ]
    if cert is not None:
        lines += [
            "# HELP edge_cert_not_after_timestamp_seconds Expiry of the certificate in the Swarm.",
            "# TYPE edge_cert_not_after_timestamp_seconds gauge",
            f"edge_cert_not_after_timestamp_seconds {cert['not_after']:.0f}",
        ]
    if os.path.isdir(config["textfile_dir"]):
        write_textfile(os.path.join(config["textfile_dir"], "edge_cert.prom"), lines)
    return 0 if ok else 1


def cmd_current(config: dict) -> int:
    cert = current_cert(config)
    print(json.dumps({"crt": cert["crt"], "key": cert["key"]} if cert else {}))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2 or args[1] not in ("run", "current") or set(args[2:]) - {"--here", "--force"}:
        print(__doc__.split("Usage:", 1)[1].strip(), file=sys.stderr)
        return 2
    with open(args[0]) as f:
        config = json.load(f)
    if args[1] == "current":
        return cmd_current(config)
    return cmd_run(config, "--here" in args, "--force" in args)


if __name__ == "__main__":
    sys.exit(main())
