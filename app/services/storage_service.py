"""Object storage (S3 / MinIO) service."""

from fastapi import UploadFile

from app.schemas.file import FileMetadata


class StorageService:
    """Manage file operations against S3-compatible storage."""

    async def upload_file(self, file: UploadFile, key: str) -> FileMetadata:
        """Upload a file to the configured S3 bucket."""
        ...

    async def delete_file(self, key: str) -> bool:
        """Delete a file from the configured S3 bucket."""
        ...

    async def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a time-limited pre-signed download URL."""
        ...
