#!/usr/bin/env python3
"""Desired and running replicas of every Swarm service, as Prometheus metrics.

Writes a node_exporter textfile (role alloy, docs/monitoring.md). Alloy serves
it with the host metrics; the SwarmServiceReplicasMismatch alert compares the
two. Runs on the managers (only they can list services) from a systemd timer.
On an error the old file stays, and swarm_services_last_success_timestamp_seconds
stops moving (alert SwarmServiceMetricsStale).

Usage: swarm-services-metrics OUTPUT_FILE
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

# "1/1", "3/3" (global), "0/1 (1/1 completed)" (jobs).
REPLICAS = re.compile(r"^(\d+)/(\d+)")


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    output = sys.argv[1]

    services = [
        json.loads(line)
        for line in docker("service", "ls", "--format", "{{json .}}").splitlines()
        if line.strip()
    ]
    stacks = {}
    if services:
        for spec in json.loads(docker("service", "inspect", *[s["ID"] for s in services])):
            labels = spec["Spec"].get("Labels") or {}
            stacks[spec["Spec"]["Name"]] = labels.get("com.docker.stack.namespace", "")

    lines = [
        "# HELP swarm_service_replicas_desired Tasks the Swarm service should run.",
        "# TYPE swarm_service_replicas_desired gauge",
        "# HELP swarm_service_replicas_running Tasks of the Swarm service that are running.",
        "# TYPE swarm_service_replicas_running gauge",
    ]
    desired, running = [], []
    for service in sorted(services, key=lambda s: s["Name"]):
        match = REPLICAS.match(service.get("Replicas", ""))
        if not match:
            continue
        labels = 'stack="{}",service="{}",mode="{}"'.format(
            label(stacks.get(service["Name"], "")),
            label(service["Name"]),
            label(service.get("Mode", "")),
        )
        running.append(f"swarm_service_replicas_running{{{labels}}} {match.group(1)}")
        desired.append(f"swarm_service_replicas_desired{{{labels}}} {match.group(2)}")
    lines[2:2] = desired
    lines += running
    lines += [
        "# HELP swarm_services_last_success_timestamp_seconds When the services were last listed.",
        "# TYPE swarm_services_last_success_timestamp_seconds gauge",
        f"swarm_services_last_success_timestamp_seconds {time.time():.0f}",
    ]

    # Atomic replace: the collector never reads a half-written file.
    directory = os.path.dirname(os.path.abspath(output))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".swarm_services.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, output)
    except BaseException:
        os.unlink(tmp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
