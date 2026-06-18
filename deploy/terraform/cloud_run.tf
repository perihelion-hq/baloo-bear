locals {
  # Non-secret environment (plain values).
  plain_env = {
    DATABASE_ENABLED     = "true"
    APP_ENVIRONMENT      = "production"
    APP_HOST             = "0.0.0.0"
    APP_PORT             = "8000"
    LOG_LEVEL            = "INFO"
    AGENT_PROVIDER       = "synthetic"
    AGENT_MODEL          = "glm"
    AGENT_FALLBACK_MODEL = "google/gemini-3.1-pro-preview"
    ALLOWED_REPOSITORIES = "perihelion-hq/roster"
    FP_AUDIT_LOG_PATH    = "/tmp/baloo/fp-audit.jsonl" # /var/log is read-only on Cloud Run
    REPO_CHECKOUT_ROOT   = "/tmp/baloo-repos"
  }

  # Secret-backed environment: env var name => Secret Manager secret_id.
  secret_env = {
    DATABASE_URL          = "DATABASE_URL"
    SYNTHETIC_API_KEY     = "SYNTHETIC_API_KEY"
    GEMINI_API_KEY        = "GEMINI_API_KEY"
    ANTHROPIC_API_KEY     = "ANTHROPIC_API_KEY"
    GITHUB_PRIVATE_KEY    = "GITHUB_PRIVATE_KEY"
    GITHUB_WEBHOOK_SECRET = "GITHUB_WEBHOOK_SECRET"
    DASHBOARD_PASSWORD    = "DASHBOARD_PASSWORD"
  }
}

resource "google_cloud_run_v2_service" "baloo" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.baloo_run.email
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = 1
      max_instance_count = var.max_instances
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.baloo.connection_name]
      }
    }

    containers {
      image = var.image

      ports {
        container_port = 8000
      }

      resources {
        cpu_idle = false # CPU always allocated == --no-cpu-throttling
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.accessor,
    google_project_iam_member.cloudsql_client,
  ]
}
