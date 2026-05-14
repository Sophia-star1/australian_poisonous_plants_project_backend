# APL Backend

> Enterprise-grade RESTful API scaffold built with FastAPI, designed for cloud-native containerized deployment.

## Tech Stack

| Layer | Tool | Version |
|---|---|---|
| Framework | FastAPI | >=0.111 |
| Server | Uvicorn + Gunicorn | latest |
| Validation | Pydantic v2 | >=2.6 |
| ORM | SQLAlchemy 2.0 (async) | >=2.0 |
| DB Driver | asyncpg | latest |
| Migrations | Alembic | latest |
| Auth | python-jose + passlib | latest |
| Config | pydantic-settings | >=2.0 |
| Object Storage | aioboto3 (S3/MinIO) | latest |
| Cache | redis (hiredis) | >=5.0 |
| Logging | structlog (JSON) | latest |
| Task Queue | Celery + Redis | latest |
| Package Manager | uv | latest |

## Project Structure

```
apl_backend/
├── pyproject.toml              # Dependencies & tool config
├── .env.example                # Environment variable template
├── .pre-commit-config.yaml     # Code quality hooks
├── alembic.ini                 # Database migration config
├── Dockerfile                  # Multi-stage build
├── docker-compose.yml          # Local dev environment
├── alembic/                    # Migration scripts
├── app/                        # Application root
│   ├── main.py                 # App entry point
│   ├── config.py               # Settings management
│   ├── dependencies.py         # Dependency injection
│   ├── api/v1/                 # API routes (auth, users, files)
│   ├── core/                   # Security, logging, exceptions
│   ├── db/                     # Database engine & sessions
│   ├── models/                 # ORM models
│   ├── schemas/                # Pydantic schemas
│   ├── services/               # Business logic layer
│   ├── middleware/             # Middleware (CORS, request logging)
│   └── worker/                 # Celery async tasks
├── tests/                      # Test scaffold
└── k8s/                        # Kubernetes manifests
```

## Prerequisites

- Python >= 3.12
- Docker & Docker Compose
- uv (recommended) or pip

## Quick Start

### 1. Clone and configure environment

```bash
git clone <repo-url> && cd apl_backend
cp .env.example .env
# Edit .env with your actual values
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Start infrastructure services

```bash
docker-compose up -d postgres redis minio
```

### 4. Run database migrations

```bash
alembic upgrade head
```

### 5. Start the development server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Once running, visit:

- API Docs: http://localhost:8000/docs
- Health Check: http://localhost:8000/health
- MinIO Console: http://localhost:9001

## Docker Deployment

### Full-stack local launch

```bash
docker-compose up -d
```

### Production build

```bash
docker build -t apl-backend:latest .
```

## Celery Workers

```bash
celery -A app.worker.celery_app:celery worker --loglevel=info
```

## Kubernetes Deployment

```bash
kubectl apply -f k8s/
```

Manifests include: Deployment (2 replicas), ClusterIP Service, Traefik IngressRoute, ConfigMap, Secret, and HPA (scales at 50% CPU).

## Testing

```bash
pytest tests/ -v
```

## Code Quality

```bash
# Install pre-commit hooks
pre-commit install

# Manual checks
ruff check .
ruff format .
mypy app/
```

## API Response Format

All endpoints return a unified `ApiResponse[T]` envelope:

```json
{
  "code": 200,
  "message": "success",
  "data": { ... }
}
```

Paginated endpoints return `PageResult[T]` inside `data`:

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

## License

MIT
