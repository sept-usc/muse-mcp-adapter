locals {
  id          = "mma-${var.name}"
  secret_name = var.tailscale_auth_key_secret != null ? var.tailscale_auth_key_secret : try(google_secret_manager_secret.key[0].secret_id, "")
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

resource "google_project_service" "apis" {
  for_each           = toset(["compute.googleapis.com", "iap.googleapis.com", "secretmanager.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

# ---- the network: its own, with no inbound path except SSH through Identity-Aware Proxy ---------
resource "google_compute_network" "vpc" {
  name                    = local.id
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name                     = local.id
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "${local.id}-iap-ssh"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  source_ranges = var.iap_ssh_ranges
  target_tags   = [local.id]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# No public address: outbound only, through Cloud NAT. Muse reaches the adapter through Tailscale Funnel.
resource "google_compute_router" "router" {
  name    = local.id
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = local.id
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# ---- the Tailscale auth key, read once at first boot --------------------------------------------
resource "google_secret_manager_secret" "key" {
  count     = var.tailscale_auth_key != null ? 1 : 0
  secret_id = "${local.id}-tailscale-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "key" {
  count       = var.tailscale_auth_key != null ? 1 : 0
  secret      = google_secret_manager_secret.key[0].id
  secret_data = var.tailscale_auth_key
}

# The VM's identity can read that one secret and nothing else. The adapter container cannot reach
# the metadata server (the egress guard blocks it), so it cannot use this identity.
resource "google_service_account" "installer" {
  account_id   = "${local.id}-install"
  display_name = "Reads the Tailscale auth key once, at first boot (${local.id})"
}

resource "google_secret_manager_secret_iam_member" "reader" {
  secret_id  = local.secret_name
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${google_service_account.installer.email}"
  depends_on = [google_secret_manager_secret.key]
}

# ---- the VM ---------------------------------------------------------------------------------------
resource "google_compute_instance" "vm" {
  name         = local.id
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.id]
  labels       = { app = "muse-mcp-adapter", mma-deployment = var.name }

  boot_disk {
    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
      size  = var.disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id # no access_config: no public address
  }

  service_account {
    email  = google_service_account.installer.email
    scopes = ["cloud-platform"] # limited by IAM to the one secret
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script         = file("${path.module}/startup.sh")
    muse-name              = var.name
    muse-install-source    = var.install_source
    muse-tailscale-secret  = local.secret_name
    muse-egress-allow      = join(",", var.egress_allow)
  }

  lifecycle {
    precondition {
      condition     = (var.tailscale_auth_key_secret != null) != (var.tailscale_auth_key != null)
      error_message = "Give exactly one of tailscale_auth_key_secret (recommended) or tailscale_auth_key."
    }
  }

  depends_on = [
    google_compute_router_nat.nat,
    google_secret_manager_secret_iam_member.reader,
    google_secret_manager_secret_version.key,
  ]
}

resource "google_compute_resource_policy" "snapshots" {
  count  = var.snapshots ? 1 : 0
  name   = "${local.id}-daily"
  region = var.region
  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "03:00"
      }
    }
    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
  }
}

resource "google_compute_disk_resource_policy_attachment" "snapshots" {
  count = var.snapshots ? 1 : 0
  name  = google_compute_resource_policy.snapshots[0].name
  disk  = google_compute_instance.vm.name
  zone  = var.zone
}
