# Australian Poisonous Plants: backend API

The REST API behind the Australian Poisonous Plants mobile app and website. It
serves the poisonous-plant library and lets the public contribute plant
photos, which administrators review before they are published.

- **Plant library:** browse, search and view plants with their toxicity
  details and photos.
- **Accounts:** sign-up and sign-in with tokens, profile name and picture,
  contributor and reviewer badges.
- **Contributions:** photo submissions. Each photo is screened by an on-board
  "is this a plant?" model before it is stored. Contributors can edit a
  submission until it is reviewed.
- **Review:** administrators approve a submission into a new or existing plant
  (its photos join the library) or reject it; the contributor is emailed
  either way.
- **Administration:** roles, plant corrections and deletion, screening
  thresholds.

## Start here

Step-by-step guides, written for readers with no technical background.
Every command is given for Windows and Mac, with what it does and what you
should see:

| Guide | When |
|---|---|
| [Run on your computer](docs/run-on-your-computer.md) | Try everything out safely on a Windows or Mac computer. |
| [Deploy on your own server](docs/deploy-on-your-own-server.md) | Put it online on one rented Linux server, with automatic HTTPS and nightly backups. |
| [Deploy on Google Cloud](docs/deploy-on-google-cloud.md) | Put it online with managed services (Cloud Run and Supabase), with no server to look after. |
| [Everyday tasks](docs/everyday-tasks.md) | Once it is online: reviewing submissions, managing plants and administrators, health checks. |

Technical reference:

| Document | For |
|---|---|
| This README | Developers: the API, local development, the codebase, conventions |
| [DEPLOY.md](DEPLOY.md) | Every setting, storage option and deployment option in detail |
| [OPERATIONS.md](OPERATIONS.md) | Operations background: privacy, logs, backups, security checklist, known issues |
| `https://<api-host>/docs` | Every endpoint, with request and response schemas |

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async) with asyncpg · PostgreSQL ·
Pydantic 2 · ONNX Runtime and libvips for photo screening · Supabase Storage
or any S3-compatible storage · SMTP for email · optional Redis · uv for
dependencies · Docker.

## API overview

Everything is under `/api/v1` except the health checks. The full reference,
with schemas and each endpoint's errors, is at `/docs` and `/redoc` on any
running instance.

**Authentication.** Send `Authorization: Bearer <access_token>`. Access
tokens last 30 minutes; renew with `POST /auth/refresh`. Refresh tokens last
7 days and each works **once**: every refresh returns a new pair. Logout
revokes both. Roles (`user`, `admin`) are read from the account on every
request, so a role change applies immediately.

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/health`, `/health/ready` | public | Liveness; readiness (database answers, else `503`) |
| POST | `/auth/signup`, `/auth/login` | public | Create an account / sign in; returns tokens |
| POST | `/auth/refresh`, `/auth/logout` | public | New token pair / revoke tokens |
| GET | `/auth/me`, `/auth/session` | user | The signed-in account |
| GET | `/plants/`, `/plants/count`, `/plants/{id}` | public | Library, its size, one plant in full |
| GET | `/plants/search?q=&field=&limit=` | public | Ranked search; `field` narrows to named fields |
| PATCH, DELETE | `/plants/{id}` | admin | Correct an entry; delete it with its library photos |
| POST | `/submissions/` | public or user | Submit an observation (multipart, `photos` field) |
| GET | `/submissions/me`, `/submissions/{id}` | user | Your submissions; one (yours, or any for admins) |
| PATCH | `/submissions/{id}/content` | user | Edit your pending submission |
| POST, DELETE | `/submissions/{id}/photos[/{photo_id}]` | user | Add or remove photos while pending |
| GET | `/submissions/` | admin | Review queue, filterable by status, name, email |
| PATCH | `/submissions/{id}` | admin | Approve (new or existing plant) or reject |
| GET | `/users/`, `/users/{id}` | admin | Accounts |
| PATCH | `/users/{id}/role` | admin | Promote or demote (never the last admin) |
| PATCH | `/users/me` | user | Change display name |
| PUT, DELETE | `/users/me/avatar` | user | Set or remove profile picture |
| GET | `/badges/levels`, `/badges/me` | public, user | Badge ladders; your badges |
| GET | `/badges/users/{id}` | admin | Anyone's badges |
| GET, PATCH | `/admin/screening` | admin | Photo-screening thresholds |

**Submission lifecycle.**

```
upload ──► pending ──► approved   plant created or extended, photos copied into the
   │          │                   library, contributor emailed
   │          └─────► rejected    nothing published, contributor emailed
   └─ refused at upload (422) if any photo fails screening
