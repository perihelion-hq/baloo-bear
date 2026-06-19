resource "google_artifact_registry_repository" "baloo" {
  location      = var.region
  repository_id = "baloo"
  format        = "DOCKER"
  description   = "Container images for the rocky-pi PR-review bot"
  depends_on    = [google_project_service.apis]
}
