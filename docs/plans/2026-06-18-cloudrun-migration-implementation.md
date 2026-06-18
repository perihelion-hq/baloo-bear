# Cloud Run Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the `rocky-pi[bot]` / baloo-bear PR-review service off its Cloudflare quick-tunnel self-hosting onto Cloud Run + Cloud SQL in `perihelion-485106`, provisioned as Terraform IaC, with a stable HTTPS URL.

**Architecture:** Terraform manages all durable infrastructure (Artifact Registry, Cloud SQL Postgres, Secret Manager secret containers, a runtime service account with least-privilege IAM, the Cloud Run v2 service, and an optional migration Job). The container image is built by Cloud Build and pushed to Artifact Registry, tagged by git SHA (never `latest`). The app is deployed unchanged except for one additive ops entrypoint (`scripts/migrate.py`); database migrations run at startup via the existing advisory-locked `init_db()` path. Real secret values are injected by the operator directly into Secret Manager and are never committed or echoed.

**Tech Stack:** Terraform (`hashicorp/google` provider), Cloud Run v2, Cloud SQL for PostgreSQL 16, Secret Manager, Artifact Registry, Cloud Build, FastAPI/uvicorn (existing app), Python 3.11, pytest.

**Spec:** [docs/plans/2026-06-18-cloudrun-migration-design.md](2026-06-18-cloudrun-migration-design.md)

## Global Constraints

