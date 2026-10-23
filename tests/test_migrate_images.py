"""Moving photos out of the original Supabase project: app/db/migrate_images.py.

A deployment built from an imported database still points at the original
project's storage. The script copies each photo into the configured storage
and rewrites the URL. These tests run it against an in-memory stand-in for
the database, a recording S3 client and a mocked download host.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from app.config import settings
from app.db import migrate_images
from app.services.storage_service import StorageService

OLD = "https://old.supabase.co/storage/v1/object/public"
NEW = "https://media.example.org"
THUMB = "https://images.ala.org.au/image/abc.jpg"

#: Objects in the fake S3 bucket, by full key; reset for every test.
STORED: dict[str, bytes] = {}


class FakeDb:
    """The image columns as {(table, column): {id: url}}; answers the script's SQL."""

    def __init__(self, tables: dict[tuple[str, str], dict[int, str | None]]) -> None:
        self.tables = tables

    async def fetch(self, sql: str) -> list[dict[str, Any]]:
        match = re.search(r"SELECT id, (\w+) AS url FROM (\w+)", sql)
        assert match
        rows = self.tables.get((match[2], match[1]), {})
        return [{"id": i, "url": u} for i, u in sorted(rows.items()) if u]

    async def execute(self, sql: str, new: str, row_id: int, old: str) -> None:
        match = re.search(r"UPDATE (\w+) SET (\w+) = \$1", sql)
        assert match
        rows = self.tables[(match[1], match[2])]
        if rows[row_id] == old:
            rows[row_id] = new

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


