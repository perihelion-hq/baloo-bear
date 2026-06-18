variable "project_id" {
  type    = string
  default = "perihelion-485106"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "service_name" {
  type    = string
  default = "rocky-pi"
}

variable "image" {
  type        = string
  description = "Full Artifact Registry image ref including SHA tag. Set at deploy time."
  # Real deploy value: us-central1-docker.pkg.dev/perihelion-485106/baloo/rocky-pi:<git-sha>
  default = "us-docker.pkg.dev/cloudrun/container/hello" # placeholder for first bootstrap apply
}

variable "db_tier" {
  type    = string
  default = "db-custom-1-3840" # 1 vCPU / 3.75 GB; tune later
}

variable "db_password" {
  type      = string
  sensitive = true
  # Provided at apply time via TF_VAR_db_password; never committed.
}

variable "max_instances" {
  type    = number
  default = 4
}