- GCP project: `perihelion-485106`. Region: `us-central1`.
- Image registry: Artifact Registry. Cloud Run image tag = git SHA. **Never `latest` in the Cloud Run service.**
- **Secret hygiene (hard rule):** real values for `SYNTHETIC_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `DASHBOARD_PASSWORD`, and `DATABASE_URL` are operator-provided into Secret Manager. They are never written to any committed file, never printed in command output, and never placed in Terraform variables files that get committed. Committed files reference them only by name.
- Startup migration is the **primary** migration path (`lifespan` → `init_db()` → `command.upgrade(..., "head")` under a Postgres advisory lock; idempotent). The migration Job is optional and calls the `init_db()` entrypoint, **never** the raw `alembic` CLI (which reads `alembic.ini`'s hardcoded localhost URL).
- Background reviews are fire-and-forget (`asyncio.create_task` at `baloo/github/webhook_handler.py:258`). `cpu_idle = false` (CPU always allocated, the v2 equivalent of `--no-cpu-throttling`) + `min_instance_count = 1` are the minimum conditions to keep that structure operable — **not** a completion guarantee. A durable queue is out of scope (deferred per spec §2).
- Cloud Run root filesystem is read-only except `/tmp`. Any path the app writes to must be under `/tmp`. Specifically `FP_AUDIT_LOG_PATH` must be overridden to a `/tmp` path (default `/var/log/baloo/fp-audit.jsonl` would fail at `fp_verifier.py:333` `mkdir`).
- No change to review logic, model selection, or the Rocky pidgin voice.
- **Gated actions** (require explicit operator approval at execution time; each is billable and/or outward-facing): `terraform apply`, operator secret-version injection, image build/push, Cloud Run service deploy, GitHub App webhook repoint, Cloudflare-tunnel/compose host decommission. Authoring the code/IaC is NOT gated; running it is.

---

### Task 1: Terraform skeleton — provider, variables, API enablement

**Files:**
- Create: `deploy/terraform/versions.tf`
- Create: `deploy/terraform/variables.tf`
- Create: `deploy/terraform/apis.tf`
- Create: `deploy/terraform/terraform.tfvars.example`
- Create: `deploy/terraform/.gitignore`

**Interfaces:**
- Produces: Terraform variables `project_id`, `region`, `image`, `db_tier`, `db_password`, `max_instances`, `service_name` consumed by all later tasks. Enabled APIs that all later GCP resources depend on.

- [ ] **Step 1: Write `deploy/terraform/versions.tf`**

```hcl
terraform {
  required_version = ">= 1.12.0"
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
```

- [ ] **Step 2: Write `deploy/terraform/variables.tf`**

```hcl
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
  default     = "us-docker.pkg.dev/cloudrun/container/hello" # placeholder for first bootstrap apply
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
```

- [ ] **Step 3: Write `deploy/terraform/apis.tf`**

```hcl
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}
```

- [ ] **Step 4: Write `deploy/terraform/terraform.tfvars.example`** (committed; contains NO secrets)

```hcl
project_id    = "perihelion-485106"
region        = "us-central1"
service_name  = "rocky-pi"
db_tier       = "db-custom-1-3840"
max_instances = 4
# db_password is NOT set here. Provide it at apply time:
#   export TF_VAR_db_password="$(openssl rand -base64 24)"
# image is set at deploy time:
#   terraform apply -var image="us-central1-docker.pkg.dev/perihelion-485106/baloo/rocky-pi:<SHA>"
```

- [ ] **Step 5: Write `deploy/terraform/.gitignore`**

```gitignore
# Never commit real var values or state
terraform.tfvars
*.auto.tfvars
.terraform/
*.tfstate
*.tfstate.*
crash.log
```

- [ ] **Step 6: Validate formatting and syntax**

Run: `cd deploy/terraform && terraform fmt -check && terraform init -backend=false && terraform validate`
Expected: `fmt` reports no changes; `validate` prints `Success! The configuration is valid.`

- [ ] **Step 7: Commit**

```bash
git add deploy/terraform/versions.tf deploy/terraform/variables.tf deploy/terraform/apis.tf deploy/terraform/terraform.tfvars.example deploy/terraform/.gitignore
git commit -m "infra: terraform skeleton (provider, vars, API enablement) for Cloud Run migration"
```

---

### Task 2: Artifact Registry repository

**Files:**
- Create: `deploy/terraform/artifact_registry.tf`

**Interfaces:**
- Produces: a Docker repo `baloo` in `var.region`; image refs take the form `us-central1-docker.pkg.dev/perihelion-485106/baloo/rocky-pi:<SHA>`.

- [ ] **Step 1: Write `deploy/terraform/artifact_registry.tf`**

```hcl
resource "google_artifact_registry_repository" "baloo" {
  location      = var.region
  repository_id = "baloo"
  format        = "DOCKER"
  description   = "Container images for the rocky-pi PR-review bot"
  depends_on    = [google_project_service.apis]
}
```

- [ ] **Step 2: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add deploy/terraform/artifact_registry.tf
git commit -m "infra: Artifact Registry repo for rocky-pi images"
```

---

### Task 3: Runtime service account and IAM

**Files:**
- Create: `deploy/terraform/iam.tf`

**Interfaces:**
- Produces: `google_service_account.baloo_run` (referenced by the Cloud Run service, the Job, and secret IAM bindings). Grants `roles/cloudsql.client` at project scope.

- [ ] **Step 1: Write `deploy/terraform/iam.tf`**

```hcl
resource "google_service_account" "baloo_run" {
  account_id   = "rocky-pi-run"
  display_name = "rocky-pi Cloud Run runtime"
}

resource "google_project_iam_member" "cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.baloo_run.email}"
}
```

- [ ] **Step 2: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add deploy/terraform/iam.tf
git commit -m "infra: runtime service account + cloudsql.client binding"
```

---

### Task 4: Secret Manager secret containers + accessor bindings

**Files:**
- Create: `deploy/terraform/secrets.tf`

**Interfaces:**
- Consumes: `google_service_account.baloo_run` (Task 3).
- Produces: empty secret containers for all seven secret names; grants the runtime SA `roles/secretmanager.secretAccessor` on each. Secret **versions** (the real values) are added out-of-band by the operator — never by Terraform.

- [ ] **Step 1: Write `deploy/terraform/secrets.tf`**

```hcl
locals {
  secret_ids = [
    "SYNTHETIC_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DASHBOARD_PASSWORD",
    "DATABASE_URL",
  ]
}

