#!/usr/bin/env python3
"""Summarise Hubble flows into one line per connection (docs/network-policy.md).

Reads `hubble observe -o json` output (a file or stdin) and prints every
distinct connection: source -> destination, port, verdict and count. Replies
are skipped, so each line is one direction a policy has to allow. Endpoints are
named namespace/app (app from the usual labels, else the pod name without its
hash); everything outside the pods by its Cilium identity (host, remote-node,
kube-apiserver, ingress, world), with the IP and, when the DNS proxy saw it,
the name. Drops carry their reason (DROPPED:POLICY_DENIED); LAST is when the
connection was last seen (UTC), so leftovers from a rollout stand out. Used to
draft the CiliumNetworkPolicies and to review AUDIT verdicts before enforcing
them.

    hubble observe --follow -o json --namespace monitoring > flows.json
    scripts/hubble-flows.py flows.json
    scripts/hubble-flows.py --verdict AUDIT flows.json
    scripts/hubble-flows.py --since 24h policy-verdicts.log
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
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


def flow_time(flow: dict) -> dt.datetime | None:
    """Hubble's RFC 3339 time with nanoseconds, as an aware datetime."""
    raw = flow.get("time")
    if not raw:
        return None
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", raw)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return dt.datetime.fromisoformat(m.group(1) + frac + tz)


def duration(text: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", text)
    if not m:
        raise argparse.ArgumentTypeError("use <number>m, <number>h or <number>d")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    return dt.timedelta(**{unit: int(m.group(1))})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", help="hubble observe -o json output (default: stdin)")
    ap.add_argument("--verdict", help="only this verdict (FORWARDED, DROPPED, AUDIT, ...)")
    ap.add_argument("--since", type=duration, help="only flows newer than this (30m, 6h, 2d)")
    args = ap.parse_args()

    fh = open(args.file) if args.file else sys.stdin
    seen: collections.Counter[tuple[str, str, str, str]] = collections.Counter()
    last: dict[tuple[str, str, str, str], dt.datetime] = {}
    cutoff = dt.datetime.now(dt.timezone.utc) - args.since if args.since else None
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
        when = flow_time(flow)
        if cutoff and (when is None or when < cutoff):
            continue
        if verdict == "DROPPED" and flow.get("drop_reason_desc"):
            verdict = f"DROPPED:{flow['drop_reason_desc']}"
        # Only the first packet of a TCP connection; everything for UDP/ICMP.
        tcp = (flow.get("l4") or {}).get("TCP")
        if tcp and not (tcp.get("flags") or {}).get("SYN"):
            continue
        ip = flow.get("IP") or {}
        src = name_of(flow.get("source") or {}, ip.get("source", "?"), flow.get("source_names") or [])
        dst = name_of(flow.get("destination") or {}, ip.get("destination", "?"),
                      flow.get("destination_names") or [])
        key = (src, dst, port_of(flow), verdict)
        seen[key] += 1
        if when and (key not in last or when > last[key]):
            last[key] = when

    rows = sorted(seen.items(), key=lambda kv: kv[0])
    width_src = max([len(k[0]) for k, _ in rows] + [6])
    width_dst = max([len(k[1]) for k, _ in rows] + [11])
    width_v = max([len(k[3]) for k, _ in rows] + [7])
    print(f"{'SOURCE':<{width_src}}  {'DESTINATION':<{width_dst}}  {'PORT':<10} {'VERDICT':<{width_v}}  "
          f"{'COUNT':>6}  LAST")
    for key, n in rows:
        src, dst, port, verdict = key
        seen_at = last[key].strftime("%Y-%m-%d %H:%M") if key in last else "-"
        print(f"{src:<{width_src}}  {dst:<{width_dst}}  {port:<10} {verdict:<{width_v}}  {n:>6}  {seen_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
