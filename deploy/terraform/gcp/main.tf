locals {
  public = var.ingress == "caddy"
}

resource "google_compute_network" "vpc" {
  name                    = "mma-${var.name}"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "mma-${var.name}"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_firewall" "ssh_iap" {
  name          = "mma-${var.name}-ssh-iap"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  source_ranges = var.ssh_source_ranges
  target_tags   = ["mma-${var.name}"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "web" {
  count         = local.public ? 1 : 0
  name          = "mma-${var.name}-web"
  network       = google_compute_network.vpc.name
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["mma-${var.name}"]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_address" "ip" {
  count  = local.public ? 1 : 0
  name   = "mma-${var.name}"
  region = var.region
}

resource "google_compute_router" "router" {
  count   = local.public ? 0 : 1
  name    = "mma-${var.name}"
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  count                              = local.public ? 0 : 1
  name                               = "mma-${var.name}"
  router                             = google_compute_router.router[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_instance" "vm" {
  name         = "mma-${var.name}"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["mma-${var.name}"]
  labels       = { app = "muse-mcp-adapter", deployment = var.name }

  boot_disk {
    initialize_params {
      image = "projects/debian-cloud/global/images/family/debian-12"
      size  = var.disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    dynamic "access_config" {
      for_each = local.public ? [1] : []
      content {
        nat_ip = google_compute_address.ip[0].address
      }
    }
  }

  # No service account: nothing on the VM can use cloud credentials.
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
  }

  depends_on = [google_compute_router_nat.nat]
}

resource "google_compute_resource_policy" "snapshots" {
  name   = "mma-${var.name}-daily"
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
  name = google_compute_resource_policy.snapshots.name
  disk = google_compute_instance.vm.name
  zone = var.zone
}