resource "google_secret_manager_secret" "app" {
  for_each  = toset(local.secret_ids)
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.baloo_run.email}"
}
```

- [ ] **Step 2: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add deploy/terraform/secrets.tf
git commit -m "infra: Secret Manager containers + accessor IAM for rocky-pi"
```

---

### Task 5: Cloud SQL Postgres instance, database, user

**Files:**
- Create: `deploy/terraform/cloudsql.tf`

**Interfaces:**
- Consumes: `var.db_tier`, `var.db_password` (sensitive).
- Produces: `google_sql_database_instance.baloo` with `.connection_name` (form `perihelion-485106:us-central1:rocky-pi-db`), database `baloo`, user `baloo`. The connection name is consumed by the Cloud Run service (Task 6) and the migration Job (Task 9).

- [ ] **Step 1: Write `deploy/terraform/cloudsql.tf`**

```hcl
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
```

- [ ] **Step 2: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add deploy/terraform/cloudsql.tf
git commit -m "infra: Cloud SQL Postgres 16 instance, database, user"
```

---

### Task 6: Cloud Run v2 service

**Files:**
- Create: `deploy/terraform/cloud_run.tf`
- Create: `deploy/terraform/outputs.tf`

**Interfaces:**
- Consumes: `var.image`, `google_service_account.baloo_run` (Task 3), `google_secret_manager_secret.app` (Task 4), `google_sql_database_instance.baloo.connection_name` (Task 5).
- Produces: `google_cloud_run_v2_service.baloo` with `.uri` (the stable HTTPS URL → `PUBLIC_BASE_URL`). Output `service_url`, `cloudsql_connection_name`, `runtime_sa_email`.

- [ ] **Step 1: Write `deploy/terraform/cloud_run.tf`**

```hcl
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
    service_account = google_service_account.baloo_run.email
    timeout         = "300s"
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
```

> **Note (env that is operator/install-specific, set at deploy time, not hardcoded here):** `PUBLIC_BASE_URL` (the service's own URL — known only after first apply), `BRAND_ICON_URL` (optional; else derived as `${PUBLIC_BASE_URL}/assets/rocky.png`), `INSTALLATION_ID`, `GITHUB_APP_ID`, `DASHBOARD_USERNAME`. Add these to `local.plain_env` once known, or pass via `-var`. See Task 8.

- [ ] **Step 2: Write `deploy/terraform/outputs.tf`**

```hcl
output "service_url" {
  value = google_cloud_run_v2_service.baloo.uri
}

output "cloudsql_connection_name" {
  value = google_sql_database_instance.baloo.connection_name
}

output "runtime_sa_email" {
  value = google_service_account.baloo_run.email
}
```

- [ ] **Step 3: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add deploy/terraform/cloud_run.tf deploy/terraform/outputs.tf
git commit -m "infra: Cloud Run v2 service (cpu_idle=false, min=1, cloudsql, secret env)"
```

---

### Task 7: Standalone migration entrypoint (`scripts/migrate.py`) + tests

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `baloo.config.settings.get_settings`, `baloo.db.engine.init_db` (existing; `async def init_db(database_url: str) -> None`).
- Produces: `scripts.migrate.main()` — a synchronous entrypoint that runs the same advisory-locked, idempotent migration as app startup, reading `DATABASE_URL` from settings. Used by the optional Cloud Run Job (Task 9) and for local/manual migration. Never invokes the raw `alembic` CLI.

- [ ] **Step 1: Write the failing test `tests/test_migrate.py`**

