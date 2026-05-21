"""Submission service — aligned to real Supabase schema.

`POST /submissions/` writes:
  * one row into `submissions`
  * one row into `submission_images` (more can be added later when the
    mobile app uploads multiple photos).

`GET /submissions/me` returns the rows whose `contributor_email`
matches the signed-in user — the real schema has no `user_id` FK, so
we identify a user's submissions by email.

Admin endpoints (`GET /submissions/`, `GET /submissions/{id}`,
`PATCH /submissions/{id}`) support the reviewer workflow.
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.submission import Submission, SubmissionImage
from app.schemas.submission import SubmissionCreate, SubmissionFilter, SubmissionUpdate
from app.services.storage_service import StorageService


class SubmissionService:
    """Create, list, and read plant observation submissions."""

    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        """Initialize with a database session and optional storage backend."""
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
        photo: UploadFile,
    ) -> Submission:
        """Persist a new submission + its primary image."""
        photo_url = await self._store_photo(photo)
        sub = Submission(
            contributor_name=payload.contributor_name.strip(),
            contributor_email=str(payload.contributor_email).strip().lower(),
            user_description=payload.user_description or None,
            location_state=payload.location_state or None,
            latitude=payload.latitude,
            longitude=payload.longitude,
            status="pending",
        )
        sub.images.append(SubmissionImage(image_url=photo_url, is_primary=True))
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
