"""Copy every photo the database points at into the configured storage.

For a new deployment whose database was imported from the existing one: the
image URLs it holds still point at the original Supabase project, which the
new owner does not control and which may go away. This downloads each of
those photos, stores it in whatever storage this deployment is configured
for (`STORAGE_BACKEND`), and rewrites the URL in the database:

    python -m app.db.migrate_images --dry-run   # report only
    python -m app.db.migrate_images

Run it once the new storage is configured and while the old URLs still load.

Every photo it copies loses its metadata on the way (GPS position, time,
device; see `app.services.photo_metadata`). Photos stored before that was
done can be cleaned where they are, keeping their URLs:

    python -m app.db.migrate_images --strip-in-place

Only Supabase Storage public URLs (``.../storage/v1/object/public/<bucket>/
<key>``) are moved. Anything else — the curated thumbnails, which live on an
external image service, or URLs already in the new storage — is left as it
is. The key is kept, and each column goes to the logical bucket the code
expects it in, so later deletes and promotions find the files.

Safe to re-run: rows already pointing at the new storage are skipped, uploads
overwrite, and each row is updated in its own transaction, and only if it
still holds the URL that was copied. A failure is reported and the rest carry
on; the exit status is non-zero if anything failed.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.db.session import get_engine
from app.services.photo_metadata import MalformedImageError, strip_metadata
from app.services.storage_service import StorageError, StorageService

logger = get_logger(__name__)

#: What every Supabase Storage public URL contains, whichever project.
SUPABASE_PUBLIC_MARKER = "/storage/v1/object/public/"


@dataclass(frozen=True, slots=True)
class ImageColumn:
    """A column of public image URLs and the logical bucket its files belong in."""

    table: str
    column: str
    bucket: str


def image_columns() -> tuple[ImageColumn, ...]:
    """Every column that stores a photo URL. A function so tests see their settings."""
    return (
        ImageColumn("plant_images_v2", "image_url", settings.SUPABASE_PLANT_BUCKET),
        ImageColumn("plant_images_v2", "thumbnail_url", settings.SUPABASE_PLANT_BUCKET),
        ImageColumn("submission_images", "image_url", settings.SUPABASE_SUBMISSION_BUCKET),
        ImageColumn("app_users", "avatar_url", settings.SUPABASE_AVATAR_BUCKET),
    )


def supabase_key(url: str) -> str | None:
    """The object key of a Supabase Storage public URL, or None for any other URL."""
    if SUPABASE_PUBLIC_MARKER not in url:
        return None
    tail = url.split(SUPABASE_PUBLIC_MARKER, 1)[1].split("?", 1)[0]
    _, _, key = tail.partition("/")
    return key or None


@dataclass(slots=True)
class Report:
    copied: int = 0
    already_moved: int = 0
    #: Photos already in this storage whose metadata was removed in place.
    stripped: int = 0
    failed: list[str] = field(default_factory=list)


async def migrate_images(
    conn: Any,
    storage: StorageService,
    http: httpx.AsyncClient,
    *,
    dry_run: bool = False,
    strip_in_place: bool = False,
) -> Report:
    """Move the photos behind every Supabase URL in `image_columns()`.

    With `strip_in_place`, photos already in the configured storage are also
    downloaded and, if they carry metadata, stored again under the same key
    without it; their URLs do not change.
    """
    root = storage.public_root()
    report = Report()
    #: Source URL -> new URL, so a photo referenced twice is copied once.
    done: dict[str, str] = {}
    checked: set[str] = set()

    for col in image_columns():
        rows = await conn.fetch(
            f"SELECT id, {col.column} AS url FROM {col.table} "
            f"WHERE {col.column} IS NOT NULL ORDER BY id"
        )
        for row in rows:
            old: str = row["url"]
            where = f"{col.table}.{col.column} id={row['id']}"
            if old.startswith(f"{root}/"):
                report.already_moved += 1
                in_bucket = storage.key_in_bucket(old, col.bucket)
                if strip_in_place and in_bucket and old not in checked:
                    checked.add(old)
                    try:
                        if await _restrip(storage, old, col.bucket, in_bucket, dry_run):
                            logger.info("image_metadata_stripped", row=where, dry_run=dry_run)
                            report.stripped += 1
                    except (httpx.HTTPError, StorageError) as exc:
                        logger.error("image_strip_failed", row=where, error=str(exc))
                        report.failed.append(where)
                continue
            key = supabase_key(old)
            if key is None:
                continue
            if dry_run:
                logger.info("image_would_move", row=where, source=old, bucket=col.bucket, key=key)
                report.copied += 1
                continue
            try:
                new = done.get(old) or await _copy(storage, http, old, col.bucket, key)
            except (httpx.HTTPError, StorageError) as exc:
                logger.error("image_move_failed", row=where, source=old, error=str(exc))
                report.failed.append(where)
                continue
            done[old] = new
            async with conn.transaction():
                await conn.execute(
                    f"UPDATE {col.table} SET {col.column} = $1 WHERE id = $2 AND {col.column} = $3",
                    new,
                    row["id"],
                    old,
                )
            logger.info("image_moved", row=where, url=new)
            report.copied += 1
    return report


async def _download(http: httpx.AsyncClient, url: str, key: str) -> tuple[bytes, str]:
    """The photo at `url` and its content type."""
    response = await http.get(url)
    if response.status_code != 200:
        raise StorageError(f"Download failed with HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if not content_type.startswith("image/"):
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return response.content, content_type


def _without_metadata(data: bytes, url: str) -> bytes:
    """`data` minus its metadata; unchanged, with a warning, if it cannot be parsed.

    A photo that is already public is still worth moving even when its
    metadata cannot be removed, so a parse failure does not stop the copy.
    """
    try:
        return strip_metadata(data)
    except MalformedImageError:
        logger.warning("image_metadata_not_stripped", source=url)
        return data


async def _copy(
    storage: StorageService, http: httpx.AsyncClient, url: str, bucket: str, key: str
) -> str:
    data, content_type = await _download(http, url, key)
    stored = await storage.upload_bytes(
        _without_metadata(data, url), key, bucket, content_type, upsert=True
    )
    return stored.url


async def _restrip(storage: StorageService, url: str, bucket: str, key: str, dry_run: bool) -> bool:
    """Store the photo at `url` again without its metadata; False if it had none.

    Read through the storage API rather than `url`: this server may not be
    able to reach its own public address.
    """
    data, content_type = await storage.download_bytes(key, bucket)
    clean = _without_metadata(data, url)
    if clean is data:
        return False
    if not dry_run:
        await storage.upload_bytes(clean, key, bucket, content_type, upsert=True)
    return True


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.migrate_images", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would move; change nothing"
    )
    parser.add_argument(
        "--strip-in-place",
        action="store_true",
        help="also remove metadata from photos already in this storage (URLs unchanged)",
    )
    args = parser.parse_args(argv)

    storage = StorageService()
    if not storage.enabled:
        logger.error(
            "image_migration_refused",
            detail="No storage is configured; set STORAGE_BACKEND and its settings first.",
        )
        return 2

    engine = get_engine()
    try:
        async with (
            engine.connect() as connection,
            httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http,
        ):
            raw = await connection.get_raw_connection()
            report = await migrate_images(
                raw.driver_connection,
                storage,
                http,
                dry_run=args.dry_run,
                strip_in_place=args.strip_in_place,
            )
    finally:
        await engine.dispose()

    logger.info(
        "image_migration_done",
        backend=storage.backend,
        dry_run=args.dry_run,
        moved=report.copied,
        already_moved=report.already_moved,
        metadata_stripped=report.stripped,
        failed=len(report.failed),
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