@pytest.fixture
def puts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """S3 is the target; returns the put_object calls it receives."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", "apl-media")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", NEW)
    STORED.clear()
    calls: list[dict[str, Any]] = []

    class _Body:
        def __init__(self, data: bytes) -> None:
            self.data = data

        async def __aenter__(self) -> _Body:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            pass

        async def read(self) -> bytes:
            return self.data

    class _Client:
        async def put_object(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            STORED[kwargs["Key"]] = kwargs["Body"]
            return {}

        async def get_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"Body": _Body(STORED[kwargs["Key"]]), "ContentType": "image/jpeg"}

    @asynccontextmanager
    async def _client(self: Any) -> AsyncIterator[_Client]:
        yield _Client()

    monkeypatch.setattr(StorageService, "_s3", _client)
    return calls


def _http(gone: set[str] | None = None) -> tuple[httpx.AsyncClient, list[str]]:
    """A download client serving every URL as a JPEG except those in `gone`."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched.append(url)
        if url in (gone or set()):
            return httpx.Response(404)
        return httpx.Response(
            200, content=b"jpeg:" + url.encode(), headers={"content-type": "image/jpeg"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), fetched


def _library() -> FakeDb:
    return FakeDb(
        {
            ("plant_images_v2", "image_url"): {
                1: f"{OLD}/plant-images-v2/001.jpg",
                2: f"{OLD}/plant-images-v2/plants/7/b.jpg",
            },
            ("plant_images_v2", "thumbnail_url"): {1: THUMB, 2: None},
            ("submission_images", "image_url"): {5: f"{OLD}/submission-images/submissions/9/a.jpg"},
            ("app_users", "avatar_url"): {3: f"{OLD}/avatars/users/3/c.jpg", 4: None},
        }
    )


@pytest.mark.asyncio
async def test_every_supabase_photo_moves_to_its_bucket_and_the_row_follows(
    puts: list[dict[str, Any]],
) -> None:
    db = _library()
    http, _ = _http()
    report = await migrate_images.migrate_images(db, StorageService(), http)

    assert report.copied == 4
    assert report.failed == []
    assert db.tables[("plant_images_v2", "image_url")] == {
        1: f"{NEW}/plant-images-v2/001.jpg",
        2: f"{NEW}/plant-images-v2/plants/7/b.jpg",
    }
    assert db.tables[("submission_images", "image_url")] == {
        5: f"{NEW}/submission-images/submissions/9/a.jpg"
    }
    assert db.tables[("app_users", "avatar_url")] == {3: f"{NEW}/avatars/users/3/c.jpg", 4: None}
    # The bytes stored are the bytes downloaded, under the same key.
    put = next(p for p in puts if p["Key"] == "avatars/users/3/c.jpg")
    assert put["Body"] == f"jpeg:{OLD}/avatars/users/3/c.jpg".encode()
    assert put["ContentType"] == "image/jpeg"


@pytest.mark.asyncio
async def test_urls_hosted_elsewhere_are_left_alone(puts: list[dict[str, Any]]) -> None:
    """The curated thumbnails live on an external image service, not in our storage."""
    db = _library()
    http, fetched = _http()
    await migrate_images.migrate_images(db, StorageService(), http)
    assert db.tables[("plant_images_v2", "thumbnail_url")] == {1: THUMB, 2: None}
    assert THUMB not in fetched


@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(puts: list[dict[str, Any]]) -> None:
    db = _library()
    http, _ = _http()
    await migrate_images.migrate_images(db, StorageService(), http)
    uploads = len(puts)

    again = await migrate_images.migrate_images(db, StorageService(), http)

    assert (again.copied, again.already_moved, again.failed) == (0, 4, [])
    assert len(puts) == uploads


@pytest.mark.asyncio
async def test_a_dry_run_reports_without_touching_anything(puts: list[dict[str, Any]]) -> None:
    db = _library()
    before = {k: dict(v) for k, v in db.tables.items()}
    http, fetched = _http()

    report = await migrate_images.migrate_images(db, StorageService(), http, dry_run=True)

    assert report.copied == 4
    assert db.tables == before
    assert puts == []
    assert fetched == []


@pytest.mark.asyncio
async def test_a_photo_that_will_not_download_is_reported_and_the_rest_carry_on(
    puts: list[dict[str, Any]],
) -> None:
    db = _library()
    gone = f"{OLD}/plant-images-v2/001.jpg"
    http, _ = _http(gone={gone})

    report = await migrate_images.migrate_images(db, StorageService(), http)

    assert report.failed == ["plant_images_v2.image_url id=1"]
    assert report.copied == 3
    assert db.tables[("plant_images_v2", "image_url")][1] == gone


@pytest.mark.asyncio
async def test_a_photo_used_by_two_rows_is_copied_once(puts: list[dict[str, Any]]) -> None:
    shared = f"{OLD}/plant-images-v2/shared.jpg"
    db = FakeDb({("plant_images_v2", "image_url"): {1: shared, 2: shared}})
    http, fetched = _http()

    await migrate_images.migrate_images(db, StorageService(), http)

    assert fetched == [shared]
    assert set(db.tables[("plant_images_v2", "image_url")].values()) == {
        f"{NEW}/plant-images-v2/shared.jpg"
    }


@pytest.mark.asyncio
async def test_it_refuses_to_run_without_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", "")
    monkeypatch.setattr(
        migrate_images, "get_engine", lambda: pytest.fail("must not touch the database")
    )
    assert await migrate_images.main([]) == 2


@pytest.mark.parametrize(
    ("url", "key"),
    [
        (f"{OLD}/plant-images-v2/plants/7/b.jpg", "plants/7/b.jpg"),
        (f"{OLD}/avatars/users/3/c.jpg?t=123", "users/3/c.jpg"),
        (f"{OLD}/avatars/", None),
        (THUMB, None),
    ],
)
def test_the_key_comes_from_the_supabase_url(url: str, key: str | None) -> None:
    assert migrate_images.supabase_key(url) == key


@pytest.mark.asyncio
async def test_supabase_uploads_overwrite_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-run must overwrite what a failed run left behind; the app must never."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "supabase")
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "service-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    service = StorageService()
    await service.upload_bytes(b"x", "a.jpg", "avatars", "image/jpeg")
    await service.upload_bytes(b"x", "a.jpg", "avatars", "image/jpeg", upsert=True)

    assert [r.headers.get("x-upsert") for r in seen] == [None, "true"]


# ── metadata ─────────────────────────────────────────────────────────────────


def _jpeg_with_gps() -> bytes:
    import io

    from PIL import Image

    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif.get_ifd(0x8825).update({1: "S", 2: (35.0, 16.0, 50.0)})
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(out, "JPEG", exif=exif)
    return out.getvalue()


def _serving(photos: dict[str, bytes]) -> tuple[httpx.AsyncClient, list[str]]:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(
            200, content=photos[str(request.url)], headers={"content-type": "image/jpeg"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), fetched


@pytest.mark.asyncio
async def test_a_moved_photo_loses_its_location_on_the_way(puts: list[dict[str, Any]]) -> None:
    source = f"{OLD}/plant-images-v2/gps.jpg"
    db = FakeDb({("plant_images_v2", "image_url"): {1: source}})
    http, _ = _serving({source: _jpeg_with_gps()})

    await migrate_images.migrate_images(db, StorageService(), http)

    assert b"PhoneMaker" not in puts[0]["Body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True], ids=["for real", "dry run"])
async def test_photos_already_here_are_cleaned_in_place_when_asked(
    puts: list[dict[str, Any]], dry_run: bool
) -> None:
    with_gps = f"{NEW}/plant-images-v2/plants/1/gps.jpg"
    clean = f"{NEW}/submission-images/submissions/2/clean.jpg"
    db = FakeDb(
        {
            ("plant_images_v2", "image_url"): {1: with_gps},
            ("submission_images", "image_url"): {2: clean},
        }
    )
    import io

    from PIL import Image

    plain = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(plain, "JPEG")
    STORED["plant-images-v2/plants/1/gps.jpg"] = _jpeg_with_gps()
    STORED["submission-images/submissions/2/clean.jpg"] = plain.getvalue()
    http, fetched = _serving({})

    report = await migrate_images.migrate_images(
        db, StorageService(), http, dry_run=dry_run, strip_in_place=True
    )

    assert report.stripped == 1
    # Read through the storage API, never this server's own public URL.
    assert fetched == []
    if dry_run:
        assert puts == []
    else:
        # Same key, so the URL in the row stays valid; only the dirty one is rewritten.
        assert [p["Key"] for p in puts] == ["plant-images-v2/plants/1/gps.jpg"]
        assert b"PhoneMaker" not in puts[0]["Body"]
    assert db.tables[("plant_images_v2", "image_url")] == {1: with_gps}


@pytest.mark.asyncio
async def test_without_the_flag_photos_already_here_are_not_downloaded(
    puts: list[dict[str, Any]],
) -> None:
    here = f"{NEW}/plant-images-v2/plants/1/gps.jpg"
    db = FakeDb({("plant_images_v2", "image_url"): {1: here}})
    http, fetched = _serving({here: _jpeg_with_gps()})

    report = await migrate_images.migrate_images(db, StorageService(), http)

    assert (report.already_moved, report.stripped, fetched, puts) == (1, 0, [], [])
