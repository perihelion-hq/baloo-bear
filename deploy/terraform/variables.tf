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

variable "public_base_url" {
  type        = string
  description = "Public base URL of the deployed service, used for absolute links and the comment icon in GitHub comments. Known only after the first apply; set on the re-apply via -var public_base_url=<service_url>. Empty omits absolute links and is harmless."
  default     = ""
}

variable "installation_id" {
  type        = string
  description = "GitHub App installation ID for tenant scoping. Empty serves all installations; the ALLOWED_REPOSITORIES allowlist is the active repository-scope guard. Set to pin this broker to a single installation."
  default     = ""
}

variable "db_tier" {
  type    = string
  default = "db-custom-1-3840" # 1 vCPU / 3.75 GB; tune later
}

variable "max_instances" {
  type    = number
  default = 4
}

variable "sql_authorized_networks" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  description = "Operator IP allowlist for the Cloud SQL public endpoint. App traffic uses the /cloudsql Unix socket and does not need this; it is for operator break-glass (psql) access only. Default is the operator Tailscale exit IP, mirrored from roster infra/environments/dev/dev.auto.tfvars (operator-tailscale-exit). Update if the exit IP changes."
  default = [
    {
      cidr_block   = "39.122.223.76/32"
      display_name = "operator-tailscale-exit"
    }
  ]
}
