output "instance" {
  value = google_compute_instance.vm.name
}

output "public_ip" {
  value = local.public ? google_compute_address.ip[0].address : null
}

output "next_step" {
  value = "Install the adapter on the VM with: muse-vm up ssh --host <user>@<vm> --ingress ${var.ingress}"
}
