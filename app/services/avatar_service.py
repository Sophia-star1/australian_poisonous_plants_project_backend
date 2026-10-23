"""Profile pictures: normalise an upload, store it, point the account at it.

Every avatar is stored the same way whatever was uploaded — a 512x512 JPEG
with no metadata — so clients can lay it out at a fixed size, a list of
users loads a few tens of kilobytes each, and nothing about where or with
what the photo was taken leaves the server.

Each upload gets a new random key rather than overwriting the old object, so
the URL changes with the picture and no client or CDN keeps showing a cached
old one. The previous file is deleted once the new URL has been committed.
"""

from __future__ import annotations

import io

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.ml.classifier import ImageTooLargeError, UndecodableImageError, open_within_budget
from app.models.user import User
from app.services.storage_service import StorageService, build_object_key

#: Side of the stored square, in pixels: sharp on a phone's profile screen.
AVATAR_SIZE = 512
AVATAR_JPEG_QUALITY = 85


def normalise_avatar(data: bytes) -> bytes:
    """Any accepted image, as a `AVATAR_SIZE` square JPEG with no metadata.

    * The decode is bounded by `open_within_budget`, as screening's is: a
      small file can declare an enormous image.
    * EXIF orientation is applied first. A phone stores a portrait photo
      sideways plus a rotation flag, and without this the face lies on its
      side.
    * Transparency is flattened onto white; JPEG has no alpha, and a plain
      conversion turns transparent areas black.
    * The centre is cropped to a square and scaled to fit, up or down.
    * The JPEG is written without EXIF or an ICC profile, so a GPS position,
      the camera and the time it was taken are all dropped.

    Raises:
        UndecodableImageError: the bytes are not an image.
        ImageTooLargeError: the pixel dimensions are beyond the decode budget.
    """
    try:
        opened = open_within_budget(data)
        image = ImageOps.exif_transpose(opened) or opened
        image.load()
    except ImageTooLargeError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UndecodableImageError("The file could not be read as an image.") from exc

    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")

    square = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    square.save(out, "JPEG", quality=AVATAR_JPEG_QUALITY, optimize=True)
    return out.getvalue()


class AvatarService:
    """Set and clear a user's avatar. The caller owns the commit."""

    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        self.db = db
        self.storage = storage or StorageService()
        #: The object uploaded by this request, for `discard_upload`.
        self._uploaded_key: str | None = None

    async def replace(self, user: User, upload: UploadFile) -> str | None:
        """Store `upload` as `user`'s avatar; returns the URL it replaces.

        The old file is not touched here: deleting it before the new URL is
        committed would leave the account pointing at nothing if the commit
        failed. Pass the returned URL to `delete_file` after committing.
        """
        data = await self.storage.read_upload(upload)
        # Decoding and resizing are CPU-bound; inline they would stall every
        # other request on the instance.
        jpeg = await run_in_threadpool(normalise_avatar, data)

        key = build_object_key(f"users/{user.id}", "avatar.jpg")
        stored = await self.storage.upload_bytes(
            jpeg, key, bucket=settings.SUPABASE_AVATAR_BUCKET, content_type="image/jpeg"
        )
        self._uploaded_key = stored.key

        previous = user.avatar_url
        user.avatar_url = stored.url or stored.key
        await self.db.flush()
        return previous

    async def remove(self, user: User) -> str | None:
        """Clear `user`'s avatar; returns the URL it had, if any."""
        previous = user.avatar_url
        user.avatar_url = None
        await self.db.flush()
        return previous

    async def delete_file(self, url: str) -> None:
        """Remove a replaced avatar's file, if it is one of ours. Never raises.

        Only a URL into this project's avatar bucket is deleted; anything
        else is left alone rather than guessed at.
        """
        key = self.storage.key_in_bucket(url, settings.SUPABASE_AVATAR_BUCKET)
        if key is not None:
            await self.storage.delete_file(key, bucket=settings.SUPABASE_AVATAR_BUCKET)

    async def discard_upload(self) -> None:
        """Delete what this request uploaded, for when its commit fails."""
        key, self._uploaded_key = self._uploaded_key, None
        if key is not None:
            await self.storage.delete_file(key, bucket=settings.SUPABASE_AVATAR_BUCKET)
