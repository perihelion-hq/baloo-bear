# Cloud Run Migration Design — rocky-pi[bot] / baloo-bear

- **Date:** 2026-06-18
- **Status:** Design (approved-with-corrections; pre-implementation)
- **Scope:** Move the PR-review bot off its current Cloudflare quick-tunnel
  self-hosting onto Google Cloud, using **Cloud Run + Cloud SQL**.
- **GCP project:** `perihelion-485106` (org / shared-tooling home)
- **Region:** `us-central1`
- **Persona:** Authored under the DevOps-engineer lens (build / deploy / ops
  hats) with the `kubernetes-specialist` review viewpoint applied as a
  comparison-only check against the Cloud Run decision.

---

## 0. Why this document exists

The bot currently runs as `docker-compose` (baloo + `postgres:16-alpine`)
behind a **Cloudflare quick tunnel** (`*.trycloudflare.com`). Quick tunnels get
a **new random hostname on every restart**, which breaks the GitHub App webhook
URL, the public base URL, and the brand-icon URL each time the host bounces.
That is prototype-grade and not survivable for a service that GitHub calls by a
fixed webhook URL.

This document is the design basis for moving to a managed, stable-URL runtime.
It does **not** authorize provisioning, secret injection, webhook changes, or
decommissioning the current host — those are gated on explicit operator
approval and tracked in the implementation plan.

---

## 1. Decision summary

| Axis | Decision | Why |
| --- | --- | --- |
| Compute | **Cloud Run (service)** | Stable HTTPS URL, scale-to-floor, no node/cluster ops; the app is a single FastAPI container with a webhook entrypoint. |
| Database | **Cloud SQL for PostgreSQL** | Managed Postgres, private connection to Cloud Run via the built-in connector; replaces the compose-local `postgres:16-alpine`. |
| Image registry | **Artifact Registry** | First-party registry in the same project; no `latest` tag in deploys. |
| Secrets | **Secret Manager** | All real secret values injected by the operator; never committed, never echoed. |
| Region | `us-central1` | Matches existing org tooling footprint. |

**GKE was assessed and rejected as the host.** The `kubernetes-specialist`
viewpoint confirms there is no per-pod, multi-service, or custom-scheduling
requirement here: one stateless HTTP container + one managed database. GKE
would add cluster lifecycle, node pools, and Workload-Identity wiring with no
offsetting benefit. Cloud Run is the smaller correct surface. (The existing
`roster-runtime-dev` Autopilot cluster is Roster-runtime-dedicated and lives in
a different project; reusing it would couple this tool to product runtime — a
boundary we explicitly keep.)

---

## 2. Cloud Run background-task semantics (CORRECTED — no completion guarantee)

The webhook handler is **fire-and-forget**. On an eligible event it does:

```python
# baloo/github/webhook_handler.py:258
task = asyncio.create_task(process_pr_review(...))
# ... returns immediately:
return {"status": "queued"}
```

It schedules the review coroutine and returns `{"status": "queued"}` **before
the review runs**. The HTTP response does not represent review completion.

Cloud Run's default request-scoped CPU model would throttle CPU once the
response is sent, which would stall or starve that detached task. The settings
below are the **minimum conditions that make this existing structure *operable*
on Cloud Run** — they are **not a completion guarantee**:

| Setting | Effect | What it does NOT do |
| --- | --- | --- |
| `--no-cpu-throttling` | CPU stays allocated after the response returns, so the detached task keeps running; switches billing to instance-based. | Does not pin the instance forever. |
| `--min-instances=1` | Keeps one warm instance so a webhook isn't cold-starting and so the background worker has somewhere to run. | Does not prevent additional instances from being added or removed by autoscaling. |

**Residual risk, stated honestly:** even with both settings, an instance can
still be scaled in or replaced. Outstanding detached tasks then receive only a
`SIGTERM` grace window (`--timeout` / shutdown grace), not a guaranteed
drain-to-completion. A review in flight during scale-in or a deploy can be cut
off. This is acceptable for the first migration because the existing dedup makes
a re-delivered or re-triggered webhook safe to reprocess (see §5), but it is a
known limitation.

