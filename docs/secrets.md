# Secrets (SOPS + age)

Secrets are committed encrypted, as `SopsSecret` manifests named `*.sops.yaml`. sops-secrets-operator decrypts them in the cluster (ADR 0006). This page covers the day-to-day tasks.

## One-time: the age key

```bash
devbox shell                              # sets SOPS_AGE_KEY_FILE=.artifacts/age.key
age-keygen -o .artifacts/age.key          # prints the public key (age1...)
chmod 600 .artifacts/age.key
age-keygen -y .artifacts/age.key          # show the public key again
```

1. Put the public key into `.sops.yaml` (replace `AGE_PUBLIC_KEY`) and commit it. The public key is not secret.
2. **Back up the private key** outside this machine, for example in a password manager and on an offline USB stick. Without it, no secret in this repo can be decrypted.
3. Load it into the cluster:

   ```bash
   make sops-key       # runs playbooks/sops.yml (also part of make site)
   ```

## Add a secret

Pipe the plaintext straight into `sops`, so it never touches the disk. Example: the Cloudflare API token for cert-manager.

```bash
f=kubernetes/platform/cert-manager/cloudflare-api-token.sops.yaml
mkdir -p "$(dirname "$f")"
read -rsp 'Cloudflare API token: ' CF_API_TOKEN; echo

sops encrypt --filename-override "$f" --input-type yaml --output-type yaml /dev/stdin > "$f" <<YAML
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: cloudflare-api-token
  namespace: cert-manager
spec:
  secretTemplates:
    - name: cloudflare-api-token
      stringData:
        api-token: "${CF_API_TOKEN}"
YAML
unset CF_API_TOKEN

make sops-check     # confirms the file is encrypted
```

`--filename-override` makes sops pick the creation rule in `.sops.yaml` for the target path.

The `SopsSecret` must be part of an Argo CD Application (its `path` or `sources`). That Application needs `SkipDryRunOnMissingResource=true` in `syncOptions`, so it does not fail on a fresh cluster before the CRD exists.

## Edit or read a secret

```bash
sops edit kubernetes/platform/cert-manager/cloudflare-api-token.sops.yaml     # opens $EDITOR, re-encrypts on save
sops decrypt kubernetes/platform/cert-manager/cloudflare-api-token.sops.yaml  # print to stdout
```

## Check it in the cluster

```bash
kubectl get sopssecrets -A
kubectl -n cert-manager get secret cloudflare-api-token
kubectl -n sops logs deploy/sops-secrets-operator
```

## Rotate the age key

1. `age-keygen -o .artifacts/age.key.new` and put its public key into `.sops.yaml` in place of the old one.
2. Re-encrypt every file. `SOPS_AGE_KEY_FILE` still points at the old key, which sops needs to decrypt:

   ```bash
   for f in $(git ls-files 'kubernetes/*.sops.yaml'); do sops updatekeys -y "$f"; done
   ```

3. `mv .artifacts/age.key.new .artifacts/age.key`, run `make sops-key`, update the backup, commit.

If the old key leaked, rotate the secrets themselves too (new Cloudflare token and so on): the old ciphertext is still in the Git history.
