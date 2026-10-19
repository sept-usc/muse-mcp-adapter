variable "project" {
  description = "Google Cloud project to create the adapter in."
  type        = string
}

variable "name" {
  description = "Deployment name. Resources are named mma-<name>."
  type        = string
  default     = "muse"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,20}$", var.name))
    error_message = "name must start with a letter and use lowercase letters, digits and hyphens (21 characters at most)."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "machine_type" {
  type    = string
  default = "e2-small"
}

variable "disk_gb" {
  type    = number
  default = 20
}

variable "install_source" {
  description = "What pip installs on the VM. Pin a release or commit for production, for example git+https://github.com/sept-usc/muse-mcp-adapter@<commit>."
  type        = string
  default     = "git+https://github.com/sept-usc/muse-mcp-adapter"
}

variable "tailscale_auth_key_secret" {
  description = "Name of an existing Secret Manager secret that holds a Tailscale auth key (recommended: the key never enters Terraform state). Use a single-use, untagged key that expires soon."
  type        = string
  default     = null
}

variable "tailscale_auth_key" {
  description = "A Tailscale auth key, if you would rather have Terraform store it in Secret Manager for you. It is then in the Terraform state, so protect the state."
  type        = string
  default     = null
  sensitive   = true
}

variable "egress_allow" {
  description = "Private CIDR ranges the adapter may reach (default: none)."
  type        = list(string)
  default     = []
}

variable "snapshots" {
  description = "Daily disk snapshots, kept for 7 days."
  type        = bool
  default     = true
}

variable "iap_ssh_ranges" {
  description = "Source ranges allowed to SSH: Google's Identity-Aware Proxy range, so SSH is only reachable through IAP."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}
