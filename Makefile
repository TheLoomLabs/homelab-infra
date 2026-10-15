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

.PHONY: ping base k8s swarm site
ping: ## Check SSH to every node
	cd $(ANSIBLE_DIR) && ansible all -m ansible.builtin.ping

base: ## OS baseline + hardening on every node
	$(PLAYBOOK) playbooks/base.yml

k8s: ## Build the Kubernetes cluster (kubeadm, kube-vip, Cilium)
	$(PLAYBOOK) playbooks/kubernetes.yml

swarm: ## Build the Docker Swarm cluster
	$(PLAYBOOK) playbooks/swarm.yml

site: ## Everything: base + Kubernetes + Swarm
	$(PLAYBOOK) playbooks/site.yml

## --- Quality -----------------------------------------------------------------

.PHONY: fmt secrets lint
fmt: ## Format Terraform code
	terraform fmt -recursive terraform

secrets: ## Scan the full git history for leaked secrets (gitleaks)
	gitleaks git --redact --verbose --exit-code 1 .

lint: ## Run all linters (same as CI)
	terraform fmt -check -recursive terraform
	cd $(TF_DIR) && TF_DATA_DIR=.terraform-lint terraform init -backend=false -input=false >/dev/null \
		&& TF_DATA_DIR=.terraform-lint terraform validate
	tflint --init && tflint --recursive --config "$(CURDIR)/.tflint.hcl"
	yamllint .
	cd $(ANSIBLE_DIR) && ansible-lint
	$(MAKE) secrets