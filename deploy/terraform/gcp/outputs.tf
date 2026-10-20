output "instance" {
  description = "The VM's name."
  value       = google_compute_instance.vm.name
}

output "watch_the_install" {
  description = "Follow the first-boot install (10 to 20 minutes). It ends with the adapter's address."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --project ${var.project} --zone ${var.zone} --tunnel-through-iap --command 'sudo tail -f /var/log/muse-vm-install.log'"
}

output "connect_a_server" {
  description = "Once the install has finished, connect an MCP server (run from your computer; it asks for a token if the server needs one)."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --project ${var.project} --zone ${var.zone} --tunnel-through-iap -- -t sudo muse-vm add-server github --name ${var.name}"
}
