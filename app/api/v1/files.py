"""File management endpoints: upload, download, delete via object storage."""

from fastapi import APIRouter, Depends, UploadFile

from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.file import FileMetadata, FileUploadResponse

router = APIRouter()


@router.post("/upload", response_model=ApiResponse[FileUploadResponse])
async def upload_file(
    file: UploadFile,
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[FileUploadResponse]:
    """Upload a file to object storage."""
    ...


@router.get("/{file_key:path}/url", response_model=ApiResponse[FileMetadata])
async def get_presigned_url(
    file_key: str,
    expires_in: int = 3600,
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[FileMetadata]:
    """Generate a pre-signed download URL for a file."""
    ...


@router.delete("/{file_key:path}", response_model=ApiResponse[None])
async def delete_file(
    file_key: str,
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[None]:
    """Delete a file from object storage."""
    ...
