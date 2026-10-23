"""Object storage: Supabase Storage or any S3-compatible service.

Three logical buckets, matching the review workflow:

* ``submission-images`` — where a contributor's photos land on upload and stay
  as an audit trail of what the reviewer actually saw.
* ``plant-images-v2`` — the curated library; a photo is copied here only when a
  submission is approved.
* ``avatars`` — profile pictures.

The backend is chosen by `settings.storage_backend` (STORAGE_BACKEND):

* **Supabase** — each logical bucket is a Supabase bucket. Calls use the
  service-role key, which bypasses row level security and must stay
  server-side; it is never returned to a client. Public URLs are
  ``<SUPABASE_URL>/storage/v1/object/public/<bucket>/<key>``.
* **S3** (AWS S3, Cloudflare R2, MinIO, ...) — one physical bucket,
  AWS_S3_BUCKET, with each logical bucket as a key prefix, so nothing depends
  on bucket names being free (AWS bucket names are global). Public URLs are
  ``<S3_PUBLIC_BASE_URL>/<bucket>/<key>``; making them readable (a bucket
  policy, an R2 public domain, a CDN) is the operator's job.

Either way copies are made server-side rather than by downloading and
re-uploading, so promoting a submission costs one request per photo regardless
of file size.

When neither is configured the service degrades to a no-op backend so tests
and a bare local checkout still work — but it says so loudly at startup and on
every write. The previous placeholder did the same thing silently, which is
why every photo uploaded so far was discarded without anyone noticing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import aioboto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile

from app.config import settings
from app.core.logging import get_logger
from app.services.photo_metadata import MalformedImageError, strip_metadata

logger = get_logger(__name__)

#: Fallback when a filename carries no usable extension.
_DEFAULT_SUFFIX = ".bin"

#: Extensions we are willing to put in a storage key.
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

#: What an S3 call can raise below a `StorageError`.
_S3_ERRORS = (BotoCoreError, ClientError)


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


def _s3_error_code(exc: Exception) -> str:
    """A short, loggable name for an S3 failure, e.g. "NoSuchBucket"."""
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


class StorageService:
    """Upload, copy and delete objects in Supabase Storage or S3."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.backend = settings.storage_backend
        self.enabled = self.backend != "none"
        self.base_url = settings.SUPABASE_URL.rstrip("/")
        self._s3_public = settings.S3_PUBLIC_BASE_URL.rstrip("/")
        self._timeout = timeout
        if not self.enabled:
            logger.warning(
                "storage_backend_disabled",
                detail=(
                    "No storage is configured (Supabase: SUPABASE_URL and "
                    "SUPABASE_SERVICE_ROLE_KEY; S3: AWS_S3_BUCKET and "
                    "S3_PUBLIC_BASE_URL); uploads are discarded."
                ),
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        key = settings.SUPABASE_SERVICE_ROLE_KEY
        headers = {"Authorization": f"Bearer {key}", "apikey": key}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def public_root(self) -> str:
        """Everything before ``/<bucket>/<key>`` in this backend's public URLs."""
        if self.backend == "s3":
            return self._s3_public
        return f"{self.base_url}/storage/v1/object/public" if self.base_url else ""

    def public_url(self, bucket: str, key: str) -> str:
        """Public URL for an object in a public bucket.

        For Supabase this matches the format already stored in
        ``plant_images_v2.image_url``, so both libraries are addressed the
        same way.
        """
        return f"{self.public_root()}/{bucket}/{key}"

    @asynccontextmanager
    async def _s3(self) -> AsyncIterator[Any]:
        """An S3 client for the configured service.

        Path-style addressing whenever an endpoint is given: MinIO needs it,
        and R2 and most other S3-compatible services accept it.
        """
        endpoint = settings.AWS_S3_ENDPOINT_URL or None
        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
            region_name=settings.AWS_REGION or None,
            config=BotoConfig(
                s3={"addressing_style": "path"} if endpoint else {},
                connect_timeout=self._timeout,
                read_timeout=self._timeout,
                retries={"max_attempts": 3},
            ),
        ) as client:
            yield client

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
                raise StorageError(f"File exceeds the {limit // (1024 * 1024)} MB upload limit.")
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

    async def ensure_public_bucket(self, bucket: str) -> str:
        """Make sure `bucket` can take uploads; returns what it found or did.

        Supabase: creates `bucket` as a public bucket unless it exists, and
        returns `"exists"`, `"created"` or `"exists-private"`. A private bucket
        is left private, with a warning: making one public exposes whatever
        is in it, which is a decision for a person, not something a deploy
        should do on its own.

        S3: logical buckets are key prefixes, so only the one physical bucket
        is checked; `"exists"`, or `StorageError` if it is missing.

        `"skipped"` when no storage is configured.

        Raises:
            StorageError: storage could not be reached, or refused.
        """
        if not self.enabled:
            return "skipped"
        if self.backend == "s3":
            return await self._s3_check_bucket()

        url = f"{self.base_url}/storage/v1/bucket/{bucket}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                found = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise StorageError(f"Storage request failed: {type(exc).__name__}.") from exc

        if found.status_code == 200:
            if found.json().get("public"):
                return "exists"
            logger.warning(
                "storage_bucket_private",
                bucket=bucket,
                detail="Left private; its public URLs will not load until it is made public.",
            )
            return "exists-private"

        # Supabase answers a missing bucket with 400 and "Bucket not found" in
        # the body rather than a plain 404.
        if found.status_code not in (400, 404) or "not found" not in found.text.lower():
            raise StorageError(
                f"Could not check bucket '{bucket}': HTTP {found.status_code} {found.text[:200]}"
            )

        created = await self._post(
            f"{self.base_url}/storage/v1/bucket",
            self._headers("application/json"),
            json={"id": bucket, "name": bucket, "public": True},
        )
        if created.status_code >= 400:
            raise StorageError(
                f"Could not create bucket '{bucket}': HTTP {created.status_code} "
                f"{created.text[:200]}"
            )
        logger.info("storage_bucket_created", bucket=bucket, public=True)
        return "created"

    async def _s3_check_bucket(self) -> str:
        """The one S3 bucket must exist; logical buckets are only prefixes.

        Nothing is created: whether and how the bucket is publicly readable
        (a bucket policy, a public domain, a CDN) is the operator's decision,
        and a deploy should fail on a missing bucket rather than invent one.
        """
        try:
            async with self._s3() as s3:
                await s3.head_bucket(Bucket=settings.AWS_S3_BUCKET)
        except _S3_ERRORS as exc:
            raise StorageError(
                f"S3 bucket '{settings.AWS_S3_BUCKET}' is not reachable: {_s3_error_code(exc)}."
            ) from exc
        return "exists"

    async def upload_bytes(
        self, data: bytes, key: str, bucket: str, content_type: str, *, upsert: bool = False
    ) -> StoredFile:
        """Store `data` at `key` inside `bucket`.

        Supabase refuses to overwrite an existing key unless `upsert` is set;
        S3 always overwrites. Uploads from the app use fresh random keys, so
        only a re-runnable job such as the image migration needs `upsert`.
        """
        if not self.enabled:
            logger.warning("storage_upload_skipped", bucket=bucket, key=key)
            return StoredFile(key=key, url=key)

        if self.backend == "s3":
            try:
                async with self._s3() as s3:
                    await s3.put_object(
                        Bucket=settings.AWS_S3_BUCKET,
                        Key=f"{bucket}/{key}",
                        Body=data,
                        ContentType=content_type,
                    )
            except _S3_ERRORS as exc:
                code = _s3_error_code(exc)
                logger.error("storage_upload_failed", bucket=bucket, key=key, status=code)
                raise StorageError(f"Upload failed: {code}.") from exc
        else:
            url = f"{self.base_url}/storage/v1/object/{bucket}/{key}"
            headers = self._headers(content_type)
            if upsert:
                headers["x-upsert"] = "true"
            response = await self._post(url, headers, content=data)
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
        """Read a contributed photo and store it at `key`, minus its metadata.

        The photo's GPS position, time and device details are removed before
        it is stored (see `photo_metadata`): it is served from a public URL,
        and joins the public library if approved. The pixels are untouched.

        Raises:
            StorageError: too large, empty, or a JPEG/PNG/WebP whose structure
                is broken; or the storage backend failed.
        """
        target = bucket or settings.SUPABASE_SUBMISSION_BUCKET
        try:
            data = strip_metadata(await self.read_upload(file))
        except MalformedImageError as exc:
            raise StorageError("The photo is not a readable image.") from exc
        return await self.upload_bytes(
            data, key, target, file.content_type or "application/octet-stream"
        )

    async def copy_object(
        self, source_key: str, destination_key: str, source_bucket: str, destination_bucket: str
    ) -> StoredFile:
        """Copy an object between buckets without moving bytes through us."""
        if not self.enabled:
            logger.warning("storage_copy_skipped", source=source_key, destination=destination_key)
            return StoredFile(key=destination_key, url=destination_key)

        source = f"{source_bucket}/{source_key}"
        destination = f"{destination_bucket}/{destination_key}"
        if self.backend == "s3":
            try:
                async with self._s3() as s3:
                    await s3.copy_object(
                        Bucket=settings.AWS_S3_BUCKET,
                        Key=destination,
                        CopySource={"Bucket": settings.AWS_S3_BUCKET, "Key": source},
                    )
            except _S3_ERRORS as exc:
                code = _s3_error_code(exc)
                logger.error(
                    "storage_copy_failed", source=source, destination=destination, status=code
                )
                raise StorageError(f"Copy failed: {code}.") from exc
        else:
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
                    source=source,
                    destination=destination,
                    status=response.status_code,
                    body=response.text[:200],
                )
                raise StorageError(f"Copy failed with HTTP {response.status_code}.")

        logger.info("storage_copied", source=source, destination=destination)
        return StoredFile(
            key=destination_key, url=self.public_url(destination_bucket, destination_key)
        )

    async def download_bytes(self, key: str, bucket: str) -> tuple[bytes, str]:
        """The object at `key` in `bucket`, and its content type.

        S3 reads through the API rather than the public URL: a server cannot
        always reach its own public address (no NAT hairpinning, a CDN that
        blocks it), and the API needs no public access at all. Supabase reads
        the public URL, the buckets being public.

        Raises:
            StorageError: storage is not configured, or the read failed.
        """
        if not self.enabled:
            raise StorageError("No storage is configured.")
        if self.backend == "s3":
            try:
                async with self._s3() as s3:
                    found = await s3.get_object(
                        Bucket=settings.AWS_S3_BUCKET, Key=f"{bucket}/{key}"
                    )
                    async with found["Body"] as body:
                        data = await body.read()
            except _S3_ERRORS as exc:
                raise StorageError(f"Download failed: {_s3_error_code(exc)}.") from exc
            return data, str(found.get("ContentType") or "application/octet-stream")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self.public_url(bucket, key))
        except httpx.HTTPError as exc:
            raise StorageError(f"Download failed: {type(exc).__name__}.") from exc
        if response.status_code != 200:
            raise StorageError(f"Download failed with HTTP {response.status_code}.")
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def delete_file(self, key: str, bucket: str | None = None) -> bool:
        """Remove an object. Returns False rather than raising on failure.

        Deletion is always cleanup after the database has already changed;
        raising here would fail a request whose real work succeeded.
        """
        target = bucket or settings.SUPABASE_SUBMISSION_BUCKET
        if not self.enabled:
            logger.warning("storage_delete_skipped", bucket=target, key=key)
            return False

        if self.backend == "s3":
            try:
                async with self._s3() as s3:
                    await s3.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=f"{target}/{key}")
            except _S3_ERRORS as exc:
                logger.error(
                    "storage_delete_failed", bucket=target, key=key, status=_s3_error_code(exc)
                )
                return False
            return True

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
        root = self.public_root()
        if not root:
            return None
        prefix = f"{root}/{bucket}/"
        if not url.startswith(prefix) or len(url) == len(prefix):
            return None
        return url[len(prefix) :]

    def key_from_url(self, url: str) -> str:
        """Recover the storage key from a public URL.

        Needed when promoting an approved submission: the database holds the
        URL, the copy API wants the key. Understands this backend's public
        URLs as well as Supabase's, the format every row written so far uses.
        """
        marker = "/storage/v1/object/public/"
        s3_root = f"{self._s3_public}/" if self.backend == "s3" and self._s3_public else ""
        if s3_root and url.startswith(s3_root):
            tail = url[len(s3_root) :]
        elif marker in url:
            tail = url.split(marker, 1)[1]
        else:
            return url.lstrip("/")
        # tail is "<bucket>/<key...>"; drop the bucket segment.
        _, _, key = tail.partition("/")
        return key
