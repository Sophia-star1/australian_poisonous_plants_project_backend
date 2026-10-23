# Operating the APL backend

Day-to-day running of a deployed backend. Setting one up is in
[DEPLOY.md](DEPLOY.md).

Every admin action below is an API call. The interactive documentation at
`https://<api-host>/docs` is the easiest way to make one: call
`POST /api/v1/auth/login`, copy the `access_token` from the response, click
**Authorize** and paste it in. Responses always have the shape
`{"code": ..., "message": ..., "data": ...}`.

## Contents

- [Accounts and roles](#accounts-and-roles)
- [Reviewing submissions](#reviewing-submissions)
- [Managing the plant library](#managing-the-plant-library)
- [Photo screening thresholds](#photo-screening-thresholds)
- [Logs and monitoring](#logs-and-monitoring)
- [Backups](#backups)
- [Secrets](#secrets)
- [Changing the code](#changing-the-code)
- [Changing the database schema](#changing-the-database-schema)
- [Known issues and limitations](#known-issues-and-limitations)

## Accounts and roles

There are two roles: `user` (everyone who signs up) and `admin` (can review
submissions, edit and delete plants, manage roles and screening).

### First administrator

No account is an administrator after a fresh deployment, and only an
administrator can promote someone. Sign up through the app, then promote that
account once in the database:

```sql
UPDATE app_users SET role = 'admin' WHERE email = 'you@example.org';
```

It takes effect on the next request; there is no need to sign in again.

### Promoting and demoting

```
PATCH /api/v1/users/{user_id}/role        {"role": "admin"}   or   {"role": "user"}
GET   /api/v1/users/?page=1&page_size=20  list accounts (to find an id)
```

You cannot demote yourself, and the last administrator cannot be demoted, so
the system can never be left without one.

### Signing everyone out

Each sign-in lasts 30 minutes, renewable for 7 days. To end every session at
once (for example after a suspected leak), change `SECRET_KEY` and restart.

## Reviewing submissions

Contributors submit photos and a description from the app. Each photo is
screened automatically (see [below](#photo-screening-thresholds)); the
submission then waits for an administrator.

```
GET   /api/v1/submissions/?status=pending          the queue (also: approved, rejected)
GET   /api/v1/submissions/{id}                     one submission, with content_version
PATCH /api/v1/submissions/{id}                     approve or reject
```

Approve as a **new plant**:

```json
{
  "status": "approved",
  "expected_version": "<content_version from GET>",
  "reviewer_notes": "Thanks, a clear photo of the seed pods.",
  "plant": {"botanical_name": "Abrus precatorius", "common_names": ["Crab's eye"]}
}
```

Approve as another observation of an **existing plant** (its photos join that
plant's gallery):

```json
{"status": "approved", "expected_version": "...", "plant_id": 42}
```

Reject:

```json
{"status": "rejected", "expected_version": "...", "reviewer_notes": "This is a photo of a fern, not ..."}
```

- The contributor is emailed the outcome, including `reviewer_notes`.
- `409` means the contributor changed the submission after you opened it, or
  someone else reviewed it first, or (for a new plant) a plant with that
  botanical name already exists, in which case approve into it with
  `plant_id`. Reload and decide again.
- Approval copies the photos into the plant library in the same step; if the
  copy fails, nothing is approved.

## Managing the plant library

```
GET    /api/v1/plants/count        how many plants are published
GET    /api/v1/plants/search?q=…   search as the app does
PATCH  /api/v1/plants/{id}         correct an entry (send only the fields to change)
DELETE /api/v1/plants/{id}         delete an entry for good
```

`DELETE` removes the entry, its image rows and its photos in the plant
bucket. **There is no undo**, so take a [backup](#backups) first if in doubt.
Submissions that were approved into it keep their own photos.

## Photo screening thresholds

Every uploaded photo is scored by a small "is this a plant?" model (0 to 1):

- below `reject_below` (default **0.50**): the upload is refused;
- from `reject_below` up to `accept_at` (default **0.70**): accepted, and
  flagged for the reviewer;
- at or above `accept_at`: accepted.

A submission is refused if **any** of its photos scores below `reject_below`.

```
GET   /api/v1/admin/screening
PATCH /api/v1/admin/screening     {"reject_below": 0.4, "accept_at": 0.7}
```

Changes reach every instance within about 30 seconds, without a redeploy.
Lowering `reject_below` refuses fewer honest close-ups (bark, seeds, dried
specimens) but lets more non-plant photos through to reviewers. To turn
screening off entirely, set `PLANT_CLASSIFIER_ENABLED=false` and redeploy.

## Logs and monitoring

The API writes one JSON object per line to standard output; your platform
collects it (Cloud Run: Logs tab; Docker: `docker compose logs app`). Every
request is logged with method, path, status, duration and an `X-Request-ID`,
which is also returned to the client.

Useful events to search for:

| Event | Meaning |
|---|---|
| `storage_backend` | Logged at startup: which photo storage is in use. |
| `storage_backend_disabled` | No photo storage is configured; uploads are discarded. Fix immediately. |
| `storage_upload_failed`, `storage_copy_failed` | Storage refused a write; the request failed. |
| `email_sent` / `email_send_failed` / `email_send_error` | Review emails. |
| `readiness_check_failed` | The database did not answer `/health/ready`. |
| `migration_applied`, `migrations_done` | Deploy-time migrations. |
| `unhandled_exception` | A bug: a request failed with 500. |

Monitoring: point an uptime checker at `GET /health/ready` (200 when the API
and database are up, 503 otherwise).

## Backups

Two things hold data: the database and the photo storage.

**Database.** Managed databases (Supabase paid plans, RDS, Cloud SQL) have
automatic backups; check they are on and how long they are kept. Otherwise
run a nightly dump and keep copies off the server:

```bash
pg_dump --format=custom --no-owner "postgresql://USER:PASSWORD@HOST:5432/DBNAME" > apl-$(date +%F).dump
# restore into an empty database:
pg_restore --no-owner --dbname="postgresql://USER:PASSWORD@HOST:5432/NEWDB" apl-YYYY-MM-DD.dump
```

With Docker Compose: `docker compose exec postgres pg_dump -U postgres
--format=custom apl_db > apl-$(date +%F).dump`.

**Photos.** Turn on object versioning where the service offers it (S3, R2,
MinIO), or copy the bucket elsewhere periodically, e.g. with
[rclone](https://rclone.org) `rclone sync`. Supabase Storage has no backup
of its own; copy the three buckets with rclone through Supabase's
S3-compatible endpoint.

Test a restore at least once. A backup that has never been restored is a hope.

## Secrets

| Secret | If it leaks |
|---|---|
| `SECRET_KEY` | Anyone can sign in as any account, administrators included. Replace it and restart (signs everyone out). |
| `DATABASE_URL` (its password) | Full access to all data. Change the database user's password, then update the secret. |
| `SUPABASE_SERVICE_ROLE_KEY` / `AWS_SECRET_ACCESS_KEY` | Anyone can read, replace or delete every photo. Rotate the key in Supabase / your S3 provider, then update the secret. |
| `SMTP_PASSWORD` | Anyone can read the mailbox and send as it. Change the mailbox password. |

After changing a secret, redeploy or restart so the API picks it up. Never
commit a secret to the repository; the `.env` file is git-ignored for that
reason.

### Security checklist

- **If you keep Supabase:** the mobile app once called Supabase directly with
  its public *anon* key, and that key is in the mobile repository's history.
  Anyone holding it can use Supabase's own REST API on every `public` table
  without row-level security. Enable RLS on `app_users`, `submissions`,
  `submission_images` and any other table (Table Editor → table → RLS). The
  backend connects as the table owner, so RLS does not affect it.
- An earlier version of `DEPLOY.md` contained the original database's
  connection string, password included, and it remains in git history.
  **Change that database password** if the database is still in use.
- `DEBUG=true` opens CORS to localhost and private networks and allows the
  placeholder `SECRET_KEY`. Never set it in production.
- Passwords are stored as bcrypt hashes; refresh tokens are single use;
  login gives the same answer, in the same time, whether or not an address
  has an account. Uploaded photos are decoded under a pixel budget, their
  filenames never become storage keys, and avatars are re-encoded without
  metadata (no GPS location).

## Changing the code

- Development setup and commands are in the [README](README.md).
- Every pull request and push to `dev`/`main` runs lint, format, type and
  test checks on GitHub Actions ([`ci.yml`](.github/workflows/ci.yml)). Keep
  them green, and make the check required in branch protection.
- With the Cloud Build trigger, a push to `dev` deploys to production; there
  is no staging environment. Merge only what has passed CI.

### Rolling back

Deploy the previous image again. On Cloud Run every deploy is a revision, and
earlier ones stay available:

```bash
gcloud run revisions list --service=apl-backend --region=asia-southeast1
gcloud run services update-traffic apl-backend --region=asia-southeast1 \
  --to-revisions=<REVISION_NAME>=100
```

With Docker Compose, check out the previous commit and
`docker compose up -d --build`. Database migrations are never undone
automatically, which is why each one must leave the schema usable by the
previous code (next section).

## Changing the database schema

The schema is plain SQL; there is no ORM-generated migration history.

1. Add `sql/NNN_what_it_does.sql`, numbered after the last one.
2. Write it so that running it twice is harmless (`IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`, guarded `DO` blocks); the existing files show
   the pattern.
3. Update the SQLAlchemy model in `app/models/`.
4. Deploy. `python -m app.db.migrate` runs the new file once, before the new
   code starts serving, and records it in `schema_migrations`.

Only *add* in a migration that ships with the code that needs it (a column, a
table, an index). Removing or renaming something the running code still reads
takes two deploys: first code that no longer uses it, then the migration.

## Known issues and limitations

- **Plants with the same name.** Publishing a plant whose botanical name
  already exists is refused, but two approvals of the same *new* name at the
  very same moment can both succeed, because `plants_v2` has no unique
  constraint on the name. Delete the duplicate with `DELETE /plants/{id}`.
- **Free-tier databases pause.** Supabase's free tier pauses a project after
  about a week without activity. The API then answers 503 on
  `/health/ready` and errors on everything else until the project is
  resumed from the dashboard. In 2026 the database was unreachable for a
  month (28 August to 25 September) before anyone noticed. Use a paid tier
  or another host for production, and monitor `/health/ready`.
- **One instance without Redis.** Login rate limits and signed-out tokens are
  kept in memory unless `REDIS_URL` is set. With several instances and no
  Redis, a token signed out on one instance still works on the others until
  it expires, and the rate limit is counted per instance. Instances also
  forget signed-out tokens when they restart.
- **Reviewer badge tiers are placeholders** (`REVIEWER_BADGE_TIERS`); set the
  real values when they are decided. Nothing is stored per user, so changing
  them re-grades everyone at once.
- **Cold starts on Cloud Run.** With `--min-instances=0` the first request
  after an idle period takes a few seconds while the instance starts.
- **Thumbnails are external.** Curated plants' `thumbnail_url`s point at an
  external image service, which this project does not control.
- **No email verification.** Anyone can register with an address they do not
  own. Submissions sent signed out under that address then appear in that
  account.
- **Client IP for rate limiting** is the first `X-Forwarded-For` entry, which
  a client can set itself. Behind a known proxy, use the address the proxy
  appends instead (`_client_ip` in `app/api/v1/auth.py`).
- **`GET /plants/` is not paginated** (it takes a `limit`). Fine at the
  current ~100 plants; add paging as the library grows.
- **Library photos are managed per plant, not per photo.** There are no
  endpoints to set a different cover photo, delete one photo or caption it.
- **Region latency (original deployment).** Cloud Run ran in Singapore and
  the Supabase database in Tokyo, about 70 ms per round trip. Put the API
  and the database in the same region.
