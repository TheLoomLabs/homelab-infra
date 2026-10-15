terraform {
  required_version = ">= 1.9"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.116"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }

  # State stays local for the homelab. For a team setup, swap this for an S3-
  # compatible backend (e.g. MinIO) with locking.
  backend "local" {
    path = "terraform.tfstate"
  }
}