**Follow-up (out of scope for this migration):** move review execution onto a
**durable queue** (e.g. Cloud Tasks → a `/process` endpoint, or Pub/Sub push)
so completion no longer depends on a single warm instance surviving. That is the
correct "reviews always finish" design and is deferred to a separate spec.

Source: Cloud Run billing / CPU allocation —
<https://docs.cloud.google.com/run/docs/configuring/billing-settings>

---

## 3. Database migrations (CORRECTED — startup migration is the primary path)

The app **already runs migrations at startup**. No new migration mechanism is
required, and a migration must **not** be modeled as a raw `alembic` CLI call.

```python
# baloo/github/webhook_handler.py:60 (lifespan)
if settings.database_enabled and settings.database_url:
    await init_db(settings.database_url)

# baloo/db/engine.py:110 (inside init_db -> _run_alembic_migrations)
command.upgrade(alembic_cfg, "head")   # under a Postgres advisory lock
```

Key facts that shape this decision:

- `init_db()` takes an advisory lock and runs `command.upgrade(..., "head")`,
  falling back to `Base.metadata.create_all`. It is **idempotent** and safe to
  run on every boot and across concurrent instances (advisory lock serializes
  them).
- Raw `alembic` CLI would read `alembic.ini`'s **hardcoded localhost URL**
  (`sqlalchemy.url=postgresql+asyncpg://baloo:baloo@localhost:5432/baloo`) and
  would **not** pick up `DATABASE_URL`. So any pre-deploy migration step must
  call the **`init_db()` entrypoint**, not `alembic upgrade` directly.

**Decision:**

1. **Primary path — startup migration, 0 code change.** Cloud Run boots the
   service; `lifespan` → `init_db()` applies migrations under the advisory lock.
   With `--min-instances=1` the first instance migrates; the lock makes
   additional instances safe.
