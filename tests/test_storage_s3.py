"""The S3-compatible storage backend (AWS S3, Cloudflare R2, MinIO, ...).

A deployment that does not use Supabase stores photos in one S3 bucket, with
the three logical buckets as key prefixes. These tests replace the S3 client
with a recorder; the calls it receives are the contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import settings
from app.services import storage_service
from app.services.storage_service import StorageError, StorageService

PUBLIC = "https://media.example.org"
BUCKET = "apl-media"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class FakeS3:
    """Records each S3 call; `fail` maps an operation name to the error it raises."""

    def __init__(self, fail: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail or {}

    def __getattr__(self, operation: str) -> Any:
        async def _call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((operation, kwargs))
            if operation in self.fail:
                raise self.fail[operation]
            return {}

        return _call


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Any:
    """S3 configured and Supabase not; returns a factory for the fake client."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "auto")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", PUBLIC)

    def _apply(**fail: Exception) -> FakeS3:
        fake = FakeS3(fail)

        @asynccontextmanager
        async def _client(self: Any) -> AsyncIterator[FakeS3]:
            yield fake

        monkeypatch.setattr(StorageService, "_s3", _client)
        return fake

    return _apply


# ── choosing the backend ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("choice", "supabase", "s3", "resolved"),
    [
        ("auto", True, True, "supabase"),
        ("auto", False, True, "s3"),
        ("auto", False, False, "none"),
        ("s3", True, True, "s3"),
        ("supabase", False, True, "none"),
        ("s3", True, False, "none"),
    ],
)
def test_the_backend_is_resolved_from_the_settings(
    monkeypatch: pytest.MonkeyPatch, choice: str, supabase: bool, s3: bool, resolved: str
) -> None:
    """An explicit choice with its settings missing is "none", never the other one."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", choice)
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://proj.supabase.co" if supabase else "")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "key" if supabase else "")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", BUCKET if s3 else "")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", PUBLIC if s3 else "")
    assert settings.storage_backend == resolved
    assert StorageService().enabled is (resolved != "none")


# ── operations ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_upload_goes_under_its_logical_bucket(s3: Any) -> None:
    fake = s3()
    stored = await StorageService().upload_bytes(
        b"jpeg", "submissions/1/a.jpg", "submission-images", "image/jpeg"
    )
    assert fake.calls == [
        (
            "put_object",
            {
                "Bucket": BUCKET,
                "Key": "submission-images/submissions/1/a.jpg",
                "Body": b"jpeg",
                "ContentType": "image/jpeg",
            },
        )
    ]
    assert stored.url == f"{PUBLIC}/submission-images/submissions/1/a.jpg"
    assert stored.key == "submissions/1/a.jpg"


@pytest.mark.asyncio
async def test_a_copy_is_server_side_between_prefixes(s3: Any) -> None:
    fake = s3()
    stored = await StorageService().copy_object(
        "submissions/1/a.jpg", "plants/7/b.jpg", "submission-images", "plant-images-v2"
    )
    assert fake.calls == [
        (
            "copy_object",
            {
                "Bucket": BUCKET,
                "Key": "plant-images-v2/plants/7/b.jpg",
                "CopySource": {"Bucket": BUCKET, "Key": "submission-images/submissions/1/a.jpg"},
            },
        )
    ]
    assert stored.url == f"{PUBLIC}/plant-images-v2/plants/7/b.jpg"


@pytest.mark.asyncio
async def test_a_delete_removes_the_prefixed_key(s3: Any) -> None:
    fake = s3()
    assert await StorageService().delete_file("users/7/x.jpg", bucket="avatars") is True
    assert fake.calls == [("delete_object", {"Bucket": BUCKET, "Key": "avatars/users/7/x.jpg"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["put_object", "copy_object"])
async def test_a_failed_write_raises_a_storage_error_naming_the_cause(
    s3: Any, operation: str
) -> None:
    s3(**{operation: _client_error("AccessDenied")})
    service = StorageService()
    with pytest.raises(StorageError, match="AccessDenied"):
        if operation == "put_object":
            await service.upload_bytes(b"x", "k.jpg", "avatars", "image/jpeg")
        else:
            await service.copy_object("a.jpg", "b.jpg", "submission-images", "plant-images-v2")


@pytest.mark.asyncio
async def test_a_failed_delete_reports_false_rather_than_raising(s3: Any) -> None:
    """Deletion is cleanup after the database changed; it must not fail the request."""
    s3(delete_object=_client_error("InternalError"))
    assert await StorageService().delete_file("k.jpg", bucket="avatars") is False


@pytest.mark.asyncio
async def test_the_bucket_check_heads_the_one_physical_bucket(s3: Any) -> None:
    fake = s3()
    assert await StorageService().ensure_public_bucket("avatars") == "exists"
    assert fake.calls == [("head_bucket", {"Bucket": BUCKET})]


@pytest.mark.asyncio
async def test_a_missing_bucket_fails_the_check(s3: Any) -> None:
    """A deploy should stop on a missing bucket rather than create one."""
    s3(head_bucket=_client_error("404"))
    with pytest.raises(StorageError, match=f"'{BUCKET}' is not reachable: 404"):
        await StorageService().ensure_public_bucket("avatars")


# ── URLs and keys ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "key"),
    [
        (f"{PUBLIC}/avatars/users/7/x.jpg", "users/7/x.jpg"),
        (f"{PUBLIC}/plant-images-v2/users/7/x.jpg", None),
        ("https://other.example.org/avatars/users/7/x.jpg", None),
        (f"{PUBLIC}/avatars/", None),
    ],
)
def test_key_in_bucket_answers_only_for_our_bucket(s3: Any, url: str, key: str | None) -> None:
    assert StorageService().key_in_bucket(url, "avatars") == key


def test_key_from_url_reads_s3_and_legacy_supabase_urls(s3: Any) -> None:
    """Rows written before a move to S3 still hold Supabase URLs."""
    service = StorageService()
    assert service.key_from_url(f"{PUBLIC}/submission-images/submissions/1/a.jpg") == (
        "submissions/1/a.jpg"
    )
    legacy = (
        "https://old.supabase.co/storage/v1/object/public/submission-images/submissions/1/a.jpg"
    )
    assert service.key_from_url(legacy) == "submissions/1/a.jpg"


def test_a_trailing_slash_on_the_public_url_does_not_double_up(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", f"{PUBLIC}/")
    assert StorageService().public_url("avatars", "a.jpg") == f"{PUBLIC}/avatars/a.jpg"


# ── the client ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "style"),
    [("http://minio:9000", {"addressing_style": "path"}), ("", {})],
    ids=["custom endpoint", "aws"],
)
async def test_the_client_targets_the_configured_service(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, style: dict
) -> None:
    """Path-style addressing for custom endpoints: MinIO needs it."""
    for name, value in {
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "STORAGE_BACKEND": "s3",
        "AWS_S3_BUCKET": BUCKET,
        "S3_PUBLIC_BASE_URL": PUBLIC,
        "AWS_S3_ENDPOINT_URL": endpoint,
        "AWS_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": "AKIA_TEST",
        "AWS_SECRET_ACCESS_KEY": "secret",
    }.items():
        monkeypatch.setattr(settings, name, value)
    seen: dict[str, Any] = {}

    class _Session:
        def client(self, service: str, **kwargs: Any) -> Any:
            seen.update(service=service, **kwargs)

            @asynccontextmanager
            async def _ctx() -> AsyncIterator[FakeS3]:
                yield FakeS3()

            return _ctx()

    monkeypatch.setattr(storage_service.aioboto3, "Session", _Session)

    await StorageService().upload_bytes(b"x", "a.jpg", "avatars", "image/jpeg")

    assert seen["service"] == "s3"
    assert seen["endpoint_url"] == (endpoint or None)
    assert seen["region_name"] == "ap-southeast-2"
    assert seen["aws_access_key_id"] == "AKIA_TEST"
    assert seen["config"].s3 == style
