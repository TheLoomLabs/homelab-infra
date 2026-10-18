SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

TF_DIR      := terraform/envs/homelab
ANSIBLE_DIR := ansible
PLAYBOOK    := cd $(ANSIBLE_DIR) && ansible-playbook

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

## --- Setup -------------------------------------------------------------------

.PHONY: setup
setup: ## Install Ansible collections, pre-commit hooks, init Terraform
	cd $(ANSIBLE_DIR) && ansible-galaxy collection install -r requirements.yml
	pre-commit install
	terraform -chdir=$(TF_DIR) init -input=false

## --- Terraform ---------------------------------------------------------------

.PHONY: tf-init tf-plan tf-apply tf-destroy
tf-init: ## Initialise Terraform (providers + backend)
	terraform -chdir=$(TF_DIR) init -input=false

tf-plan: tf-init ## Plan the Proxmox VMs
	terraform -chdir=$(TF_DIR) plan -out=tfplan

tf-apply: ## Apply the saved plan (creates VMs + Ansible inventory)
	terraform -chdir=$(TF_DIR) apply tfplan

tf-destroy: ## Destroy all lab VMs
	terraform -chdir=$(TF_DIR) destroy

## --- Ansible -----------------------------------------------------------------

.PHONY: ping base dns k8s cilium longhorn sops-key argocd swarm alerting logs site
ping: ## Check SSH to every node
	cd $(ANSIBLE_DIR) && ansible all -m ansible.builtin.ping

base: ## OS baseline + hardening on every node
	$(PLAYBOOK) playbooks/base.yml

dns: ## Deploy BIND on dns-01/dns-02 (zone data: dns/records.yml)
	$(PLAYBOOK) playbooks/dns.yml

k8s: ## Build the Kubernetes cluster (kubeadm, kube-vip, Cilium)
	$(PLAYBOOK) playbooks/kubernetes.yml

cilium: ## Re-apply Cilium Helm values (cilium upgrade if they changed)
	$(PLAYBOOK) playbooks/cilium.yml

longhorn: ## Prepare the workers for Longhorn (packages, data disk, node label)
	$(PLAYBOOK) playbooks/longhorn.yml

sops-key: ## Load the age private key into the cluster (SOPS)
	$(PLAYBOOK) playbooks/sops.yml

argocd: ## Bootstrap Argo CD and the root app (push kubernetes/ first)
	$(PLAYBOOK) playbooks/argocd.yml

swarm: ## Build the Docker Swarm cluster
	$(PLAYBOOK) playbooks/swarm.yml

alerting: ## Deploy the Swarm alerting stack (Traefik, ntfy, healthchecks)
	$(PLAYBOOK) playbooks/alerting.yml

logs: ## Ship the DNS and Swarm VMs' logs to Loki (Alloy)
	$(PLAYBOOK) playbooks/logs.yml

site: ## Everything: base + Kubernetes + Swarm
	$(PLAYBOOK) playbooks/site.yml

## --- Quality -----------------------------------------------------------------

.PHONY: fmt sops-check secrets manifests lint
fmt: ## Format Terraform code
	terraform fmt -recursive terraform

sops-check: ## Fail if a *.sops.yaml manifest is not encrypted
	@rc=0; for f in $$(git ls-files --cached --others --exclude-standard 'kubernetes/*.sops.yaml' 'dns/*.sops.yaml' 'swarm/*.sops.yaml' 'logging/*.sops.yaml'); do \
		grep -q '^sops:' "$$f" || { echo "not encrypted: $$f"; rc=1; }; \
	done; exit $$rc

secrets: ## Scan the full git history for leaked secrets (gitleaks)
	gitleaks git --redact --verbose --exit-code 1 .

manifests: ## Render every Argo CD app and validate it (kubeconform, promtool)
	python3 scripts/check-manifests.py

lint: ## Run all linters (same as CI)
	terraform fmt -check -recursive terraform
	cd $(TF_DIR) && TF_DATA_DIR=.terraform-lint terraform init -backend=false -input=false >/dev/null \
		&& TF_DATA_DIR=.terraform-lint terraform validate
	tflint --init && tflint --recursive --config "$(CURDIR)/.tflint.hcl"
	yamllint .
	cd $(ANSIBLE_DIR) && ansible-lint
	$(MAKE) manifests
	$(MAKE) sops-check
	$(MAKE) secrets
