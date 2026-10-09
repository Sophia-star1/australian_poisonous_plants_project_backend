"""The pre-deploy migration step: sql/ applied once each, buckets ensured."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.config import settings
from app.db import migrate
from app.services.storage_service import StorageError, StorageService

REPO = Path(__file__).resolve().parents[1]


class _Transaction:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.log.append("BEGIN")

    async def __aexit__(self, exc_type: Any, *exc: Any) -> None:
        if exc_type is None:
            self.conn.ledger |= self.conn.pending
            self.conn.log.append("COMMIT")
        else:
            self.conn.log.append("ROLLBACK")
        self.conn.pending = set()


class FakeConnection:
    """Enough of asyncpg.Connection, with transactions that commit or roll back."""

    def __init__(self, ledger: set[str] | None = None, fail_on: str | None = None) -> None:
        self.ledger = set(ledger or ())
        self.pending: set[str] = set()
        self.fail_on = fail_on
        self.log: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, sql: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in sql:
            self.log.append("LOCK")
        elif sql.startswith("INSERT INTO schema_migrations"):
            self.pending.add(args[0])
            self.log.append(f"RECORD {args[0]}")
        elif "CREATE TABLE IF NOT EXISTS schema_migrations" in sql:
            self.log.append("LEDGER")
        else:
            if self.fail_on and self.fail_on in sql:
                raise RuntimeError("syntax error")
            self.log.append(f"RUN {sql.strip()}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return 1 if args[0] in self.ledger else None


@pytest.fixture
def sql_dir(tmp_path: Path) -> Path:
    for name, body in [("002_b.sql", "B"), ("001_a.sql", "A"), ("003_c.sql", "C")]:
        (tmp_path / name).write_text(body)
    return tmp_path


@pytest.mark.asyncio
async def test_new_scripts_run_in_filename_order_and_are_recorded(sql_dir: Path) -> None:
    conn = FakeConnection()
    applied = await migrate.apply_migrations(conn, sql_dir)
    assert applied == ["001_a.sql", "002_b.sql", "003_c.sql"]
    assert conn.ledger == set(applied)
    assert [e for e in conn.log if e.startswith("RUN")] == ["RUN A", "RUN B", "RUN C"]


@pytest.mark.asyncio
async def test_recorded_scripts_are_skipped_so_a_second_run_does_nothing(sql_dir: Path) -> None:
    conn = FakeConnection(ledger={"001_a.sql"})
    assert await migrate.apply_migrations(conn, sql_dir) == ["002_b.sql", "003_c.sql"]
    assert await migrate.apply_migrations(conn, sql_dir) == []


@pytest.mark.asyncio
async def test_each_script_runs_locked_inside_its_own_transaction(sql_dir: Path) -> None:
    """The lock is per transaction, which is what survives the transaction pooler."""
    conn = FakeConnection(ledger={"001_a.sql", "002_b.sql"})
    await migrate.apply_migrations(conn, sql_dir)
    # Ledger setup, two skipped scripts, then C applied with its record.
    assert conn.log[-5:] == ["BEGIN", "LOCK", "RUN C", "RECORD 003_c.sql", "COMMIT"]
    assert conn.log.count("BEGIN") == conn.log.count("LOCK") == 4


@pytest.mark.asyncio
async def test_a_failing_script_is_not_recorded_and_stops_the_run(sql_dir: Path) -> None:
    conn = FakeConnection(fail_on="B")
    with pytest.raises(RuntimeError, match="syntax error"):
        await migrate.apply_migrations(conn, sql_dir)
    assert conn.ledger == {"001_a.sql"}, "B rolled back with its record; C never ran"
    assert "RUN C" not in conn.log


def test_the_shipped_migrations_are_found_in_order() -> None:
    names = [p.name for p in sorted(migrate.SQL_DIR.glob("*.sql"))]
    assert names[:3] == [
        "001_one_primary_per_plant.sql",
        "002_plants_v2_row_shape.sql",
        "003_app_users_avatar.sql",
    ]


@pytest.mark.asyncio
async def test_main_migrates_through_the_apps_own_engine(
    monkeypatch: pytest.MonkeyPatch, sql_dir: Path
) -> None:
    """The same URL cleaning and pooler settings as the running service.

    A second reading of DATABASE_URL could disagree with session.py's, and
    then the step would fail on a URL the service itself connects with.
    """
    conn = FakeConnection()
    calls: list[str] = []

    class _Raw:
        driver_connection = conn

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            calls.append("connection closed")

        async def get_raw_connection(self) -> _Raw:
            return _Raw()

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            calls.append("engine disposed")

    async def _ensure(self: Any, bucket: str) -> str:
        calls.append(f"bucket {bucket}")
        return "exists"

    monkeypatch.setattr(migrate, "get_engine", lambda: _Engine())
    monkeypatch.setattr(migrate, "SQL_DIR", sql_dir)
    monkeypatch.setattr(migrate.apply_migrations, "__defaults__", (sql_dir,))
    monkeypatch.setattr(StorageService, "ensure_public_bucket", _ensure)

    assert await migrate.main() == 0

    assert conn.ledger == {"001_a.sql", "002_b.sql", "003_c.sql"}
    assert calls == ["connection closed", "engine disposed", "bucket avatars"]


# ── buckets ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A configured StorageService whose HTTP calls are answered by `routes`."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "service-key")

    def _apply(routes: dict[tuple[str, str], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return routes[(request.method, request.url.path)]

        original = httpx.AsyncClient.__init__

        def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        return seen

    return _apply


GET = ("GET", "/storage/v1/bucket/avatars")
POST = ("POST", "/storage/v1/bucket")
MISSING = httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found"})


@pytest.mark.asyncio
async def test_a_public_bucket_that_exists_is_left_alone(storage: Any) -> None:
    seen = storage({GET: httpx.Response(200, json={"id": "avatars", "public": True})})
    assert await StorageService().ensure_public_bucket("avatars") == "exists"
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.asyncio
async def test_a_missing_bucket_is_created_public(storage: Any) -> None:
    seen = storage({GET: MISSING, POST: httpx.Response(200, json={"name": "avatars"})})

    assert await StorageService().ensure_public_bucket("avatars") == "created"

    create = seen[1]
    assert create.headers["authorization"] == "Bearer service-key"
    assert create.read() == b'{"id":"avatars","name":"avatars","public":true}'


@pytest.mark.asyncio
async def test_a_private_bucket_is_reported_not_made_public(storage: Any) -> None:
    """Making a bucket public exposes its contents: a person's decision."""
    seen = storage({GET: httpx.Response(200, json={"id": "avatars", "public": False})})
    assert await StorageService().ensure_public_bucket("avatars") == "exists-private"
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routes",
    [
        {GET: httpx.Response(401, json={"message": "Invalid JWT"})},
        {GET: MISSING, POST: httpx.Response(403, json={"message": "denied"})},
    ],
    ids=["cannot check", "cannot create"],
)
async def test_storage_refusing_fails_the_step(storage: Any, routes: dict) -> None:
    """A failure has to stop the deploy rather than ship code with no bucket."""
    storage(routes)
    with pytest.raises(StorageError):
        await StorageService().ensure_public_bucket("avatars")


@pytest.mark.asyncio
async def test_without_storage_configured_it_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    assert await StorageService().ensure_public_bucket("avatars") == "skipped"


# ── the deploy pipeline ──────────────────────────────────────────────────────


def test_the_pipeline_migrates_before_it_deploys() -> None:
    build = yaml.safe_load((REPO / "cloudbuild.yaml").read_text(encoding="utf-8"))
    steps = build["steps"]
    migrate_at = next(i for i, s in enumerate(steps) if s.get("id") == "migrate")
    deploy_at = next(i for i, s in enumerate(steps) if "deploy" in s.get("args", []))
    push_at = next(i for i, s in enumerate(steps) if "push" in s.get("args", []))
    assert push_at < migrate_at < deploy_at

    step = steps[migrate_at]
    assert "python -m app.db.migrate" in " ".join(step["args"])
    assert set(step["secretEnv"]) == {"DATABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"}
    provided = {s["env"] for s in build["availableSecrets"]["secretManager"]}
    assert provided == set(step["secretEnv"])


def test_the_image_ships_the_migrations() -> None:
    assert "COPY --from=builder /build/sql ./sql" in (REPO / "Dockerfile").read_text()