```python
"""Tests for the standalone migration entrypoint."""

import pytest

import scripts.migrate as migrate


class _FakeSettings:
    def __init__(self, url):
        self.database_url = url


def test_main_calls_init_db_with_settings_url(monkeypatch):
    captured = {}

    async def fake_init_db(url):
        captured["url"] = url

    monkeypatch.setattr(
        migrate,
        "get_settings",
        lambda: _FakeSettings("postgresql+asyncpg://baloo:pw@/baloo?host=/cloudsql/p:r:i"),
    )
    monkeypatch.setattr(migrate, "init_db", fake_init_db)

    migrate.main()

    assert captured["url"].startswith("postgresql+asyncpg://")
    assert "host=/cloudsql/" in captured["url"]


def test_main_errors_when_database_url_empty(monkeypatch):
    monkeypatch.setattr(migrate, "get_settings", lambda: _FakeSettings(""))
    monkeypatch.setattr(migrate, "init_db", lambda url: None)

    with pytest.raises(SystemExit):
        migrate.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.migrate'` (or import error).

- [ ] **Step 3: Create `scripts/__init__.py`** (empty file, makes `scripts` an importable package for tests)

```python
```

- [ ] **Step 4: Write `scripts/migrate.py`**

```python
"""Standalone database migration entrypoint.

Runs the same advisory-locked, idempotent migration path as application
startup (``init_db`` -> Alembic ``upgrade head``), reading ``DATABASE_URL``
from settings so it works with the Cloud Run Unix-socket connection. This
deliberately does NOT use the raw ``alembic`` CLI, which would read
``alembic.ini``'s hardcoded localhost URL instead of ``DATABASE_URL``.

Usage:
    python scripts/migrate.py
"""

from __future__ import annotations

import asyncio
import sys

from baloo.config.settings import get_settings
from baloo.db.engine import init_db


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set; cannot run migrations.", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(init_db(settings.database_url))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_migrate.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Lint**

Run: `ruff check scripts/migrate.py tests/test_migrate.py && black --check scripts/migrate.py tests/test_migrate.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add scripts/__init__.py scripts/migrate.py tests/test_migrate.py
git commit -m "feat: standalone init_db migration entrypoint for Cloud Run Job / ops"
```

---

### Task 8: Deploy runbook (`deploy/terraform/README.md`)

**Files:**
- Create: `deploy/terraform/README.md`

**Interfaces:**
- Produces: the ordered, copy-pasteable operator procedure that ties Tasks 1–7 and 9 together, including the gated build/apply/secret/webhook steps. No new code.

- [ ] **Step 1: Write `deploy/terraform/README.md`**

````markdown
# rocky-pi Cloud Run deployment runbook

Project `perihelion-485106`, region `us-central1`. All `apply`, image-build,
secret, and webhook steps are **gated** — run them only with explicit approval.
Real secret values are entered here by the operator and are never committed.

## 0. One-time prerequisites
- `gcloud auth login` and `gcloud config set project perihelion-485106`
- (Recommended) create the state bucket and uncomment the `backend "gcs"` block
  in `versions.tf`:
  ```bash
  gsutil mb -l us-central1 gs://perihelion-485106-tfstate
  gsutil versioning set on gs://perihelion-485106-tfstate
  ```

## 1. Bootstrap APIs + Artifact Registry (so the image has somewhere to go)
```bash
cd deploy/terraform
terraform init
export TF_VAR_db_password="$(openssl rand -base64 24)"   # keep this; it goes into DATABASE_URL below
terraform apply \
  -target=google_project_service.apis \
  -target=google_artifact_registry_repository.baloo
