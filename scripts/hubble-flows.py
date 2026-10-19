#!/usr/bin/env python3
"""Summarise Hubble flows into one line per connection (docs/network-policy.md).

Reads `hubble observe -o json` output (a file or stdin) and prints every
distinct connection: source -> destination, port, verdict and count. Replies
are skipped, so each line is one direction a policy has to allow. Endpoints are
named namespace/app (app from the usual labels, else the pod name without its
hash); everything outside the pods by its Cilium identity (host, remote-node,
kube-apiserver, ingress, world), with the IP and, when the DNS proxy saw it,
the name. Used to draft the CiliumNetworkPolicies and to review AUDIT verdicts
before enforcing them.

    hubble observe --follow -o json --namespace monitoring > flows.json
    scripts/hubble-flows.py flows.json
    scripts/hubble-flows.py --verdict AUDIT flows.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys

RESERVED = re.compile(r"^reserved:(.+)$")


def labels_of(ep: dict) -> dict[str, str]:
    out = {}
    for item in ep.get("labels") or []:
        src_key, _, value = item.partition("=")
        out[src_key.split(":", 1)[-1]] = value
    return out


def reserved_of(ep: dict) -> str | None:
    for item in ep.get("labels") or []:
        m = RESERVED.match(item)
        if m:
            return m.group(1)
    return None


def name_of(ep: dict, ip: str, names: list[str]) -> str:
    ns = ep.get("namespace")
    if ns:
        labels = labels_of(ep)
        name = labels.get("app.kubernetes.io/name") or labels.get("app") or labels.get("k8s-app") \
            or labels.get("name") or re.sub(r"(-[a-z0-9]{8,10})?-[a-z0-9]{5}$", "", ep.get("pod_name", "?"))
        comp = labels.get("app.kubernetes.io/component")
        return f"{ns}/{name}" + (f"[{comp}]" if comp and comp != name else "")
    res = reserved_of(ep) or f"identity:{ep.get('identity', '?')}"
    if res in ("world", "world-ipv4", "world-ipv6", "unmanaged", "remote-node", "host") or res.startswith("identity:"):
        extra = ",".join(names) if names else ip
        return f"{res}({extra})"
    return res


def port_of(flow: dict) -> str:
    l4 = flow.get("l4") or {}
    for proto in ("TCP", "UDP", "SCTP"):
        if proto in l4:
            return f"{l4[proto].get('destination_port', '?')}/{proto}"
    if "ICMPv4" in l4 or "ICMPv6" in l4:
        return "icmp"
    return "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", help="hubble observe -o json output (default: stdin)")
    ap.add_argument("--verdict", help="only this verdict (FORWARDED, DROPPED, AUDIT, ...)")
    args = ap.parse_args()

    fh = open(args.file) if args.file else sys.stdin
    seen: collections.Counter[tuple[str, str, str, str]] = collections.Counter()
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            flow = json.loads(line).get("flow") or {}
        except json.JSONDecodeError:
            continue
        if not flow or flow.get("is_reply"):
            continue
        verdict = flow.get("verdict", "?")
        if args.verdict and verdict != args.verdict:
            continue
        # Only the first packet of a TCP connection; everything for UDP/ICMP.
        tcp = (flow.get("l4") or {}).get("TCP")
        if tcp and not (tcp.get("flags") or {}).get("SYN"):
            continue
        ip = flow.get("IP") or {}
        src = name_of(flow.get("source") or {}, ip.get("source", "?"), flow.get("source_names") or [])
        dst = name_of(flow.get("destination") or {}, ip.get("destination", "?"),
                      flow.get("destination_names") or [])
        seen[(src, dst, port_of(flow), verdict)] += 1

    rows = sorted(seen.items(), key=lambda kv: kv[0])
    width_src = max([len(k[0]) for k, _ in rows] + [6])
    width_dst = max([len(k[1]) for k, _ in rows] + [11])
    print(f"{'SOURCE':<{width_src}}  {'DESTINATION':<{width_dst}}  {'PORT':<10} {'VERDICT':<10} COUNT")
    for (src, dst, port, verdict), n in rows:
        print(f"{src:<{width_src}}  {dst:<{width_dst}}  {port:<10} {verdict:<10} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
