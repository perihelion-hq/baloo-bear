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

# DATABASE_URL is derived from the single-source password (cloudsql.tf), so it
# can never drift from the Cloud SQL user. asyncpg form: the app uses
# create_async_engine; alembic strips "+asyncpg" for the sync migration engine.
# Cloud SQL unix socket via ?host=/cloudsql/<connection_name>. Format matches
# the previous manual injection (see deploy/terraform/README.md).
resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.app["DATABASE_URL"].id
  secret_data = "postgresql+asyncpg://baloo:${random_password.db.result}@/baloo?host=/cloudsql/${google_sql_database_instance.baloo.connection_name}"
}
