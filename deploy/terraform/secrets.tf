locals {
  secret_ids = [
    "SYNTHETIC_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DASHBOARD_PASSWORD",
    "DATABASE_URL",
  ]
}

resource "google_secret_manager_secret" "app" {
  for_each  = toset(local.secret_ids)
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.baloo_run.email}"
}
