# APL Backend

> Enterprise-grade RESTful API scaffold built with FastAPI, designed for cloud-native containerized deployment.

## Tech Stack

| Layer | Tool | Version | Required |
|---|---|---|---|
| Framework | FastAPI | >=0.111 | Yes |
| Server | Uvicorn (dev) / Gunicorn (prod) | latest | Yes |
| Validation | Pydantic v2 | >=2.6 | Yes |
| ORM | SQLAlchemy 2.0 (async) | >=2.0 | Yes |
| DB Driver | asyncpg | latest | Yes |
| Database | PostgreSQL (Supabase or local) | 16+ | Yes |
| Migrations | Alembic | latest | Yes |
| Auth | python-jose + passlib | latest | Yes |
| Config | pydantic-settings | >=2.0 | Yes |
| Logging | structlog (JSON) | latest | Yes |
| Package Manager | uv | latest | Yes |
| Object Storage | aioboto3 (S3/MinIO) | latest | No |
| Cache | redis (hiredis) | >=5.0 | No |
| Task Queue | Celery + Redis | latest | No |

## Project Structure

```
apl_backend/
│
├── pyproject.toml                 # Dependencies, build config, tool settings (ruff/mypy/pytest)
├── .env.example                   # Environment variable template (copy to .env)
├── .env                           # Actual environment config (git-ignored, never commit)
├── .gitignore                     # Git ignore rules
├── .pre-commit-config.yaml        # Pre-commit hooks: ruff lint/format + mypy
├── alembic.ini                    # Alembic migration runner config
├── Dockerfile                     # Multi-stage production build (builder + runtime)
├── docker-compose.yml             # Full-stack local orchestration (production)
│
├── alembic/                       # Database migration scripts
│   ├── env.py                     # Async migration environment (reads DATABASE_URL from .env)
│   └── versions/                  # Auto-generated migration files (empty until first migration)
│
├── app/                           # ──── Application source code ────
│   ├── __init__.py
│   ├── main.py                    # App factory: lifespan, middleware, routers, /health endpoint
│   ├── config.py                  # Settings class (pydantic-settings, reads from .env)
│   ├── dependencies.py            # FastAPI Depends: get_current_user, require_role, etc.
│   │
│   ├── api/                       # ── HTTP layer (routes only, no business logic) ──
│   │   ├── __init__.py
│   │   ├── router.py              # Aggregates all v1 routers under /api/v1
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── auth.py            # POST /auth/login, /auth/refresh, /auth/logout
│   │       ├── users.py           # GET/POST/PATCH/DELETE /users
│   │       └── files.py           # POST /files/upload, GET /files/{key}/url, DELETE /files/{key}
│   │
│   ├── core/                      # ── Cross-cutting infrastructure ──
│   │   ├── __init__.py
│   │   ├── security.py            # JWT create/verify, password hash/verify (python-jose + passlib)
│   │   ├── logging.py             # structlog JSON config with request_id processor
│   │   └── exceptions.py          # AppException hierarchy + global exception handlers
│   │
│   ├── db/                        # ── Database connectivity ──
│   │   ├── __init__.py
│   │   ├── session.py             # AsyncEngine, AsyncSessionLocal, get_db() dependency
│   │   └── base.py                # DeclarativeBase, TimestampMixin (created_at, updated_at)
│   │
│   ├── models/                    # ── SQLAlchemy ORM models ──
│   │   ├── __init__.py
│   │   └── user.py                # User: id, email, hashed_password, is_active, role
│   │
│   ├── schemas/                   # ── Pydantic request/response schemas ──
│   │   ├── __init__.py
│   │   ├── common.py              # ApiResponse[T] envelope, PageResult[T], success()/error() helpers
│   │   ├── auth.py                # LoginRequest, TokenResponse, RefreshRequest
│   │   ├── user.py                # UserCreate, UserUpdate, UserOut
│   │   └── file.py                # FileUploadResponse, FileMetadata
│   │
│   ├── services/                  # ── Business logic layer ──
│   │   ├── __init__.py
│   │   ├── auth_service.py        # AuthService: authenticate, refresh, revoke
│   │   ├── user_service.py        # UserService: CRUD + pagination
│   │   └── storage_service.py     # StorageService: S3 upload, delete, presigned URL
│   │
│   ├── middleware/                 # ── ASGI middleware ──
│   │   ├── __init__.py
│   │   ├── logging_middleware.py   # Injects X-Request-ID, logs method/path/status/duration
│   │   └── cors_middleware.py      # CORS config from ALLOWED_ORIGINS setting
│   │
│   └── worker/                    # ── Celery async tasks (optional) ──
│       ├── __init__.py
│       ├── celery_app.py          # Celery instance (only created when CELERY_BROKER_URL is set)
│       └── tasks/
│           ├── __init__.py
│           ├── email.py           # send_email_task
│           └── report.py          # generate_report_task
│
├── tests/                         # ── Test scaffold ──
│   ├── __init__.py
│   ├── conftest.py                # Fixtures: async_client (httpx + ASGITransport)
│   ├── test_auth.py               # Auth endpoint test stubs
│   ├── test_users.py              # User CRUD test stubs
│   └── test_files.py              # File endpoint test stubs
│
└── k8s/                           # ── Kubernetes manifests (production) ──
    ├── deployment.yaml            # 2-replica Deployment with readiness/liveness probes
    ├── service.yaml               # ClusterIP Service (80 → 8000)
    ├── ingress.yaml               # Traefik IngressRoute with TLS
    ├── configmap.yaml             # Non-sensitive config values
    ├── secret.yaml                # Sensitive values (base64 placeholders)
    └── hpa.yaml                   # HorizontalPodAutoscaler (CPU 50%, 2–10 replicas)
```

