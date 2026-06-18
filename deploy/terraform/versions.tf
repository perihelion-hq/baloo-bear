terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 7.0"
    }
  }
  # Recommended: store state in a GCS bucket with restricted access, because
  # google_sql_user.password lands in state. Configure before first apply:
  # backend "gcs" {
  #   bucket = "perihelion-485106-tfstate"
  #   prefix = "rocky-pi/cloudrun"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
