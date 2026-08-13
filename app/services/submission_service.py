"""Submission service.

`POST /submissions/` writes one row into `submissions` and one row
per uploaded photo into `submission_images` (the first photo is
marked `is_primary=True`).

`GET /submissions/me` returns the rows whose `contributor_email`
matches the signed-in user — the schema has no `user_id` FK, so
we identify a user's submissions by email.

Admin endpoints (`GET /submissions/`, `GET /submissions/{id}`,
`PATCH /submissions/{id}`) support the reviewer workflow.
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.submission import Submission, SubmissionImage
from app.schemas.submission import (
    PENDING_STATUS,
    SubmissionContentUpdate,
    SubmissionCreate,
    SubmissionFilter,
    SubmissionUpdate,
)
from app.services.storage_service import StorageService


class SubmissionService:
    """Create, list, and read plant observation submissions."""

    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        self.db = db
        self.storage = storage or StorageService()

    async def _store_photo(self, photo: UploadFile) -> str:
        """Upload the photo and return its URL."""
        key = f"submissions/{photo.filename or 'upload.bin'}"
        stored = await self.storage.upload_file(photo, key)
        return stored.url or stored.key

    async def create(
        self,
        payload: SubmissionCreate,
        photos: list[UploadFile],
    ) -> Submission:
        """Persist a new submission. The first photo is marked primary."""
        sub = Submission(
            contributor_name=payload.contributor_name.strip(),
            contributor_email=str(payload.contributor_email).strip().lower(),
            user_description=payload.user_description or None,
            location_state=payload.location_state or None,
            latitude=payload.latitude,
            longitude=payload.longitude,
            status="pending",
        )

        for i, photo in enumerate(photos):
            url = await self._store_photo(photo)
            sub.images.append(SubmissionImage(image_url=url, is_primary=(i == 0)))

        self.db.add(sub)
        await self.db.flush()
        await self.db.refresh(sub, attribute_names=["images"])
        return sub

    async def list_for_email(
        self, email: str, page: int = 1, page_size: int = 20
    ) -> tuple[Sequence[Submission], int]:
        """Return one page of submissions belonging to `email`."""
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size
        norm = email.lower()

        total = await self.db.scalar(
            select(func.count(Submission.id)).where(Submission.contributor_email == norm)
        )

        stmt = (
            select(Submission)
            .options(selectinload(Submission.images))
            .where(Submission.contributor_email == norm)
            .order_by(desc(Submission.submitted_at))
            .offset(offset)
            .limit(page_size)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all(), int(total or 0)

    async def get_by_id(self, submission_id: UUID) -> Submission | None:
        """Return a single submission with its images loaded."""
        stmt = (
            select(Submission)
            .options(selectinload(Submission.images))
            .where(Submission.id == submission_id)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def list_all(self, filters: SubmissionFilter) -> tuple[Sequence[Submission], int]:
        """Admin list with optional status / name / email filters."""
        conditions = []
        if filters.status:
            conditions.append(Submission.status == filters.status)
        if filters.contributor_name:
            conditions.append(
                Submission.contributor_name.ilike(f"%{filters.contributor_name}%")
            )
        if filters.contributor_email:
            conditions.append(
                Submission.contributor_email.ilike(f"%{filters.contributor_email}%")
            )

        total = await self.db.scalar(
            select(func.count(Submission.id)).where(*conditions)
        )

        offset = (filters.page - 1) * filters.page_size
        stmt = (
            select(Submission)
            .options(selectinload(Submission.images))
            .where(*conditions)
            .order_by(desc(Submission.submitted_at))
            .offset(offset)
            .limit(filters.page_size)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all(), int(total or 0)

    async def update_content(
        self, submission_id: UUID, payload: SubmissionContentUpdate
    ) -> Submission | None:
        """Apply a contributor's edit to a still-pending submission.

        The write is a single conditional UPDATE guarded on
        `status = 'pending'`, so a reviewer approving the submission at the
        same moment cannot be overwritten: whichever statement commits first
        wins and the other sees zero rows. Returning `None` therefore means
        "someone reviewed it while you were editing", not "not found" — the
        caller has already established that the row exists.

        Only fields present in the request body are applied, so omitting a
        field leaves it untouched rather than nulling it.
        """
        changes = payload.model_dump(exclude_unset=True)
        if "contributor_name" in changes and changes["contributor_name"] is not None:
            changes["contributor_name"] = str(changes["contributor_name"]).strip()

        if not changes:
            return await self.get_by_id(submission_id)

        stmt = (
            update(Submission)
            .where(Submission.id == submission_id, Submission.status == PENDING_STATUS)
            .values(**changes)
            .returning(Submission.id)
        )
        result = await self.db.execute(stmt)
        if result.first() is None:
            return None

        await self.db.flush()
        self.db.expire_all()
        return await self.get_by_id(submission_id)

    async def add_photos(
        self, submission_id: UUID, photos: list[UploadFile]
    ) -> Submission | None:
        """Attach more photos to a pending submission.

        Guarded the same way as `update_content`: the status is re-checked in
        the database rather than trusted from the row we read earlier.
        """
        guard = await self.db.execute(
            select(Submission.id).where(
                Submission.id == submission_id, Submission.status == PENDING_STATUS
            )
        )
        if guard.first() is None:
            return None

        existing = await self.db.scalar(
            select(func.count(SubmissionImage.id)).where(
                SubmissionImage.submission_id == submission_id
            )
        )
        has_primary = bool(existing)

        for photo in photos:
            url = await self._store_photo(photo)
            self.db.add(
                SubmissionImage(
                    submission_id=submission_id,
                    image_url=url,
                    is_primary=not has_primary,
                )
            )
            has_primary = True

        await self.db.flush()
        self.db.expire_all()
        return await self.get_by_id(submission_id)

    async def delete_photo(
        self, submission_id: UUID, photo_id: UUID
    ) -> Submission | None:
        """Remove one photo from a pending submission.

        Promotes another image to primary if the deleted one held that flag,
        so a submission never ends up without a lead photo.
        """
        guard = await self.db.execute(
            select(Submission.id).where(
                Submission.id == submission_id, Submission.status == PENDING_STATUS
            )
        )
        if guard.first() is None:
            return None

        image = await self.db.scalar(
            select(SubmissionImage).where(
                SubmissionImage.id == photo_id,
                SubmissionImage.submission_id == submission_id,
            )
        )
        if image is None:
            return None

        was_primary = image.is_primary
        await self.storage.delete_file(image.image_url)
        await self.db.delete(image)
        await self.db.flush()

        if was_primary:
            replacement = await self.db.scalar(
                select(SubmissionImage)
                .where(SubmissionImage.submission_id == submission_id)
                .order_by(SubmissionImage.id.asc())
                .limit(1)
            )
            if replacement is not None:
                replacement.is_primary = True
                await self.db.flush()

        self.db.expire_all()
        return await self.get_by_id(submission_id)

    async def count_photos(self, submission_id: UUID) -> int:
        """Number of images currently attached to `submission_id`."""
        total = await self.db.scalar(
            select(func.count(SubmissionImage.id)).where(
                SubmissionImage.submission_id == submission_id
            )
        )
        return int(total or 0)

    async def update_status(
        self, submission_id: UUID, payload: SubmissionUpdate, reviewed_by: str
    ) -> Submission | None:
        """Approve or reject a submission (admin only)."""
        submission = await self.get_by_id(submission_id)
        if submission is None:
            return None
        submission.status = payload.status
        submission.reviewer_notes = payload.reviewer_notes
        submission.reviewed_by = reviewed_by
        submission.reviewed_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(submission, attribute_names=["images"])
        return submission
