resource "google_sql_database_instance" "baloo" {
  name                = "rocky-pi-db"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_autoresize   = true
    backup_configuration {
      enabled = true
    }
    ip_configuration {
      ipv4_enabled = true
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "baloo" {
  name     = "baloo"
  instance = google_sql_database_instance.baloo.name
}

resource "google_sql_user" "baloo" {
  name     = "baloo"
  instance = google_sql_database_instance.baloo.name
  password = var.db_password
}
