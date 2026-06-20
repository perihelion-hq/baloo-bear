resource "google_sql_database_instance" "baloo" {
  name                = "rocky-pi-db"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier = var.db_tier
    # ENTERPRISE (not the default ENTERPRISE_PLUS) so db-custom-* tiers are valid
    # and cost stays low for this single small instance.
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_autoresize   = true
    backup_configuration {
      enabled = true
    }
    ip_configuration {
      ipv4_enabled = true

      dynamic "authorized_networks" {
        for_each = var.sql_authorized_networks
        content {
          name  = authorized_networks.value.display_name
          value = authorized_networks.value.cidr_block
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "baloo" {
  name     = "baloo"
  instance = google_sql_database_instance.baloo.name
}

# Single source of truth for the DB password: terraform generates it once
# (stable in state) and uses it for BOTH the Cloud SQL user and the
# DATABASE_URL secret (secrets.tf) — so the two can never drift, which was the
# recurring "password authentication failed" footgun. URL-safe (no special
# chars) so it embeds in the connection string without encoding.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "baloo" {
  name     = "baloo"
  instance = google_sql_database_instance.baloo.name
  password = random_password.db.result
}
