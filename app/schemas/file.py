"""File and object-storage schemas."""

from pydantic import BaseModel


class FileUploadResponse(BaseModel):
    """Response after a successful file upload."""

    key: str
    url: str
    size: int


class FileMetadata(BaseModel):
    """Metadata and access URL for a stored file."""

    key: str
    presigned_url: str
    expires_in: int
