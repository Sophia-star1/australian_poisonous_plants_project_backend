"""Object storage backed by Supabase Storage.

Two buckets, matching the review workflow:

* ``submission-images`` — where a contributor's photos land on upload and stay
  as an audit trail of what the reviewer actually saw.
* ``plant-images`` — the curated library; a photo is copied here only when a
  submission is approved.

Copies are made server-side through Supabase's ``/object/copy`` endpoint rather
than downloading and re-uploading, so promoting a submission costs one request
per photo regardless of file size.

All calls use the service-role key. It bypasses row level security and must
stay server-side; it is never returned to a client.

When credentials are absent the service degrades to a no-op backend so tests
and a bare local checkout still work — but it says so loudly at startup and on
every write. The previous placeholder did the same thing silently, which is
why every photo uploaded so far was discarded without anyone noticing.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import uuid4

import httpx
from fastapi import UploadFile

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Fallback when a filename carries no usable extension.
_DEFAULT_SUFFIX = ".bin"

#: Extensions we are willing to put in a storage key.
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(slots=True)
class StoredFile:
    """Result of an upload or copy: the storage key plus a fetchable URL."""

    key: str
    url: str


class StorageError(RuntimeError):
    """Raised when the storage backend rejects or fails an operation."""


def build_object_key(prefix: str, filename: str | None) -> str:
    """Build a collision-proof storage key.

    The client's filename is never used as the key. Phones name every photo
    ``IMG_0001.jpg``, so two contributors would overwrite each other — the
    existing data already shows four such collisions. A client-supplied path
    is also a traversal risk. Only the extension is carried over, and only
    when it is one we recognise.
    """
    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        suffix = _DEFAULT_SUFFIX
    return f"{prefix.strip('/')}/{uuid4().hex}{suffix}"


class StorageService:
    """Upload, copy and delete objects in Supabase Storage."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.base_url = settings.SUPABASE_URL.rstrip("/")
        self.enabled = settings.supabase_storage_enabled
        self._timeout = timeout
        if not self.enabled:
            logger.warning(
                "storage_backend_disabled",
                detail=(
                    "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are unset; uploads are "
                    "discarded. Set both to persist contributor photos."
                ),
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        key = settings.SUPABASE_SERVICE_ROLE_KEY
        headers = {"Authorization": f"Bearer {key}", "apikey": key}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def public_url(self, bucket: str, key: str) -> str:
        """Public URL for an object in a public bucket.

        Matches the format already stored in ``plant_images.image_url`` so
        both libraries are addressed the same way.
        """
        return f"{self.base_url}/storage/v1/object/public/{bucket}/{key}"

    async def read_upload(self, file: UploadFile, *, rewind: bool = False) -> bytes:
        """Read an upload into memory, refusing anything oversized.

        Reads in chunks and stops at the limit rather than loading the whole
        body first, so an attacker cannot exhaust memory by declaring a small
        Content-Length and sending more.

        `rewind` leaves the handle open and positioned at the start instead of
        closing it, so the same upload can be read again. Screening needs the
        bytes before the upload path does; without this the choice would be
        between holding every photo in memory at once — ten of them, ten MB
        each — and uploading first only to delete again on a rejection.
        """
        limit = settings.MAX_UPLOAD_BYTES
        chunks: list[bytes] = []
        total = 0
        while chunk := await file.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                await file.close()
                raise StorageError(
                    f"File exceeds the {limit // (1024 * 1024)} MB upload limit."
                )
            chunks.append(chunk)
        if rewind:
            await file.seek(0)
        else:
            await file.close()
        if total == 0:
            raise StorageError("File is empty.")
        return b"".join(chunks)

    async def _post(self, url: str, headers: dict[str, str], **body: object) -> httpx.Response:
        """POST to storage, turning transport failures into `StorageError`.

        httpx raises for everything that happens below the HTTP response — a
        DNS failure, a timeout, or a header value it refuses to put on the
        wire. Those propagated untouched and reached the client as a bare 500,
        so a credential with a stray newline was indistinguishable from a bug
        in the endpoint. Callers now have exactly one exception type to handle,
        and the cause is logged rather than swallowed.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.post(url, headers=headers, **body)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            logger.error("storage_request_error", url=url, error=f"{type(exc).__name__}: {exc}")
            raise StorageError(f"Storage request failed: {type(exc).__name__}.") from exc

    # ── operations ───────────────────────────────────────────────────────────

    async def upload_bytes(
        self, data: bytes, key: str, bucket: str, content_type: str
    ) -> StoredFile:
        """Store `data` at `key` inside `bucket`."""
        if not self.enabled:
            logger.warning("storage_upload_skipped", bucket=bucket, key=key)
            return StoredFile(key=key, url=key)

        url = f"{self.base_url}/storage/v1/object/{bucket}/{key}"
        response = await self._post(url, self._headers(content_type), content=data)
        if response.status_code >= 400:
            logger.error(
                "storage_upload_failed",
                bucket=bucket,
                key=key,
                status=response.status_code,
                body=response.text[:200],
            )
            raise StorageError(f"Upload failed with HTTP {response.status_code}.")

        logger.info("storage_uploaded", bucket=bucket, key=key, bytes=len(data))
        return StoredFile(key=key, url=self.public_url(bucket, key))

    async def upload_file(
        self, file: UploadFile, key: str, bucket: str | None = None
    ) -> StoredFile:
        """Read `file` and store it at `key`."""
        target = bucket or settings.SUPABASE_SUBMISSION_BUCKET
        data = await self.read_upload(file)
        return await self.upload_bytes(
            data, key, target, file.content_type or "application/octet-stream"
        )

    async def copy_object(
        self, source_key: str, destination_key: str, source_bucket: str, destination_bucket: str
    ) -> StoredFile:
        """Copy an object between buckets without moving bytes through us."""
        if not self.enabled:
            logger.warning(
                "storage_copy_skipped", source=source_key, destination=destination_key
            )
            return StoredFile(key=destination_key, url=destination_key)

        url = f"{self.base_url}/storage/v1/object/copy"
        payload = {
            "bucketId": source_bucket,
            "sourceKey": source_key,
            "destinationBucket": destination_bucket,
            "destinationKey": destination_key,
        }
        response = await self._post(url, self._headers("application/json"), json=payload)
        if response.status_code >= 400:
            logger.error(
                "storage_copy_failed",
                source=f"{source_bucket}/{source_key}",
                destination=f"{destination_bucket}/{destination_key}",
                status=response.status_code,
                body=response.text[:200],
            )
            raise StorageError(f"Copy failed with HTTP {response.status_code}.")

        logger.info(
            "storage_copied",
            source=f"{source_bucket}/{source_key}",
            destination=f"{destination_bucket}/{destination_key}",
        )
        return StoredFile(
            key=destination_key, url=self.public_url(destination_bucket, destination_key)
        )

    async def delete_file(self, key: str, bucket: str | None = None) -> bool:
        """Remove an object. Returns False rather than raising on failure.

        Deletion is always cleanup after the database has already changed;
        raising here would fail a request whose real work succeeded.
        """
        target = bucket or settings.SUPABASE_SUBMISSION_BUCKET
        if not self.enabled:
            logger.warning("storage_delete_skipped", bucket=target, key=key)
            return False

        url = f"{self.base_url}/storage/v1/object/{target}/{key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.delete(url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.error("storage_delete_error", bucket=target, key=key, error=str(exc))
            return False

        if response.status_code >= 400:
            logger.error(
                "storage_delete_failed",
                bucket=target,
                key=key,
                status=response.status_code,
            )
            return False
        return True

    def key_in_bucket(self, url: str, bucket: str) -> str | None:
        """The key of `url` if it is a public URL into `bucket` of this project.

        `None` for anything else — another bucket, another host, a bare path.
        Unlike `key_from_url`, which drops whatever bucket the URL names, this
        is safe to delete by: it never yields a key in a bucket you did not
        name, or for a file hosted somewhere else.
        """
        if not self.base_url:
            return None
        prefix = f"{self.base_url}/storage/v1/object/public/{bucket}/"
        if not url.startswith(prefix) or len(url) == len(prefix):
            return None
        return url[len(prefix) :]

    def key_from_url(self, url: str) -> str:
        """Recover the storage key from a public URL.

        Needed when promoting an approved submission: the database holds the
        URL, the copy API wants the key.
        """
        marker = "/storage/v1/object/public/"
        if marker not in url:
            return url.lstrip("/")
        tail = url.split(marker, 1)[1]
        # tail is "<bucket>/<key...>"; drop the bucket segment.
        _, _, key = tail.partition("/")
        return key