```

## 2. Build and push the image (tagged by SHA, never latest)
```bash
SHA="$(git rev-parse --short HEAD)"
IMAGE="us-central1-docker.pkg.dev/perihelion-485106/baloo/rocky-pi:${SHA}"
gcloud builds submit --tag "${IMAGE}" .
```

## 3. Create Cloud SQL + IAM + secret containers
```bash
terraform apply   # uses TF_VAR_db_password from step 1
```

## 4. Inject secret values (operator only — values never committed/echoed)
Add one version per secret. `DATABASE_URL` embeds the DB password from step 1
and uses the Unix-socket form:
```bash
CONN="$(terraform output -raw cloudsql_connection_name)"   # perihelion-485106:us-central1:rocky-pi-db
# DATABASE_URL (note: URL-encode the password if it contains reserved chars)
printf 'postgresql+asyncpg://baloo:%s@/baloo?host=/cloudsql/%s' "${TF_VAR_db_password}" "${CONN}" \
  | gcloud secrets versions add DATABASE_URL --data-file=-

# The rest: pipe each real value from a local file or stdin, never inline in shell history.
gcloud secrets versions add SYNTHETIC_API_KEY     --data-file=/path/to/synthetic.key
gcloud secrets versions add GEMINI_API_KEY        --data-file=/path/to/gemini.key
gcloud secrets versions add ANTHROPIC_API_KEY     --data-file=/path/to/anthropic.key
gcloud secrets versions add GITHUB_PRIVATE_KEY    --data-file=/path/to/github-app.pem
gcloud secrets versions add GITHUB_WEBHOOK_SECRET --data-file=/path/to/webhook.secret
gcloud secrets versions add DASHBOARD_PASSWORD    --data-file=/path/to/dashboard.pw
```

## 5. Deploy the Cloud Run service with the real image
```bash
terraform apply -var image="${IMAGE}"
SERVICE_URL="$(terraform output -raw service_url)"
echo "Service URL: ${SERVICE_URL}"
```

## 6. Set install-specific env, then redeploy
Add to `local.plain_env` in `cloud_run.tf` (or pass via `-var`) and re-apply:
`PUBLIC_BASE_URL=${SERVICE_URL}`, `GITHUB_APP_ID`, `INSTALLATION_ID`,
`DASHBOARD_USERNAME`, and optionally `BRAND_ICON_URL`.
```bash
terraform apply -var image="${IMAGE}"
```

## 7. Smoke verify (Task 10)
```bash
curl -fsS "${SERVICE_URL}/health"
```

## 8. Repoint GitHub App webhook (gated, outward-facing)
Set the GitHub App webhook URL to `${SERVICE_URL}` (path per `webhook_handler`),
keeping `GITHUB_WEBHOOK_SECRET` in sync. Redeliver a recent webhook and confirm
a review runs end-to-end.

## 9. Decommission the Cloudflare quick tunnel + compose host (gated)
Only after the Cloud Run path is verified. Rollback before this point = repoint
the webhook back to the tunnel; after = redeploy a previous Artifact Registry
image revision (`terraform apply -var image=...:<prevSHA>`) and re-create the
tunnel host.
````

- [ ] **Step 2: Commit**

```bash
git add deploy/terraform/README.md
git commit -m "docs: Cloud Run deploy runbook (gated build/apply/secret/webhook steps)"
```

---

### Task 9 (OPTIONAL): Cloud Run migration Job (pre-deploy gate)

Skip this task for the first migration — startup migration is the primary path and is sufficient. Implement only if you later want migrate-before-serve ordering. When adopted, it does not replace startup migration (which stays as an idempotent no-op fallback).

**Files:**
- Create: `deploy/terraform/migration_job.tf`

**Interfaces:**
- Consumes: same image, SA, secrets, and Cloud SQL connection as the service (Task 6); `scripts/migrate.py` (Task 7).
- Produces: `google_cloud_run_v2_job.migrate`, runnable via `gcloud run jobs execute` before a service deploy.

- [ ] **Step 1: Write `deploy/terraform/migration_job.tf`**

```hcl
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
```

- [ ] **Step 2: Validate**

Run: `cd deploy/terraform && terraform fmt -check && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add deploy/terraform/migration_job.tf
git commit -m "infra: optional Cloud Run migration Job calling scripts/migrate.py"
```

---

### Task 10: Post-deploy verification (gated; run after Task 8 apply)

**Files:**
- None (verification only). Optionally append a "Verified" note to `deploy/terraform/README.md`.

**Interfaces:**
- Consumes: the deployed `service_url`.

- [ ] **Step 1: Health check**

Run: `curl -fsS "$(cd deploy/terraform && terraform output -raw service_url)/health"`
Expected: HTTP 200 with the app's health JSON.

- [ ] **Step 2: Confirm startup migration ran and DB is reachable**

Run: `gcloud run services logs read rocky-pi --region us-central1 --limit 100`
Expected: startup logs show `init_db`/Alembic `upgrade` completing without error; no `/var/log` permission errors; no Cloud SQL connection errors.

- [ ] **Step 3: Confirm CPU/scaling settings took effect**

Run: `gcloud run services describe rocky-pi --region us-central1 --format='value(spec.template.metadata.annotations)'` and inspect in the console that CPU is always-allocated and min instances = 1.
Expected: instance-based CPU (cpu_idle=false), min instances = 1.

- [ ] **Step 4: End-to-end review (after webhook repoint, Task 8 step 8)**

Open or update a test PR in `perihelion-hq/roster`, then confirm a Rocky review comment posts and `gcloud run services logs read` shows the background task running to completion. Note: per spec §2 this is best-effort, not guaranteed under scale-in.

- [ ] **Step 5: Record the verification note**

Append outcomes (URLs checked, log evidence, any tuning of `db_tier`/`memory`/`max_instances`) to `deploy/terraform/README.md` under a `## Verified` heading and commit.

