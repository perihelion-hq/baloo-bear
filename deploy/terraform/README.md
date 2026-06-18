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
export TF_VAR_db_password="$(openssl rand -hex 24)"   # hex avoids URL-reserved chars; goes into DATABASE_URL below
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

## 3. Create foundational resources (Cloud SQL + IAM + secret containers)

> **Gated.** This apply is `-target`ed precisely because the Cloud Run service
> and Job cannot be created until their secret versions exist (step 4); an
> untargeted apply here would attempt to create Cloud Run resources whose
> `version = "latest"` secret references would fail with zero versions.

```bash
terraform apply \
  -target='google_project_service.apis["artifactregistry.googleapis.com"]' \
  -target='google_project_service.apis["cloudbuild.googleapis.com"]' \
  -target='google_project_service.apis["iam.googleapis.com"]' \
  -target='google_project_service.apis["run.googleapis.com"]' \
  -target='google_project_service.apis["secretmanager.googleapis.com"]' \
  -target='google_project_service.apis["sqladmin.googleapis.com"]' \
  -target=google_artifact_registry_repository.baloo \
  -target=google_service_account.baloo_run \
  -target=google_project_iam_member.cloudsql_client \
  -target=google_sql_database_instance.baloo \
  -target=google_sql_database.baloo \
  -target=google_sql_user.baloo \
  -target='google_secret_manager_secret.app["SYNTHETIC_API_KEY"]' \
  -target='google_secret_manager_secret.app["GEMINI_API_KEY"]' \
  -target='google_secret_manager_secret.app["ANTHROPIC_API_KEY"]' \
  -target='google_secret_manager_secret.app["GITHUB_PRIVATE_KEY"]' \
  -target='google_secret_manager_secret.app["GITHUB_WEBHOOK_SECRET"]' \
  -target='google_secret_manager_secret.app["DASHBOARD_PASSWORD"]' \
  -target='google_secret_manager_secret.app["DATABASE_URL"]' \
  -target='google_secret_manager_secret_iam_member.accessor["SYNTHETIC_API_KEY"]' \
  -target='google_secret_manager_secret_iam_member.accessor["GEMINI_API_KEY"]' \
  -target='google_secret_manager_secret_iam_member.accessor["ANTHROPIC_API_KEY"]' \
  -target='google_secret_manager_secret_iam_member.accessor["GITHUB_PRIVATE_KEY"]' \
  -target='google_secret_manager_secret_iam_member.accessor["GITHUB_WEBHOOK_SECRET"]' \
  -target='google_secret_manager_secret_iam_member.accessor["DASHBOARD_PASSWORD"]' \
  -target='google_secret_manager_secret_iam_member.accessor["DATABASE_URL"]'
  # uses TF_VAR_db_password from step 1
```

> **Cloud SQL break-glass access:** the Cloud SQL instance has `ipv4_enabled =
> true` but the public endpoint is restricted to the operator Tailscale exit IP
> (`sql_authorized_networks`, default `39.122.223.76/32`). App traffic uses the
> `/cloudsql` Unix socket and does not use this endpoint. Update
> `sql_authorized_networks` in `terraform.tfvars` if the Tailscale exit IP
> changes.

## 4. Inject secret values (operator only — values never committed/echoed)

> **Gated.** Add one version per secret. `DATABASE_URL` embeds the DB password from step 1
> and uses the Unix-socket form:

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

## 5. Deploy the Cloud Run service and migration Job with the real image

> **Gated.** Full apply — now that all secret versions exist, Cloud Run can resolve
> `version = "latest"`. This creates `google_cloud_run_v2_service.baloo`,
> `google_cloud_run_v2_service_iam_member.invoker`, and
> `google_cloud_run_v2_job.migrate`.

```bash
terraform apply -var image="${IMAGE}"
SERVICE_URL="$(terraform output -raw service_url)"
echo "Service URL: ${SERVICE_URL}"
```

> **Public invoker binding:** the service is provisioned with an `allUsers`
> `roles/run.invoker` IAM binding so GitHub webhooks can POST without a bearer
> token (Cloud Run would otherwise return HTTP 403 on every delivery). Request
> authenticity is enforced in-app via HMAC signature verification against
> `GITHUB_WEBHOOK_SECRET` — not by the Cloud Run platform layer.

## 6. Set install-specific env, then redeploy
`GITHUB_APP_ID` (3903440) and `DASHBOARD_USERNAME` are already wired into
`local.plain_env` in `cloud_run.tf`. The only deploy-time value is
`PUBLIC_BASE_URL` (the service's own URL, known only after step 5); it drives
absolute links and the comment icon and is otherwise cosmetic. Re-apply with it
set (and optionally pin `installation_id`):
```bash
terraform apply -var image="${IMAGE}" -var public_base_url="${SERVICE_URL}"
```

## 7. Smoke verify (post-deploy verification)
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
