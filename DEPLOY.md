# Deploying the APL backend

This guide takes a new owner from nothing to a running API on their own
infrastructure. It does not assume Supabase or Google Cloud: any PostgreSQL
database, any S3-compatible photo storage and any host that runs a Docker
container will do. The setup the project team ran (Cloud Run with Supabase)
is covered at the end as one option among several.

Day-to-day running (admins, logs, backups, known issues) is in
[OPERATIONS.md](OPERATIONS.md).

## Contents

1. [What you need](#1-what-you-need)
2. [Order of operations](#2-order-of-operations)
3. [Database](#3-database)
4. [Photo storage](#4-photo-storage)
5. [Moving the existing photos](#5-moving-the-existing-photos)
6. [Email](#6-email)
7. [Configuration reference](#7-configuration-reference)
8. [Running it](#8-running-it): [Docker Compose](#option-a-docker-compose-one-server), [any container host](#option-b-any-container-host), [Google Cloud Run](#option-c-google-cloud-run-with-cloud-build)
9. [Pointing the app and website at it](#9-pointing-the-app-and-website-at-it)
10. [Checking a deployment](#10-checking-a-deployment)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What you need

| | Required | Notes |
|---|---|---|
| PostgreSQL 15 or later | Yes | Self-hosted, a managed service (AWS RDS, Google Cloud SQL, Azure, ...) or Supabase. |
| The database schema SQL files | Yes | Supplied separately with the hand-over, **not** in this repository. They create the tables and load the plant library. |
| Photo storage | Yes, in practice | Supabase Storage, or any S3-compatible service: AWS S3, Cloudflare R2, MinIO, Backblaze B2, ... Without it the API runs but **discards every uploaded photo**. |
| An SMTP mailbox | No | Sends contributors the outcome of their submission. Without it the API logs the mail instead of sending it. |
| A host for one container | Yes | Linux server with Docker, Cloud Run, AWS App Runner/ECS, Azure Container Apps, Fly.io, Render, ... 1 GiB of memory. |
| A domain with HTTPS | Yes, for production | The mobile app should talk to the API over HTTPS. |

## 2. Order of operations

The order matters. Each step assumes the one before it.

1. **Create the database and import the schema SQL** (section 3).
2. **Set up photo storage** (section 4).
3. **Configure** the environment variables (section 7).
4. **Run the backend's migrations**: `python -m app.db.migrate` (section 3.3).
   Every deployment option below does this automatically before the API starts.
5. **Start the API** (section 8).
6. **Move the existing photos** into your storage, if the imported database
   still points at the old project's photos (section 5).
7. **Create the first administrator** ([OPERATIONS.md](OPERATIONS.md#first-administrator)).
8. **Point the mobile app and website at the new API** (section 9).

## 3. Database

### 3.1 Import the schema

Run the hand-over SQL files against an empty database, in the order they are
numbered. For example, with `psql`:

```bash
psql "postgresql://USER:PASSWORD@HOST:5432/DBNAME" -v ON_ERROR_STOP=1 -f 01_schema.sql
psql "postgresql://USER:PASSWORD@HOST:5432/DBNAME" -v ON_ERROR_STOP=1 -f 02_data.sql
```

With the Docker Compose stack this happens automatically: put the files in
[`schema/`](schema/README.md) before the first start.

> **Importing into plain PostgreSQL (not Supabase).** A dump taken from
> Supabase can mention things only Supabase has: the roles `anon`,
> `authenticated` and `service_role`, the `auth` and `storage` schemas, and
> row-level-security policies that call `auth.uid()`. The backend uses none
> of them: it connects as one database user and does its own authorisation.
> If the import stops on one of those statements, delete those statements (or
> create empty roles with those names first) and run it again.

### 3.2 The connection string

The backend uses the asyncpg driver, so the URL scheme is
`postgresql+asyncpg://`, not `postgresql://`:

```
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME
```

- Characters such as `@ : / ? #` in the password must be percent-encoded
  (`@` becomes `%40`).
- To require TLS (most managed databases), append `?ssl=require`.
- **Supabase**: use the *transaction pooler* string from Project Settings →
  Database (host `...pooler.supabase.com`, port `6543`). The backend detects
  the pooler and turns off prepared-statement caching, which the pooler does
  not support.

### 3.3 The backend's own migrations

`sql/` holds the schema changes the backend made on top of the hand-over
schema. Apply them with:

```bash
python -m app.db.migrate
```

- Each file runs once, in filename order, and is recorded in a
  `schema_migrations` table. Every script is written so that running it on a
  database that already has the change does nothing, so it does not matter
  whether the hand-over export already includes them.
- It also makes sure the photo storage is ready (section 4).
- It is safe to run on every deploy, and every option in section 8 does. Two
  runs at once wait for each other.
- The scripts **assume the tables from the hand-over schema exist**. Run them
  after 3.1, never on an empty database.

To change the schema later, add `sql/005_short_description.sql` (written so it
can run twice safely) and deploy; see
[OPERATIONS.md](OPERATIONS.md#changing-the-database-schema).

## 4. Photo storage

The backend stores three kinds of photo, as three *logical buckets*:

| Logical bucket | Holds |
|---|---|
| `submission-images` | Photos as contributors upload them. Kept after review as a record of what the reviewer saw. |
| `plant-images-v2` | The published plant library: the curated photos plus every approved submission photo. |
| `avatars` | Profile pictures. |

All three must be **publicly readable**: the app loads photos straight from
their URLs. Nothing in them is private, and upload keys are random, so the
URLs cannot be guessed. Only the backend can write.

Choose one backend with `STORAGE_BACKEND`.

### Option 1: Supabase Storage

```
STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<Project Settings → API → service_role / secret key>
```

The three logical buckets are three Supabase buckets. `python -m
app.db.migrate` creates any that are missing, as public buckets. An existing
private bucket is left private with a warning in the log, because making it
public is a decision for a person; flip it to public in the dashboard.

The service-role key bypasses all of Supabase's access rules. Keep it on the
server only, never in the mobile app or website.

### Option 2: S3-compatible storage (AWS S3, Cloudflare R2, MinIO, ...)

One bucket holds everything; the logical buckets become key prefixes in it
(`avatars/users/7/3f2a....jpg`).

```
STORAGE_BACKEND=s3
AWS_S3_BUCKET=apl-media
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-southeast-2              # R2: auto
AWS_S3_ENDPOINT_URL=                   # empty for AWS; see below for others
S3_PUBLIC_BASE_URL=https://...         # see below
```

`S3_PUBLIC_BASE_URL` is the address the public reads the bucket from, with no
trailing slash: `<S3_PUBLIC_BASE_URL>/<key>` must return the object stored at
`<key>`. The backend builds every photo URL it stores from it, so it must be
the final public address, a CDN or custom domain if you use one.

| Service | `AWS_S3_ENDPOINT_URL` | `S3_PUBLIC_BASE_URL` | Making it publicly readable |
|---|---|---|---|
| AWS S3 | *(empty)* | `https://<bucket>.s3.<region>.amazonaws.com` or a CloudFront domain | Turn off "Block public access" for the bucket and add the policy below, or put CloudFront in front. |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | Your R2 public bucket URL or custom domain | Bucket → Settings → Public access (custom domain or r2.dev). |
| MinIO | `http://minio:9000` (or its address) | `https://<public-host>/<bucket>` | `mc anonymous set download <alias>/<bucket>` |

AWS bucket policy for anonymous reads (objects only, no listing):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::apl-media/*"
  }]
}
```

The access key needs `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` and
`s3:ListBucket` on the bucket (the last one lets the deploy check tell "no
such bucket" apart from "access denied").

The bucket must exist before the first deploy. `python -m app.db.migrate`
checks that it is reachable and stops the deploy if not; it never creates a
bucket or changes its permissions.

### Which one is in use?

The API logs `storage_backend` with the backend it chose on every start. With
the default `STORAGE_BACKEND=auto` it uses Supabase if both Supabase values
are set, otherwise S3 if `AWS_S3_BUCKET` and `S3_PUBLIC_BASE_URL` are set,
otherwise **none**: uploads are accepted and discarded, with a warning in the
log. Set `STORAGE_BACKEND` explicitly in production so a missing value is
visible instead of silently changing where photos go.

## 5. Moving the existing photos

If the database was imported from the original deployment, its photo URLs
still point at the original Supabase project, which you do not control and
which may be shut down. Copy them into your storage once the API's storage is
configured:

```bash
# See what would move; changes nothing.
python -m app.db.migrate_images --dry-run

# Copy the photos and rewrite the URLs.
python -m app.db.migrate_images
```

It downloads every photo behind a Supabase Storage URL in `plant_images_v2`,
`submission_images` and `app_users.avatar_url`, uploads it to the configured
storage under the same key and logical bucket, and rewrites that row. Run it
from wherever the API's environment variables are set; with Docker Compose:

```bash
docker compose run --rm app python -m app.db.migrate_images
```

- **Run it while the old URLs still work.** It cannot copy a photo that is gone.
- It is safe to run again: rows already moved are skipped, and if a run is
  interrupted the next one finishes the job.
- URLs that are not Supabase Storage URLs are left alone. The curated
  thumbnails (`thumbnail_url`) point at an external image service, not at
  this project's storage.
- It prints a summary and exits non-zero if any photo failed; the log names
  each failed row. Re-run once the cause is fixed.

## 6. Email

When a reviewer approves or rejects a submission, the contributor gets an
email. Any SMTP mailbox works (Google Workspace, Microsoft 365, Namecheap
Private Email, Zoho, Amazon SES SMTP, ...):

```
SMTP_HOST=mail.example.org
SMTP_PORT=587                     # 587 = STARTTLS; 465 = implicit TLS
SMTP_USERNAME=info@example.org
SMTP_PASSWORD=...                 # secret
EMAIL_FROM=Australian Poisonous Plants <info@example.org>
EMAIL_REPLY_TO=                   # optional
```

- `EMAIL_FROM` must be the same mailbox as `SMTP_USERNAME`. Providers refuse,
  or mark as spoofed, mail sent "from" another address.
- Publish SPF, DKIM and a DMARC record for the domain (your mail provider
  documents the values), or the mail will land in spam folders.
- Many cloud hosts block outbound port 25. Ports 587 and 465 are fine.
- Instead of SMTP, a [Resend](https://resend.com) API key also works
  (`RESEND_API_KEY`, with `SMTP_HOST` empty).
- With neither configured, the API runs normally and writes each message it
  would have sent to the log.

## 7. Configuration reference

All settings are environment variables; [`.env.example`](.env.example)
documents each one. **Secrets** must live in your platform's secret store (or
a `.env` file that is never committed), not in code or in a build file.

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | **Yes** | Signs login tokens. A long random value: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. The API refuses to start with the placeholder unless `DEBUG=true`. Changing it signs everyone out. |
| `DATABASE_URL` | **Yes** | **Yes** | Section 3.2. |
| `DEBUG` | No | | `false` in production (the default). |
| `ALLOWED_ORIGINS` | For the website | | JSON list of web origins allowed to call the API from a browser, e.g. `["https://www.example.org","https://example.org"]`. The mobile app does not need an entry. |
| `STORAGE_BACKEND` | Recommended | | `supabase` or `s3` (section 4). |
| `SUPABASE_URL` | Supabase storage | | |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase storage | **Yes** | |
| `AWS_S3_BUCKET`, `AWS_REGION`, `AWS_S3_ENDPOINT_URL`, `S3_PUBLIC_BASE_URL` | S3 storage | | |
| `AWS_ACCESS_KEY_ID` | S3 storage | | |
| `AWS_SECRET_ACCESS_KEY` | S3 storage | **Yes** | |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `EMAIL_FROM`, `EMAIL_REPLY_TO` | For email | | Section 6. |
| `SMTP_PASSWORD` | For email | **Yes** | |
| `RESEND_API_KEY` | Alternative to SMTP | **Yes** | |
| `REDIS_URL` | When running more than one instance | Usually | Shares login rate limits and signed-out tokens between instances. Without it each instance keeps its own in memory, which is correct for a single instance. |
| `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | No | | Session lengths (30 minutes / 7 days). |
| `LOGIN_RATE_LIMIT`, `SIGNUP_RATE_LIMIT`, `RATE_LIMIT_WINDOW_SECONDS` | No | | Attempts per IP and per account (10 / 5 per 300 s). |
| `MAX_UPLOAD_BYTES`, `ALLOWED_IMAGE_TYPES` | No | | 10 MB; JPEG, PNG, WebP. |
| `PLANT_CLASSIFIER_ENABLED` | No | | `false` turns off the "is this a plant?" photo check. Thresholds are set at runtime by an admin ([OPERATIONS.md](OPERATIONS.md#photo-screening-thresholds)). |
| `CONTRIBUTOR_TREE_STAGES`, `REVIEWER_BADGE_TIERS` | No | | Badge thresholds; see `.env.example`. The reviewer tiers are still placeholders. |
| `SUPABASE_SUBMISSION_BUCKET`, `SUPABASE_PLANT_BUCKET`, `SUPABASE_AVATAR_BUCKET` | No | | Logical bucket names; the defaults match the existing data. |

## 8. Running it

The image is built from the [`Dockerfile`](Dockerfile). It listens on `$PORT`
(default 8000) and runs as a non-root user. Give it **1 GiB of memory**: the
photo-screening model takes about 110 MB and decoding a large phone photo
needs more; with 512 MB the container is killed under load.

Health checks:

| Path | Meaning | Use for |
|---|---|---|
| `GET /health` | The process is up. Never touches the database. | Liveness probe. |
| `GET /health/ready` | The database answers (`503` if not, within 10 s). | Readiness probe, uptime monitoring. |

API documentation is served by the API itself at `/docs` (Swagger UI) and
`/redoc`.

### Option A: Docker Compose (one server)

[`docker-compose.yml`](docker-compose.yml) runs everything on one machine:
the API, PostgreSQL, Redis and MinIO for photos.

```bash
git clone <repository-url> apl-backend && cd apl-backend
cp .env.example .env
# Edit .env: SECRET_KEY, SMTP_*, EMAIL_FROM, ALLOWED_ORIGINS.
#   Also add (choose your own values):
#     POSTGRES_PASSWORD=...
#     MINIO_ROOT_USER=...
#     MINIO_ROOT_PASSWORD=...
#     PUBLIC_MEDIA_URL=https://media.example.org/apl-media
cp /path/to/handover/*.sql schema/
docker compose up -d --build
docker compose logs -f migrate app
```

On the first start PostgreSQL imports `schema/*.sql`, `minio-init` creates the
`apl-media` bucket with public read access, `migrate` applies `sql/`, then the
API starts on port 8000.

Put a reverse proxy with HTTPS in front (Caddy, nginx, Traefik), forwarding:

- `https://api.example.org` → `localhost:8000` (the API)
- `https://media.example.org` → `localhost:9000` (MinIO, photos); this is
  the host in `PUBLIC_MEDIA_URL`

Do not expose MinIO's console (port 9001) to the internet. PostgreSQL and
Redis are not published on host ports at all; use `docker compose exec
postgres psql -U postgres apl_db` to reach the database.

To use Supabase or an external database or S3 service instead, delete the
matching values from the `environment:` block in `docker-compose.yml` (they
override `.env`) and set them in `.env`.

Updating: `git pull && docker compose up -d --build`. Migrations run first.

### Option B: any container host

Build and push the image to your registry:

```bash
docker build -t <registry>/apl-backend:<version> .
docker push <registry>/apl-backend:<version>
```

Configure the environment variables (section 7) on the platform. Then, on
**every deploy**, run the migrations with the new image **before** routing
traffic to it. New code may read a column that only the new migration adds:

```bash
docker run --rm --env-file .env <registry>/apl-backend:<version> python -m app.db.migrate
```

Most platforms have a "release command", "pre-deploy job" or "init container"
for this. Then start the container with its default command, set the health
checks from the table above, and route HTTPS to its port.

Run one instance, or set `REDIS_URL` before running more (section 7).

### Option C: Google Cloud Run with Cloud Build

This is how the project team ran it. [`cloudbuild.yaml`](cloudbuild.yaml)
builds the image, runs `python -m app.db.migrate` with it, then deploys it to
Cloud Run. A Cloud Build trigger runs it on every push to `dev`.

The file is set up for **Supabase** storage and the team's values (region
`asia-southeast1`, the Supabase project URL, the SMTP mailbox). Change the
`substitutions:` block to yours. The commands below are bash; in PowerShell
replace the `\` line continuations with backticks.

**1. Project and APIs**

```bash
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create apl-backend \
  --repository-format=docker --location=asia-southeast1
```

**2. Secrets.** `cloudbuild.yaml` mounts these Secret Manager secrets; each
must exist or the deploy fails:

| Secret name | Becomes |
|---|---|
| `secret-key` | `SECRET_KEY` |
| `database-url` | `DATABASE_URL` |
| `supabase-secret-key` | `SUPABASE_SERVICE_ROLE_KEY` |
| `smtp-password` | `SMTP_PASSWORD` |
| `resend-api-key` | `RESEND_API_KEY` (create it with any value if unused) |

Create each without a trailing newline, typing the value at the prompt rather
than putting it in your shell history:

```bash
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets create database-url --data-file=- && unset VALUE
```

To change one later: `gcloud secrets versions add <name> --data-file=-`, then
redeploy.

**3. Permissions.** The service account that runs the build and the service
(by default `PROJECT_NUMBER-compute@developer.gserviceaccount.com`) needs
`roles/secretmanager.secretAccessor`, `roles/run.admin`,
`roles/iam.serviceAccountUser`, `roles/artifactregistry.writer` and
`roles/logging.logWriter`:

```bash
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for ROLE in secretmanager.secretAccessor run.admin iam.serviceAccountUser \
            artifactregistry.writer logging.logWriter; do
  gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="serviceAccount:${SA}" --role="roles/${ROLE}"
done
```

**4. Deploy.** Once by hand: `gcloud builds submit --config=cloudbuild.yaml`.
To deploy on every push, connect the repository under Cloud Build → Triggers,
with branch `^dev$` (or `^main$`) and configuration file `cloudbuild.yaml`.

**Notes**

- `--set-env-vars` and `--set-secrets` in `cloudbuild.yaml` *replace* the
  service's whole configuration on every deploy. A variable added by hand in
  the Cloud Run console disappears on the next deploy; add it to the file.
- **S3 storage on Cloud Run:** add `STORAGE_BACKEND=s3;AWS_S3_BUCKET=...;
  AWS_REGION=...;AWS_S3_ENDPOINT_URL=...;S3_PUBLIC_BASE_URL=...;
  AWS_ACCESS_KEY_ID=...` to `--set-env-vars`, add
  `AWS_SECRET_ACCESS_KEY=<secret-name>:latest` to `--set-secrets`, give the
  `migrate` step the same values (`env:` and `secretEnv:`, with the secret
  listed under `availableSecrets`), and remove the Supabase entries.
- Cloud Run scales to zero when idle; the first request after a quiet period
  takes a few seconds. `--min-instances=1` avoids that for roughly
  US$10–15 a month.
- Logs: Cloud Run → apl-backend → Logs, or
  `gcloud run services logs read apl-backend --region=asia-southeast1`.

## 9. Pointing the app and website at it

- **Mobile app:** set its API base URL to `https://<your-api-host>/api/v1`
  and publish a new build. Photo URLs come from the API, so the app needs no
  storage settings.
- **Website:** add its exact origin (scheme and host, no path) to
  `ALLOWED_ORIGINS`, or the browser blocks its requests.

## 10. Checking a deployment

1. `GET /health` → `{"status": "ok"}`.
2. `GET /health/ready` → `{"status": "ok", "database": "ok"}`.
3. The startup log shows `storage_backend` with `supabase` or `s3`, not `none`.
4. `GET /api/v1/plants/count` returns the size of the plant library.
5. Open a plant from `GET /api/v1/plants/` and load one of its `image_url`s
   in a browser.
6. Sign up through the app, upload a submission with a photo, approve it as
   an administrator, and check that the photo appears on the plant and the
   email arrives.

## 11. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits at start: "SECRET_KEY is unset or still a placeholder" | Set `SECRET_KEY` (section 7). |
| `/health` is fine, `/health/ready` is 503 | Database unreachable: wrong `DATABASE_URL`, firewall, TLS requirement (`?ssl=require`), or a paused free-tier database. The log line `readiness_check_failed` has the cause. |
| "Could not parse SQLAlchemy URL" | The URL is not `postgresql+asyncpg://...`, or the password has an unencoded `@`, `:` or `/`. |
| `migrate` fails with "relation ... does not exist" | The hand-over schema was not imported first (section 3.1). |
| `migrate` fails with "S3 bucket ... is not reachable" | The bucket does not exist, the keys are wrong, or the endpoint/region is wrong. |
| Errors mentioning "prepared statement" on Supabase | `DATABASE_URL` uses the pooler but not the `pooler.supabase.com` host the backend detects; use the pooler string exactly as Supabase gives it. |
| Uploads succeed but photos never appear | The log says `storage_backend_disabled`: storage settings missing (section 4). |
| Photo URLs return 403 or 404 | The bucket is not publicly readable, or `S3_PUBLIC_BASE_URL` is not the bucket's real public address. |
| No emails arrive | Look for `email_send_failed`, `email_send_error` or `email_backend_disabled` in the log. Check the SMTP password and that `EMAIL_FROM` is the login mailbox, then the recipient's spam folder (SPF/DKIM/DMARC). |
| Website gets CORS errors, the app works | Add the website's origin to `ALLOWED_ORIGINS`. |
| Container restarts under load | Not enough memory; give it 1 GiB. |