2. **Optional pre-deploy gate — Cloud Run Job (later, if we want migrate-before-
   serve ordering).** The Job runs the **same image** with a command that
   invokes the `init_db()` entrypoint (a tiny `python -m`-style entry, or reuse
   `main.py`'s init path), **not** raw `alembic`. Because startup migration is
   idempotent, when the Job has already advanced the schema the service's
   startup migration becomes a no-op. The Job needs the **same Cloud SQL
   connection and secrets** as the service (see §4).

We do **not** introduce a `BALOO_RUN_MIGRATIONS=false` flag in this migration:
the startup migration is cheap, idempotent, and advisory-locked, so disabling it
buys nothing and adds a config branch. If we later adopt the Job as a hard
pre-deploy gate and want to forbid service-side migration entirely, that flag
becomes a small, separately-scoped code change — noted, not done here.

Source: connecting Cloud Run to Cloud SQL —
<https://docs.cloud.google.com/sql/docs/postgres/connect-run>

---

## 4. Cloud SQL connection (concrete)

Cloud Run reaches Cloud SQL through the built-in Cloud SQL connector over a Unix
domain socket. Required wiring:

**Service deploy flags**

```
--add-cloudsql-instances=perihelion-485106:us-central1:<INSTANCE>
--port=8000
--no-cpu-throttling
--min-instances=1
```

**Service account IAM**

- The Cloud Run service's runtime service account needs
  **`roles/cloudsql.client`** on `perihelion-485106`.
- The same service account needs `roles/secretmanager.secretAccessor` for the
  injected secrets (§6).

**DATABASE_URL (Unix-socket form, asyncpg)**

```
postgresql+asyncpg://<USER>:<PASSWORD>@/<DBNAME>?host=/cloudsql/perihelion-485106:us-central1:<INSTANCE>
```

- `host=/cloudsql/<connection-name>` is the socket directory the connector
  mounts; there is no TCP host/port. This matches the existing
  `postgresql+asyncpg://...` driver the app already uses.
- The username/password come from Cloud SQL user config; the password is a
  Secret Manager value, not a literal in any committed file.

**Migration Job (if adopted, §3) uses the identical connection:** same
`--add-cloudsql-instances`, same `DATABASE_URL`, same service account with
`roles/cloudsql.client`, same secret bindings. The only difference from the
service is the entrypoint command (`init_db()` entry instead of the server).

Source: connecting Cloud Run to Cloud SQL —
<https://docs.cloud.google.com/sql/docs/postgres/connect-run>

---

## 5. Concurrency & deduplication (CORRECTED — same-SHA dedup only; semaphore is per-process)

Two distinct mechanisms exist; neither is a global cross-instance concurrency
limit, and the wording must be precise:

1. **`uq_reviews_active_sha` — partial unique index (same PR + same commit
   only).**

   ```python
   # baloo/db/migrations/versions/007_add_active_review_lock.py:24
   # UNIQUE INDEX ON reviews (repo_full_name, pr_number, commit_sha)
   #   WHERE review_status = 'in_progress'
   ```

   This blocks **only a duplicate in-progress review of the same
   `(repo, pr, commit_sha)`**. On Cloud Run scale-out, if the same webhook (same
   SHA) is delivered twice and lands on two instances, the second insert
   collides on this index and is rejected. **This is the property we rely on for
   safe re-delivery / scale-out.** It does **not** limit concurrency across
   different PRs or different commits.

2. **`get_review_semaphore()` — per-process `asyncio.Semaphore`.**

   ```python
   # baloo/review/orchestrator.py:93
   asyncio.Semaphore(settings.max_concurrent_reviews)
   ```

   This caps concurrent reviews **within a single process/instance**. It is
   **not** a global limit: with N Cloud Run instances the effective ceiling is
   `N × max_concurrent_reviews`. With `--min-instances=1` and modest autoscaling
   this is fine, but the spec must not describe it as a system-wide cap.

**Net guarantee on Cloud Run:** scale-out is safe specifically against
**duplicate same-SHA** reviews (index-enforced). It is **not** a global
concurrency governor. If we later need a true global cap, that belongs with the
durable-queue follow-up (§2), not this migration.

---

## 6. Configuration matrix

### 6a. Secrets (Secret Manager — operator-provided, never committed/echoed)

These hold **real secret values** supplied by the operator directly into Secret
Manager. Committed files and this spec reference them only by name. They are
**never** printed in tool output and **never** written to a committed file.

| Secret | Purpose |
| --- | --- |
| `SYNTHETIC_API_KEY` | Synthetic provider key for the GLM review model; referenced in `pi/models.json` only as `${SYNTHETIC_API_KEY}`. |
| `GITHUB_PRIVATE_KEY` | GitHub App private key (PEM). |
| `GITHUB_WEBHOOK_SECRET` | Webhook signature verification. |
| `GEMINI_API_KEY` | Fallback model provider key. |
| `DASHBOARD_PASSWORD` | Dashboard basic-auth password. |

> **Security invariant:** `SYNTHETIC_API_KEY` (and every secret above) is used
> only transiently at runtime via Secret Manager injection. Its value is never
> echoed in any output and never written to any committed file. Committed files
> reference it only as `${SYNTHETIC_API_KEY}`.

### 6b. Non-secret environment (safe to set as plain Cloud Run env vars)

| Var | Value | Notes |
| --- | --- | --- |
| `DATABASE_ENABLED` | `true` | Enables the DB path (`lifespan` → `init_db()`). |
| `DATABASE_URL` | Unix-socket form (§4) | Password component comes from a secret. |
| `APP_ENVIRONMENT` | `production` | |
| `APP_PORT` | `8000` | Must match Cloud Run `--port=8000`; `main.py` reads `settings.app_port`. |
| `APP_HOST` | `0.0.0.0` | So the container binds the Cloud Run port. |
| `PUBLIC_BASE_URL` | the stable Cloud Run service URL | Drives webhook-facing URLs and the brand-icon URL. |
| `BRAND_ICON_URL` | optional explicit icon URL | Else derived as `${PUBLIC_BASE_URL}/assets/rocky.png` (`orchestrator.py:81-90`). |
| `AGENT_PROVIDER` | `synthetic` | |
| `AGENT_MODEL` | `glm` | Short name; `MODEL_REGISTRY` maps `glm` → `(synthetic, hf:zai-org/GLM-5.2, 30)`, matching `pi/models.json`. |
| `AGENT_FALLBACK_MODEL` | `google/gemini-3.1-pro-preview` | |
| `ALLOWED_REPOSITORIES` | `perihelion-hq/roster` | |
| `INSTALLATION_ID` | the GitHub App installation id | Tenant id used by tenant filters. |
| `DASHBOARD_USERNAME` | dashboard user | Password is the secret above. |
| `GITHUB_APP_ID` | the GitHub App id | Non-secret identifier. |

---

## 7. Deployment shape & sequence (design-level; provisioning gated)

1. **Build & push image** to Artifact Registry, tagged by commit SHA (never
   `latest`). Image is the existing `Dockerfile` (python:3.11-slim + node 20 +
   `pi-coding-agent`), unchanged for this migration.
2. **Provision Cloud SQL (Postgres)** instance + database + user in
   `perihelion-485106 / us-central1`.
3. **Create Secret Manager secrets** (§6a) — operator injects real values.
4. **Deploy Cloud Run service** with the flags in §4, env in §6b, secret
   bindings in §6a, `--add-cloudsql-instances`, `--no-cpu-throttling`,
   `--min-instances=1`, `--port=8000`. Startup migration runs via `init_db()`.
5. **(Optional) Cloud Run Job** as a pre-deploy migration gate, same image +
   same connection/secrets, calling the `init_db()` entrypoint (§3).
6. **Repoint the GitHub App webhook URL** to the stable Cloud Run URL +
   `PUBLIC_BASE_URL`. *(Outward-facing change — gated on operator approval.)*
7. **Smoke-verify** `/health`, a test webhook delivery, and one real PR review
   end-to-end.
8. **Decommission** the Cloudflare quick tunnel + local compose host only after
   the Cloud Run path is verified. *(Gated on operator approval.)*

**Rollback:** the Cloudflare-tunnel host remains untouched until step 8, so
rollback is "repoint the webhook back to the tunnel" until decommission. After
decommission, rollback is "redeploy a previous Artifact Registry image revision
+ re-create the tunnel host."

---

## 8. Out of scope (explicitly deferred)

- Durable review queue / guaranteed completion (Cloud Tasks or Pub/Sub) — §2
  follow-up.
- Global cross-instance concurrency cap — §5 follow-up.
- `BALOO_RUN_MIGRATIONS=false` hard pre-deploy migration gate — §3, only if the
  Job becomes the sole migration path.
- IaC authoring (Terraform) for the above — to be decided in the implementation
  plan; this spec fixes the target shape, not the provisioning tool.
- Any change to review logic, model selection, or the Rocky pidgin voice.

---

## 9. Verification note

- **Verified against source** (file:line cited inline): fire-and-forget webhook
  (`webhook_handler.py:258`), startup migration (`webhook_handler.py:60`,
  `engine.py:110`), same-SHA partial unique index
  (`007_add_active_review_lock.py:24`), per-process semaphore
  (`orchestrator.py:93`), icon URL derivation (`orchestrator.py:81-90`),
  `AGENT_MODEL=glm` ↔ `pi/models.json` (`hf:zai-org/GLM-5.2`).
- **Assumptions (unverified, flagged):** exact Cloud SQL tier/size, autoscaling
  `--max-instances`, and `--timeout` grace values are left to the implementation
  plan; they do not change the design shape.
- **External sources:** Cloud Run billing/CPU
  (<https://docs.cloud.google.com/run/docs/configuring/billing-settings>),
  Cloud Run ↔ Cloud SQL
  (<https://docs.cloud.google.com/sql/docs/postgres/connect-run>).
- **Authority boundary:** this is a design artifact. It does not authorize
  provisioning, secret injection, webhook repointing, or host decommissioning —
  each is gated on explicit operator approval per §7.
