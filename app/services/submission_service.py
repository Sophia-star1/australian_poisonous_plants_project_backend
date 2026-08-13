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
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.submission import Submission, SubmissionImage
from app.schemas.submission import SubmissionCreate, SubmissionFilter, SubmissionUpdate
from app.services.storage_service import StorageService, build_object_key


class SubmissionService:
    """Create, list, and read plant observation submissions."""

    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        self.db = db
        self.storage = storage or StorageService()

    async def _store_photo(self, photo: UploadFile, submission_id: UUID) -> str:
        """Upload one photo and return the URL to record against the row.

        Photos are foldered by submission id, which keeps a submission's files
        together for review and makes cleanup a prefix delete. The filename
        itself never becomes part of the key — see `build_object_key`.
        """
        key = build_object_key(f"submissions/{submission_id}", photo.filename)
        stored = await self.storage.upload_file(
            photo, key, bucket=settings.SUPABASE_SUBMISSION_BUCKET
        )
        return stored.url or stored.key

    async def create(
        self,
        payload: SubmissionCreate,
        photos: list[UploadFile],
    ) -> Submission:
        """Persist a new submission. The first photo is marked primary.

        The id is generated up front rather than left to the column default so
        the photos can be stored under it before the row is flushed. Uploads
        happen first on purpose: if storage fails the request aborts and the
        transaction rolls back, so a submission never exists with photos that
        do not.
        """
        submission_id = uuid4()
        sub = Submission(
            id=submission_id,
            contributor_name=payload.contributor_name.strip(),
            contributor_email=str(payload.contributor_email).strip().lower(),
            user_description=payload.user_description or None,
            location_state=payload.location_state or None,
            latitude=payload.latitude,
            longitude=payload.longitude,
            status="pending",
        )

        for i, photo in enumerate(photos):
            url = await self._store_photo(photo, submission_id)
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