```bash
git add deploy/terraform/README.md
git commit -m "docs: record Cloud Run deployment verification"
```

---

## Self-Review

**1. Spec coverage:**
- §1 Cloud Run + Cloud SQL + AR + Secret Manager + region → Tasks 1–6. ✓
- §2 `cpu_idle=false` + `min_instance_count=1`, no completion guarantee → Task 6 + Global Constraints. ✓
- §3 startup migration primary (0 code change) + optional Job via `init_db` entrypoint → Global Constraints + Tasks 7, 9. ✓
- §4 `--add-cloudsql-instances`/`roles/cloudsql.client`/Unix-socket DATABASE_URL → Tasks 3, 5, 6 (volume mount) + runbook step 4. ✓
- §5 same-SHA dedup vs per-process semaphore → no code change needed; behavior preserved (Global Constraints note). ✓
- §6 secret + non-secret env matrix → Task 6 `local.plain_env` / `local.secret_env` + runbook. ✓
- §7 deployment sequence + rollback → Task 8 runbook + Task 10. ✓
- §8 out-of-scope (durable queue, global cap, BALOO_RUN_MIGRATIONS flag) → not implemented, consistent. ✓

**2. Placeholder scan:** No "TBD/TODO". The Cloud Run `var.image` placeholder (`cloudrun/container/hello`) is an intentional, documented bootstrap default replaced at deploy time, not a gap. Install-specific env (PUBLIC_BASE_URL etc.) is explicitly deferred to deploy time with exact instructions (Task 6 note + runbook step 6).

**3. Type consistency:** `init_db(database_url: str)` used consistently (Task 7 test + impl + Job command). Secret IDs are identical across `secrets.tf` `local.secret_ids`, `cloud_run.tf` `local.secret_env` values, and the runbook `gcloud secrets versions add` calls. `connection_name` output (Task 6 outputs) matches the runbook's `terraform output -raw cloudsql_connection_name`.

**Deviation from spec (noted):** §6b lists `DATABASE_URL` as non-secret with "password from a secret." Cloud Run cannot interpolate a secret into a plain env var, and the app reads the full `DATABASE_URL`, so this plan treats the **entire `DATABASE_URL` as a Secret Manager secret**. This is a faithfulness-preserving refinement; the spec can be updated to match if desired.