```

While pending, the contributor can edit the text and add or remove photos;
each edit changes `content_version`. A reviewer sends back the
`content_version` they saw as `expected_version`, and a mismatch is refused
with `409`, so nobody approves content they did not see. Reviewed submissions
are read-only.

**Upload limits.** JPEG, PNG or WebP; 10 MB per file (`MAX_UPLOAD_BYTES`);
1–10 photos per submission; 20 upload requests per hour per IP and per
account (`SUBMISSION_RATE_LIMIT`, then `429`). Images over 13 MP are decoded
at about 2 MP by libvips; over 200 MP they are refused unread. Hosting
platforms may cap the request as a whole (Cloud Run: 32 MiB, answered with a
bare `413`), so the app should resize photos to about 2048 px before upload.
Photos are stored without their GPS and other metadata
(`app/services/photo_metadata.py`).

## Running it locally

You need Python 3.12 and [uv](https://docs.astral.sh/uv/)
(`pip install uv`, or see its install guide).

```bash
uv sync                      # creates .venv with the exact locked versions
cp .env.example .env         # Windows: copy .env.example .env
```

Edit `.env`:

- `DEBUG=true` (allows the placeholder `SECRET_KEY` and local CORS origins).
- `DATABASE_URL`: a PostgreSQL database that has the hand-over schema loaded
  (DEPLOY.md, section 3). The simplest is the Compose database. Put the
  schema files in `schema/`, publish its port with a
  `docker-compose.override.yml` (git-ignored):

  ```yaml
  services:
    postgres:
      ports: ["127.0.0.1:5432:5432"]
  ```

  then run `docker compose up -d postgres` and use
  `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/apl_db`.
- Storage and email are optional locally: without them, uploads are discarded
  and emails are written to the log.

```bash
uv run python -m app.db.migrate                  # apply sql/ (once, and after pulling new ones)
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs. To test from a phone on the same Wi-Fi, run
uvicorn with `--host 0.0.0.0` and use the computer's LAN address.

To run the whole stack in containers instead: `docker compose up -d --build`
(see DEPLOY.md, option A).

## Checks

CI runs exactly these on every pull request
([.github/workflows/ci.yml](.github/workflows/ci.yml)); run them before
pushing:

```bash
uv run ruff check app tests
uv run ruff format --check app tests     # without --check to fix
uv run mypy app                          # strict mode
uv run pytest                            # no database or network needed
```

`uv run pre-commit install` runs the first three on every commit.

## Codebase

```
app/
├── main.py              App factory: middleware, error handlers, routers, /health, /health/ready
├── config.py            Every setting, read from the environment / .env (documented inline)
├── dependencies.py      Auth dependencies: get_current_user, get_optional_user, require_admin
├── api/v1/              HTTP layer only: parse, call a service, wrap the result
│   ├── auth.py          signup, login, refresh, logout, session, me
│   ├── plants.py        browse, count, search, detail; admin edit and delete
│   ├── submissions.py   create, own list, edit, photos; admin queue and review
│   ├── users.py         own name and avatar; admin user list and roles
│   ├── badges.py        contributor tree and reviewer badges
│   └── admin.py         photo-screening thresholds
├── services/            Business logic; one class per area (PlantService, SubmissionService, ...)
│   ├── storage_service.py   Supabase Storage / S3 behind one interface
│   └── photo_metadata.py    Removes GPS and other metadata from uploaded photos
├── models/              SQLAlchemy ORM models (tables: plants_v2, plant_images_v2,
│                        submissions, submission_images, app_users, screening_thresholds)
├── schemas/             Pydantic request/response models; common.py has the response envelope
├── core/                Security (JWT, bcrypt), exceptions, structured logging,
│                        rate limiting, token revocation
├── middleware/          CORS, request logging with X-Request-ID
├── ml/                  The plant classifier and its ONNX weights (see app/ml/README.md)
└── db/
    ├── session.py       Engine and sessions (commit on success, rollback on error)
    ├── migrate.py       `python -m app.db.migrate`: applies sql/, checks storage
    └── migrate_images.py  `python -m app.db.migrate_images`: moves photos to new storage
sql/                     Schema changes on top of the hand-over schema, applied in order
schema/                  Where the hand-over schema SQL goes for Docker Compose (not committed)
tests/                   pytest suite
cloudbuild.yaml          Google Cloud Build: build → migrate → deploy to Cloud Run
docker-compose.yml       Self-hosted stack: API, PostgreSQL, Redis, RustFS (S3), Caddy (HTTPS)
deploy/                  Caddyfile (HTTPS and the read-only photo host) and backup.sh
docs/                    Step-by-step guides for running, deploying and operating
```

### Conventions

- **Layers.** Routes stay thin; logic goes in a service; services take an
  `AsyncSession`. The session commits when the request succeeds and rolls
  back on any exception, so a service never commits half a change.
- **Response envelope.** Every endpoint returns
  `{"code": <int>, "message": <str>, "data": <payload or null>}`, errors
  included (`ApiResponse[T]`). Lists are paginated as
  `{"items": [...], "total", "page", "page_size"}` (`PageResult[T]`).
- **Errors.** Raise the `AppException` subclasses in `app/core/exceptions.py`
  (`NotFoundError`, `ConflictError`, ...); the handlers turn them into the
  envelope with the right status code.
- **Schema changes** are plain SQL files in `sql/`, written to be safe to run
  twice; see OPERATIONS.md.
- **Settings** are added to `app/config.py` with a comment saying what they
  are for, and to `.env.example`.
- **Comments explain why.** Much of the code records the incident or
  constraint behind a decision; keep that up when changing it.

### Background

The mobile app first talked to Supabase directly. This backend replaced that
with a REST API and its own authentication, which is why users live in
`app_users` rather than Supabase's `auth.users`. Search ranking is done in
Python (`_score_match` in `app/services/plant_service.py`) to keep the app's
original ordering.

## License

MIT
