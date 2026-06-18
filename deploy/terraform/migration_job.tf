resource "google_cloud_run_v2_job" "migrate" {
  name     = "${var.service_name}-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.baloo_run.email

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.baloo.connection_name]
        }
      }

      containers {
        image   = var.image
        command = ["python", "scripts/migrate.py"]

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = "DATABASE_URL"
              version = "latest"
            }
          }
        }
        env {
          name  = "DATABASE_ENABLED"
          value = "true"
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.accessor,
    google_project_iam_member.cloudsql_client,
  ]
}
