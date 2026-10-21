#!/usr/bin/env python3
"""Back up the local Docker volumes of Swarm stacks with restic (role swarm_backup).

Runs on every Swarm node from a systemd timer (docs/swarm-backup.md). For each
group in the config it backs up the volumes only if one of the group's
services has a running container on this node, so every volume is backed up
once, by the node that holds the live copy. Groups with "stop" have their
services scaled to 0 for the backup and back up afterwards.

The repository and its credentials come from the environment (systemd
EnvironmentFile). Results go to a node_exporter textfile for Alloy; the alerts
SwarmBackupStale, SwarmBackupFailed and SwarmBackupCheckStale read them. The
nodes never delete snapshots: the R2 bucket lock would refuse it anyway.

Usage: swarm-backup CONFIG backup
       swarm-backup CONFIG check
"""

import json
import os
import subprocess
import sys
import tempfile
import time

STATE_FILE = "/var/lib/swarm-backup/state.json"
# Wait this long for another restic process (the weekly check, a maintenance
# prune from the workstation) to release its exclusive lock.
RETRY_LOCK = "15m"


def log(message: str) -> None:
    print(message, flush=True)


def run(*args: str, timeout: int = 600) -> str:
    return subprocess.run(
        list(args), check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def docker(*args: str, timeout: int = 300) -> str:
    return run("docker", *args, timeout=timeout)


def running_here(service: str) -> bool:
    """A container of this Swarm service runs on this node."""
    out = docker(
        "ps", "-q",
        "--filter", f"label=com.docker.swarm.service.name={service}",
        "--filter", "status=running",
    )
    return bool(out.strip())


def replicas(service: str) -> int:
    out = docker(
        "service", "inspect", service, "--format",
        "{{if .Spec.Mode.Replicated}}{{.Spec.Mode.Replicated.Replicas}}{{end}}",
    ).strip()
    if not out:
        raise RuntimeError(f"{service} is not a replicated service, can't scale it")
    return int(out)


def scale(service: str, count: int) -> None:
    # --detach=false waits until the tasks are stopped or running.
    docker("service", "scale", "--detach=false", f"{service}={count}", timeout=300)


def wait_stopped(service: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while running_here(service):
        if time.time() > deadline:
            raise RuntimeError(f"{service} still has a running container after {timeout}s")
        time.sleep(2)


def mountpoint(volume: str) -> str:
    return docker("volume", "inspect", volume, "--format", "{{.Mountpoint}}").strip()


def restic_backup(path: str, host: str, volume: str) -> dict:
    out = run(
        "restic", "backup", path,
        "--host", host,
        "--tag", "swarm-backup",
        "--tag", f"volume={volume}",
        "--retry-lock", RETRY_LOCK,
        "--json",
        timeout=3600,
    )
    for line in reversed(out.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("message_type") == "summary":
            return message
    raise RuntimeError(f"restic printed no summary for {volume}")


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def write_textfile(path: str, lines: list) -> None:
    # Atomic replace: the collector never reads a half-written file.
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".swarm_backup.", suffix=".tmp")
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


def backup_group(group: dict, host: str, state: dict) -> bool:
    name = group["name"]
    services = group["services"]
    if not any(running_here(s) for s in services):
        log(f"[{name}] no running container of {', '.join(services)} on this node: skipped")
        return True

    paths = {volume: mountpoint(volume) for volume in group["volumes"]}
    stopped = []
    ok = True
    try:
        if group.get("stop", True):
            for service in services:
                count = replicas(service)
                log(f"[{name}] stopping {service} ({count} replica(s))")
                # Listed before the scale: a scale that fails halfway is
                # still undone in finally.
                stopped.append((service, count))
                scale(service, 0)
                wait_stopped(service)
        for volume, path in paths.items():
            start = time.time()
            summary = restic_backup(path, host, volume)
            duration = time.time() - start
            state.setdefault("volumes", {})[volume] = {
                "last_success": time.time(),
                "duration": duration,
                "size_bytes": summary.get("total_bytes_processed", 0),
                "added_bytes": summary.get("data_added", 0),
                "snapshot": summary.get("snapshot_id", ""),
            }
            log(
                f"[{name}] {volume}: snapshot {summary.get('snapshot_id', '?')[:8]}, "
                f"{summary.get('total_bytes_processed', 0)} bytes, "
                f"{summary.get('data_added', 0)} added, {duration:.0f}s"
            )
    except (subprocess.SubprocessError, RuntimeError, OSError) as error:
        detail = getattr(error, "stderr", "") or ""
        log(f"[{name}] FAILED: {error} {detail.strip()}")
        ok = False
    finally:
        for service, count in reversed(stopped):
            try:
                log(f"[{name}] starting {service} ({count} replica(s))")
                scale(service, count)
            except (subprocess.SubprocessError, OSError) as error:
                detail = getattr(error, "stderr", "") or ""
                log(f"[{name}] FAILED to start {service} again: {error} {detail.strip()}")
                ok = False
    return ok


def cmd_backup(config: dict) -> int:
    host = config["host"]
    state = load_state()
    started = time.time()
    results = [backup_group(group, host, state) for group in config["groups"]]
    ok = all(results)
    state["last_run"] = time.time()
    state["last_run_success"] = ok
    state["last_run_duration"] = time.time() - started
    save_state(state)

    # Only volumes still in the config: a group removed from Ansible stops
    # reporting instead of turning stale forever.
    configured = {v for group in config["groups"] for v in group["volumes"]}
    volumes = {v: s for v, s in state.get("volumes", {}).items() if v in configured}
    metrics = {
        "swarm_backup_volume_last_success_timestamp_seconds": ("When the volume was last backed up from this node.", "last_success"),
        "swarm_backup_volume_duration_seconds": ("How long its last backup took.", "duration"),
        "swarm_backup_volume_size_bytes": ("Size of the volume at its last backup.", "size_bytes"),
        "swarm_backup_volume_added_bytes": ("New data its last backup uploaded (after dedup and compression).", "added_bytes"),
    }
    lines = []
    for metric, (help_text, key) in metrics.items():
        lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"]
        for volume, values in sorted(volumes.items()):
            lines.append(f'{metric}{{volume="{label(volume)}"}} {values.get(key, 0):.0f}')
    lines += [
        "# HELP swarm_backup_last_run_timestamp_seconds When swarm-backup last ran on this node.",
        "# TYPE swarm_backup_last_run_timestamp_seconds gauge",
        f"swarm_backup_last_run_timestamp_seconds {state['last_run']:.0f}",
        "# HELP swarm_backup_last_run_success 1 if every group of the last run succeeded.",
        "# TYPE swarm_backup_last_run_success gauge",
        f"swarm_backup_last_run_success {int(ok)}",
        "# HELP swarm_backup_last_run_duration_seconds How long the last run took, downtime included.",
        "# TYPE swarm_backup_last_run_duration_seconds gauge",
        f"swarm_backup_last_run_duration_seconds {state['last_run_duration']:.0f}",
    ]
    write_textfile(os.path.join(config["textfile_dir"], "swarm_backup.prom"), lines)
    return 0 if ok else 1


def cmd_check(config: dict) -> int:
    state = load_state()
    check = state.setdefault("check", {})
    try:
        run(
            "restic", "check",
            "--read-data-subset", config["check_read_data_subset"],
            "--retry-lock", RETRY_LOCK,
            timeout=6 * 3600,
        )
        ok = True
        check["last_success"] = time.time()
        log("restic check: no errors")
    except subprocess.CalledProcessError as error:
        ok = False
        log(f"restic check FAILED (exit {error.returncode}):\n{error.stdout}\n{error.stderr}")
    check["last_run_success"] = ok
    save_state(state)
    write_textfile(
        os.path.join(config["textfile_dir"], "swarm_backup_check.prom"),
        [
            "# HELP swarm_backup_check_last_success_timestamp_seconds When restic check last passed.",
            "# TYPE swarm_backup_check_last_success_timestamp_seconds gauge",
            f"swarm_backup_check_last_success_timestamp_seconds {check.get('last_success', 0):.0f}",
            "# HELP swarm_backup_check_last_run_success 1 if the last restic check passed.",
            "# TYPE swarm_backup_check_last_run_success gauge",
            f"swarm_backup_check_last_run_success {int(ok)}",
        ],
    )
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in ("backup", "check"):
        print("\n".join(__doc__.strip().splitlines()[-2:]), file=sys.stderr)
        return 2
    with open(sys.argv[1]) as f:
        config = json.load(f)
    return cmd_backup(config) if sys.argv[2] == "backup" else cmd_check(config)


if __name__ == "__main__":
    sys.exit(main())
