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

    # Fidelity report is disabled: it spawns a pi --provider anthropic subprocess
    # (fidelity_analyzer.py) that requires a real ANTHROPIC_API_KEY. We run only
    # the synthetic GLM + gemini-fallback path, so ANTHROPIC_API_KEY carries a
    # placeholder version solely to satisfy the secret_env "latest" reference.
    FIDELITY_ENABLED   = "false"
    FP_AUDIT_LOG_PATH  = "/tmp/baloo/fp-audit.jsonl" # /var/log is read-only on Cloud Run
    REPO_CHECKOUT_ROOT = "/tmp/baloo-repos"

    # GitHub App identity. Numeric App ID, NOT the Client ID (getting-started.md §3, §12).
    GITHUB_APP_ID      = "3903440"
    DASHBOARD_USERNAME = "baloo"

    # Install-specific values resolved at deploy time (empty defaults are safe).
    PUBLIC_BASE_URL = var.public_base_url # service URL; set on re-apply once known
    INSTALLATION_ID = var.installation_id # empty = serve all installations
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
              secret  = google_secret_manager_secret.app[env.key].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 30
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.accessor,
    google_project_iam_member.cloudsql_client,
  ]
}

# GitHub sends unauthenticated webhook POSTs; platform-layer auth must be open.
# Request authenticity is enforced in-app via GitHub HMAC signature verification
# (WEBHOOK signature check) and the dashboard password — not by Cloud Run IAM.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.baloo.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
