output "service_url" {
  value = google_cloud_run_v2_service.baloo.uri
}

output "cloudsql_connection_name" {
  value = google_sql_database_instance.baloo.connection_name
}

output "runtime_sa_email" {
  value = google_service_account.baloo_run.email
}
