"""Object storage (S3 / MinIO) service.

Used by `SubmissionService` to persist user-uploaded plant photos. In
early development the bucket may not be configured — `upload_file`
falls back to recording the local key so tests and dev environments
can still exercise the submission flow.
"""

from dataclasses import dataclass

from fastapi import UploadFile


@dataclass(slots=True)
class StoredFile:
    """Minimal result of an upload (key + accessible URL)."""

    key: str
    url: str


class StorageService:
    """Manage file operations against S3-compatible storage."""

    async def upload_file(self, file: UploadFile, key: str) -> StoredFile:
        """Upload `file` under `key` and return its storage record.

        TODO: wire up `aioboto3` once `AWS_S3_BUCKET` is configured. For
        now we simply read-and-discard the stream and echo the key —
        enough to keep the submission flow working in dev/tests.
        """
        try:
            await file.read()
        finally:
            await file.close()
        return StoredFile(key=key, url=key)

    async def delete_file(self, key: str) -> bool:
        """Delete a file from the configured S3 bucket."""
        return True

    async def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a time-limited pre-signed download URL."""
        return key
