"""Tests for the Supabase Storage backend.

The previous implementation read every upload and threw the bytes away while
returning a plausible-looking result, so nothing ever reached a bucket and no
error was raised. These tests pin the behaviour that replaces it.
"""

from typing import Any

import httpx
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.config import settings
from app.services.storage_service import (
    StorageError,
    StorageService,
    build_object_key,
)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Supabase credentials are present."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "service-role-secret")


def _upload(
    name: str = "photo.JPG", data: bytes = b"bytes", ctype: str = "image/jpeg"
) -> UploadFile:
    import io

    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers=Headers({"content-type": ctype}),
    )


def _mock_transport(recorder: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder["method"] = request.method
        recorder["url"] = str(request.url)
        recorder["headers"] = dict(request.headers)
        recorder["content"] = request.content
        return httpx.Response(status, json={"Key": "ok"})

    return httpx.MockTransport(handler)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Intercept outgoing HTTP so no real request leaves the test."""

    def _apply(status: int = 200) -> dict[str, Any]:
        recorder: dict[str, Any] = {}
        transport = _mock_transport(recorder, status)
        original = httpx.AsyncClient.__init__

        def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        return recorder

    return _apply


# ── key generation ───────────────────────────────────────────────────────────


def test_key_never_reuses_the_client_filename() -> None:
    """Regression: phones name every photo IMG_0001.jpg."""
    first = build_object_key("submissions/abc", "IMG_0001.jpg")
    second = build_object_key("submissions/abc", "IMG_0001.jpg")
    assert first != second
    assert "IMG_0001" not in first


def test_key_keeps_a_recognised_extension() -> None:
    """The suffix survives so content type stays guessable from the key."""
    assert build_object_key("submissions/x", "photo.JPG").endswith(".jpg")
    assert build_object_key("submissions/x", "shot.webp").endswith(".webp")


def test_key_rejects_unexpected_extensions() -> None:
    """An unknown or hostile suffix falls back rather than being echoed."""
    assert build_object_key("submissions/x", "payload.php").endswith(".bin")
    assert build_object_key("submissions/x", None).endswith(".bin")


def test_key_cannot_escape_its_prefix() -> None:
    """A traversal attempt in the filename cannot alter the path."""
    key = build_object_key("submissions/abc", "../../etc/passwd.jpg")
    assert key.startswith("submissions/abc/")
    assert ".." not in key


# ── uploads ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_upload_posts_to_supabase(capture: Any) -> None:
    """The bytes actually go somewhere now."""
    recorder = capture()
    service = StorageService()
    result = await service.upload_file(_upload(), "submissions/a/b.jpg")

    assert recorder["method"] == "POST"
    assert recorder["url"] == (
        "https://proj.supabase.co/storage/v1/object/submission-images/submissions/a/b.jpg"
    )
    assert recorder["content"] == b"bytes"
    assert result.url == (
        "https://proj.supabase.co/storage/v1/object/public/submission-images/submissions/a/b.jpg"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_upload_authenticates_with_the_service_role_key(capture: Any) -> None:
    """Both header forms Supabase expects are sent."""
    recorder = capture()
    await StorageService().upload_file(_upload(), "submissions/a/b.jpg")
    assert recorder["headers"]["authorization"] == "Bearer service-role-secret"
    assert recorder["headers"]["apikey"] == "service-role-secret"


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_upload_failure_raises_rather_than_pretending(capture: Any) -> None:
    """The old code returned success no matter what happened."""
    capture(status=500)
    with pytest.raises(StorageError, match="HTTP 500"):
        await StorageService().upload_file(_upload(), "submissions/a/b.jpg")


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_oversized_upload_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit is enforced while reading, not after buffering everything."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(StorageError, match="upload limit"):
        await StorageService().read_upload(_upload(data=b"x" * 100))


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_empty_upload_is_refused() -> None:
    """A zero-byte photo is not a photo."""
    with pytest.raises(StorageError, match="empty"):
        await StorageService().read_upload(_upload(data=b""))


# ── copy ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_copy_uses_the_server_side_endpoint(capture: Any) -> None:
    """Promotion must not stream bytes through the API server."""
    recorder = capture()
    result = await StorageService().copy_object(
        source_key="submissions/a/b.jpg",
        destination_key="plants/1/b.jpg",
        source_bucket="submission-images",
        destination_bucket="plant-images",
    )

    assert recorder["url"] == "https://proj.supabase.co/storage/v1/object/copy"
    assert recorder["content"] == (
        b'{"bucketId":"submission-images","sourceKey":"submissions/a/b.jpg",'
        b'"destinationBucket":"plant-images","destinationKey":"plants/1/b.jpg"}'
    )
    assert result.url.endswith("/public/plant-images/plants/1/b.jpg")


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_copy_failure_raises(capture: Any) -> None:
    """An approval must not silently lose the photo."""
    capture(status=404)
    with pytest.raises(StorageError, match="HTTP 404"):
        await StorageService().copy_object(
            source_key="a",
            destination_key="b",
            source_bucket="submission-images",
            destination_bucket="plant-images",
        )


# ── delete ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_delete_reports_failure_without_raising(capture: Any) -> None:
    """Deletion is cleanup; it must not fail a request that already succeeded."""
    capture(status=400)
    assert await StorageService().delete_file("submissions/a/b.jpg") is False


# ── URL / key round trip ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("configured")
def test_key_from_url_round_trip() -> None:
    """The database stores URLs; the copy API wants keys."""
    service = StorageService()
    url = service.public_url("submission-images", "submissions/a/b.jpg")
    assert service.key_from_url(url) == "submissions/a/b.jpg"


@pytest.mark.usefixtures("configured")
def test_key_from_url_tolerates_legacy_values() -> None:
    """Rows written before this change hold a bare path, not a URL."""
    assert StorageService().key_from_url("submissions/test1.jpg") == "submissions/test1.jpg"


# ── disabled backend ─────────────────────────────────────────────────────────


# ── credential hygiene ───────────────────────────────────────────────────────


@pytest.mark.parametrize("trailing", ["\n", "\r\n", " ", "\t"])
def test_credentials_survive_a_trailing_newline(trailing: str) -> None:
    """Secret Manager hands back whatever bytes it was given.

    The usual ways of loading a secret append a newline — a PowerShell pipe
    into `--data-file=-`, a here doc, `echo` without `-n`. Unstripped, the key
    went into an `Authorization` header and httpx refused to send it, which
    surfaced as HTTP 500 on every upload with nothing pointing at the secret.
    """
    from app.config import Settings

    parsed = Settings(
        SUPABASE_URL="https://proj.supabase.co" + trailing,
        SUPABASE_SERVICE_ROLE_KEY="sb_secret_abc123" + trailing,
        DATABASE_URL="postgresql+asyncpg://u:p@h/db" + trailing,
    )
    assert parsed.SUPABASE_SERVICE_ROLE_KEY == "sb_secret_abc123"
    assert parsed.SUPABASE_URL == "https://proj.supabase.co"
    assert parsed.DATABASE_URL == "postgresql+asyncpg://u:p@h/db"


@pytest.mark.usefixtures("configured")
def test_header_value_is_always_legal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever reaches the header must be something httpx will send."""
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_abc")
    for value in StorageService()._headers("image/png").values():
        assert "\n" not in value and "\r" not in value


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_transport_failure_becomes_a_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure below the HTTP response must not escape as a bare 500.

    The endpoint catches `StorageError` and answers 422. Anything else reaches
    the generic handler, and the contributor is told "internal server error"
    for what is really a broken deployment.
    """

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)

    with pytest.raises(StorageError, match="Storage request failed"):
        await StorageService().upload_file(_upload(), "submissions/a/b.jpg")

    with pytest.raises(StorageError, match="Storage request failed"):
        await StorageService().copy_object(
            source_key="a",
            destination_key="b",
            source_bucket="submission-images",
            destination_bucket="plant-images",
        )


# ── disabled backend ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_backend_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without credentials the service degrades instead of crashing."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    service = StorageService()
    assert service.enabled is False
    result = await service.upload_file(_upload(), "submissions/a/b.jpg")
    assert result.url == "submissions/a/b.jpg"