## Prerequisites

- **Python >= 3.12** — download from [python.org](https://www.python.org/downloads/)
- **uv** — fast Python package manager, install with `pip install uv`
- **PostgreSQL database** — we recommend [Supabase](https://supabase.com) (free tier) for development; no local database install needed

## Quick Start (Development)

The project is designed to start with minimal setup. Redis, MinIO, Celery, and Docker are all optional and can be enabled later when needed.

### Step 1 — Clone and configure environment

```bash
git clone <repo-url>
cd apl_backend
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Open `.env` and set your `DATABASE_URL`. If you're using Supabase, find the connection string at **Supabase Dashboard → Project Settings → Database → Connection string → URI**, then change the protocol prefix:

```
# Original from Supabase:
#   postgresql://postgres.xxxxx:password@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
#
# Change postgresql:// to postgresql+asyncpg:// for SQLAlchemy async:
DATABASE_URL=postgresql+asyncpg://postgres.xxxxx:password@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
```

### Step 2 — Install dependencies

```bash
uv sync
```

This creates a `.venv` virtual environment and installs all packages. All subsequent commands should use `uv run` to execute within this environment.

### Step 3 — Start the development server

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Step 4 — Verify it works

Open your browser:

| URL | What you'll see |
|---|---|
| http://localhost:8000/health | `{"status": "ok"}` — confirms the server is running |
| http://localhost:8000/docs | Swagger UI — interactive API documentation |
| http://localhost:8000/redoc | ReDoc — alternative API documentation |

### Step 5 (later) — Run database migrations

When you start building features that need database tables, generate and run migrations:

```bash
# Generate a migration from your models
uv run alembic revision --autogenerate -m "create users table"

# Apply migrations to the database
uv run alembic upgrade head
```

## Environment Variables

All configuration is managed through the `.env` file. Here's what each variable does:

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_NAME` | No | `apl-backend` | Application display name |
| `DEBUG` | No | `true` | Enable debug mode (auto-reload, verbose errors) |
| `SECRET_KEY` | **Yes** | `change-me` | Secret for signing JWT tokens — **change in production** |
| `ALGORITHM` | No | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `30` | Access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | No | `7` | Refresh token lifetime |
| `DATABASE_URL` | **Yes** | localhost | PostgreSQL async connection string |
| `ALLOWED_ORIGINS` | No | `["http://localhost:3000"]` | CORS allowed origins (JSON array) |
| `REDIS_URL` | No | *(empty)* | Redis connection string — leave empty to skip |
| `AWS_ACCESS_KEY_ID` | No | *(empty)* | S3/MinIO access key — leave empty to skip |
| `AWS_SECRET_ACCESS_KEY` | No | *(empty)* | S3/MinIO secret key |
| `AWS_S3_BUCKET` | No | *(empty)* | S3 bucket name |
| `AWS_S3_ENDPOINT_URL` | No | *(empty)* | S3 endpoint (set for MinIO, omit for AWS) |
| `CELERY_BROKER_URL` | No | *(empty)* | Celery broker URL — leave empty to skip |

## Architecture Overview

```
Client Request
    │
    ▼
┌─────────────────────────────┐
│  Middleware                  │
│  (CORS, Request Logging)    │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  API Layer (app/api/v1/)    │  ← Routes only, delegates to services
│  auth.py, users.py, files.py│
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Service Layer              │  ← Business logic, validation
│  (app/services/)            │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Data Layer                 │  ← ORM models, DB session
│  (app/models/, app/db/)     │
└──────────┬──────────────────┘
           │
           ▼
      PostgreSQL (Supabase)
```

The request flow follows a strict layered pattern: **Routes → Services → Models/DB**. Routes handle HTTP concerns (parsing, response formatting), services contain business logic, and models define the database schema.

## Common Development Commands

```bash
# Start dev server
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Run tests
uv run pytest tests/ -v

# Lint and format
uv run ruff check .
uv run ruff format .

# Type checking
uv run mypy app/

# Generate a new migration
uv run alembic revision --autogenerate -m "describe your change"

# Apply migrations
uv run alembic upgrade head

# Rollback last migration
uv run alembic downgrade -1

# Install pre-commit hooks (one-time setup)
uv run pre-commit install
```

## Production Deployment

When you're ready to deploy, the project includes full Docker and Kubernetes support.

### Docker Compose (full stack)

```bash
copy .env.example .env
# Edit .env with production values (DEBUG=false, real SECRET_KEY, etc.)
docker-compose up -d
```

This starts: the FastAPI app, PostgreSQL 16, Redis 7, MinIO, and a Celery worker.

### Docker (build image only)

```bash
docker build -t apl-backend:latest .
```

The Dockerfile uses multi-stage builds: a `builder` stage installs dependencies with `uv`, then a slim `runtime` stage copies only the virtual environment and app code. The final image runs as a non-root user with Gunicorn + Uvicorn workers.

### Kubernetes

```bash
kubectl apply -f k8s/
```

The `k8s/` directory provides a production-ready manifest set: Deployment (2 replicas with health probes), ClusterIP Service, Traefik IngressRoute with TLS, ConfigMap, Secret (base64 placeholders), and HPA that scales from 2 to 10 pods at 50% CPU utilization.

## API Response Format

All endpoints return a unified `ApiResponse[T]` envelope:

```json
{
  "code": 200,
  "message": "success",
  "data": { ... }
}
```

Paginated endpoints wrap results in `PageResult[T]`:

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "items": [...],
    "total": 100,
    "page": 1,
    "page_size": 20
  }
}
```

Error responses follow the same structure:

```json
{
  "code": 404,
  "message": "Resource not found",
  "data": null
}
```

## License

MIT
