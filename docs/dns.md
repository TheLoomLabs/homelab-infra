# Lab DNS (BIND)

`dns-01` (`192.168.1.11`, primary) and `dns-02` (`192.168.1.12`, secondary) serve `lab.theloomlabs.org` and resolve everything else for the house (ADR 0008). The Starlink router's Custom DNS points at both.

| What | Where |
|---|---|
| Extra records (wildcard, `pve`, `k8s-api`, …) | `dns/records.yml` |
| Inventory hosts | automatic A + PTR from `ansible/inventory/hosts.yml` (Terraform) |
| TSIG secret (zone transfers) | `dns/tsig.sops.yaml` (SOPS) |
| Role and templates | `ansible/roles/bind` |
| Resolvers of the lab VMs | `dns_servers` / `upstream_dns_servers` in `terraform.tfvars`, applied by role `common` |

## Add or change a record

1. Edit `dns/records.yml`:

   ```yaml
   bind_records:
     - {name: nas, type: A, value: 192.168.1.50}
     - {name: git, type: CNAME, value: forgejo}
     - {name: short-ttl, type: A, value: 192.168.1.51, ttl: 60}
   ```

   Names are relative to the zone. Apps behind the Gateway need no record, because the `*` wildcard already points at `192.168.1.61`. A wildcard never matches a name that exists in the zone, and that includes empty nodes: a record `ssh.git` would make `git` exist (with no data), and `git.lab.theloomlabs.org` would stop resolving. Non-HTTP names get a flat name instead (`git-ssh`).

2. Deploy and check:

   ```bash
   make dns
   dig @192.168.1.12 +short nas.lab.theloomlabs.org
   ```

   The role bumps the SOA serial, validates the zone with `named-checkzone`, reloads the primary and waits until every secondary serves the new serial. A second `make dns` reports no changes.

3. Commit `dns/records.yml`.

## Check that it works

```bash
dig @192.168.1.11 +short argocd.lab.theloomlabs.org          # 192.168.1.61 (wildcard)
dig @192.168.1.12 +short k8s-cp-01.lab.theloomlabs.org       # secondary has the zone
dig @192.168.1.12 +short -x 192.168.1.200                    # PTR
dig @192.168.1.11 +short theloomlabs.org                     # recursion (public website)
dig @192.168.1.11 dnssec-failed.org | grep status            # SERVFAIL = DNSSEC validation works
dig @192.168.1.11 lab.theloomlabs.org AXFR | tail -1         # Transfer failed. (no TSIG key)
dig +short +norecurse @192.168.1.11 lab.theloomlabs.org SOA  # same serial on both servers
dig +short +norecurse @192.168.1.12 lab.theloomlabs.org SOA
```

On the servers: `journalctl -u named -f` shows NOTIFY, transfers and errors.

## When DNS is down

The whole house resolves through these two VMs.

1. **Restore the house first:** turn **Custom DNS off** in the Starlink app (Settings → Custom DNS). Clients fall back to Starlink's own DNS within a minute. Lab names stop resolving, everything else works.
2. Find the cause: is `pve` up, are the VMs running (`qm list` on `pve`), is `named` running (`systemctl status named`)?
3. Fix it and redeploy (`make dns`), then run the checks above and turn Custom DNS back on with `192.168.1.11` and `192.168.1.12`.

Never put a public resolver as the second Custom DNS entry. Clients pick either server, so lab names would fail at random.

A secondary keeps answering for two weeks (SOA expire) while the primary is down. Only changes need the primary.

## Rotate the TSIG key

```bash
printf 'bind_tsig_secret: %s\n' "$(head -c 32 /dev/urandom | base64)" > dns/tsig.sops.yaml
sops encrypt --in-place dns/tsig.sops.yaml
make sops-check && make dns
```

Both servers get the new key in the same run. Commit the file.

## Lab VM resolvers

The lab VMs resolve through `dns-01` and `dns-02`. The DNS VMs themselves use the public resolvers (`upstream_dns_servers`).

- **No search domain.** The zone has a wildcard (`*` → the Gateway), so with `lab.theloomlabs.org` as a search domain every name that doesn't exist would still resolve as `<name>.lab.theloomlabs.org`. Kubernetes pods use `ndots:5` and try the search domains before the name itself, so an external name like `<account>.r2.cloudflarestorage.com` resolved to `192.168.1.61` (Longhorn's backups to R2 failed with "connection reset by peer"). Use full names, e.g. `ssh ops@k8s-cp-01.lab.theloomlabs.org`.
- Pods get the node's search domains when they start. After a change, restart the pods that resolve external names.

- Cloud-init writes the network config once, on first boot. Terraform ignores cloud-init changes on existing VMs, because changing them would recreate the VM (ADR 0008).
- The role `common` rewrites the `nameservers` in `/etc/netplan/50-cloud-init.yaml`, stops cloud-init from regenerating it, and applies the change with `resolvectl`. It never runs `netplan apply`, which can drop addresses netplan doesn't know about, such as the kube-vip VIP.
- After changing the resolvers, restart CoreDNS so pods use them: `kubectl -n kube-system rollout restart deployment coredns`.

## Gotchas

- **Snippet uploads need SSH to `pve` through the ssh-agent.** The Proxmox provider uploads cloud-init snippets over SSH as `root`, using only the ssh-agent (it ignores `~/.ssh/config`). Run `ssh-add -L` before any apply that changes snippets. If the agent is not reachable, Terraform deletes the old snippets and then fails to upload the new ones, and the VMs can't start again until they are back. Upload with `-parallelism=2`, because many SSH connections at once make `sshd` on `pve` drop some.
- The DNS-01 check of cert-manager uses public resolvers on purpose: BIND answers the lab zone itself and doesn't know the `_acme-challenge` records in Cloudflare.
