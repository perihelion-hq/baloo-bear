resource "google_service_account" "baloo_run" {
  account_id   = "rocky-pi-run"
  display_name = "rocky-pi Cloud Run runtime"
}

resource "google_project_iam_member" "cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.baloo_run.email}"
}
