variable "project" {
  description = "Google Cloud project ID."
  type        = string
}

variable "name" {
  description = "Deployment name; prefixes every resource."
  type        = string
  default     = "muse-adapter"
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

variable "ingress" {
  description = "caddy gives the VM a public IP and opens 80/443; tailscale-funnel and cloudflare-tunnel need no inbound access and no public IP."
  type        = string
  default     = "tailscale-funnel"
  validation {
    condition     = contains(["caddy", "tailscale-funnel", "cloudflare-tunnel"], var.ingress)
    error_message = "ingress must be caddy, tailscale-funnel or cloudflare-tunnel."
  }
}

variable "ssh_source_ranges" {
  description = "Source ranges allowed to SSH. The default is Google's IAP range, so SSH is only reachable through IAP."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}
