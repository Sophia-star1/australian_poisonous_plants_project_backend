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

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.ml.classifier import Decision, Verdict, get_classifier
from app.models.plant import TOXICITY_SPECIES, UNKNOWN, Plant, PlantImage
from app.models.submission import Submission, SubmissionImage
from app.schemas.submission import (
    PENDING_STATUS,
    PlantIdentification,
    SubmissionContentUpdate,
    SubmissionCreate,
    SubmissionFilter,
    SubmissionUpdate,
    content_fingerprint,
)
from app.services.storage_service import StorageService, build_object_key


class AlreadyReviewedError(RuntimeError):
    """Raised when a submission was reviewed by someone else first."""

    def __init__(self, current_status: str) -> None:
        super().__init__(f"Submission has already been {current_status}.")
        self.current_status = current_status


_screening_semaphore: asyncio.Semaphore | None = None


def _screening_slots() -> asyncio.Semaphore:
    """Limit concurrent decoding, built lazily so the setting can be changed.

    Created on first use rather than at import: a module-level semaphore
    freezes `PLANT_SCREENING_CONCURRENCY` at import time, which makes it
    untestable and ignores any value set after the module loads.
    """
    global _screening_semaphore
    if _screening_semaphore is None:
        _screening_semaphore = asyncio.Semaphore(settings.PLANT_SCREENING_CONCURRENCY)
    return _screening_semaphore


def reset_screening_limit() -> None:
    """Drop the cached semaphore. For tests that change the setting."""
    global _screening_semaphore
    _screening_semaphore = None


class ContentChangedError(RuntimeError):
    """The submission changed after the reviewer loaded it."""

    def __init__(self, current_version: str) -> None:
        super().__init__(
            "This submission has been edited since you opened it. Reload it and "
            "check the changes before deciding."
        )
        self.current_version = current_version


def submission_fingerprint(submission: Submission) -> str:
    """The `content_version` for a loaded row.

    Same function the response uses, fed from the ORM instead of the schema,
    so the value a reviewer receives and the value checked against are
    computed one way only.
    """
    return content_fingerprint(
        contributor_name=submission.contributor_name,
        user_description=submission.user_description,
        location_state=submission.location_state,
        latitude=submission.latitude,
        longitude=submission.longitude,
        photos=[(image.id, image.is_primary) for image in submission.images],
    )


def _unknown_defaults() -> dict[str, Any]:
    """Every NOT NULL `plants_v2` column, set to the dataset's "no value".

    `plants_v2` has no nullable columns and records a gap as the string
    `'Unknown'` — as `['Unknown']` for a text array, and as an object of
    `'Unknown'` for the two jsonb columns. `botanical_name` is absent on
    purpose: a reviewer must supply it, and defaulting it to `'Unknown'` would
    publish an unnamed plant rather than fail.

    Built fresh on each call because the nested structures are mutable; a
    module-level constant would be shared between every approved plant.
    """
    return {
        "family_name": UNKNOWN,
        "common_names": [UNKNOWN],
        "origin_status": UNKNOWN,
        "distribution": [UNKNOWN],
        "habitat": UNKNOWN,
        "seasonality_of_toxicity": UNKNOWN,
        "toxic_parts": [UNKNOWN],
        "lethality": UNKNOWN,
        "health_effects": UNKNOWN,
        "toxicity_by_species": {key: UNKNOWN for key in (*TOXICITY_SPECIES, "notes")},
        "toxin_profile": {
            "primary_class": UNKNOWN,
            "secondary_class": UNKNOWN,
            "toxins_implicated": UNKNOWN,
        },
        "morphological_attributes": {
            "height_m": UNKNOWN,
            "growth_form": UNKNOWN,
            "woodiness": UNKNOWN,
            "life_history": UNKNOWN,
            "fruit_colour": UNKNOWN,
            "flowering_time": [UNKNOWN],
        },
    }


class NotAPlantError(ValueError):
    """Raised when screening is confident that no photo shows a plant."""

    def __init__(self, probabilities: list[float]) -> None:
        best = max(probabilities) if probabilities else 0.0
        super().__init__(
            "None of the uploaded photos appear to show a plant "
            f"(highest confidence {best:.0%}). Please upload a photo of the "
            "plant itself — a leaf, flower, fruit, bark or the whole plant."
        )
        self.probabilities = probabilities


@dataclass(frozen=True, slots=True)
class Screening:
    """What screening concluded about a whole submission."""

    is_plant: bool
    per_photo: list[Verdict]

    @property
    def best(self) -> float:
        return max((v.confidence for v in self.per_photo), default=0.0)

    def as_record(self) -> dict[str, Any]:
        """Shape written to `submissions.ai_top_matches`."""
        return {
            "screening": {
                "plant_probability": self.best,
                "photos": [v.as_dict() for v in self.per_photo],
            }
        }


class SubmissionService:
    """Create, list, and read plant observation submissions."""

    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        self.db = db
        self.storage = storage or StorageService()

    async def screen(self, photos: list[UploadFile]) -> Screening | None:
        """Score every photo before a single byte reaches storage.

        Returns `None` when screening is unavailable — no weights, or the
        feature switched off. That is "no opinion", never a rejection: a
        classifier that cannot load must not take the upload path down with
        it.

        The submission is refused only when *every* photo is a confident
        negative. Refusing on any single one would be harsher than the model
        earns: it turns away 7 of the 104 photographs in this project's own
        curated library at its default cutoff, and one stray frame among
        several is far more likely to be an honest mistake than an abuse. A
        photo in the uncertain band still goes through, marked for the
        reviewer rather than bounced back at the contributor.

        Each photo is read, scored, then released, so peak memory is one
        photo rather than all ten. `rewind` leaves the handle ready for the
        upload path to read again.
        """
        classifier = get_classifier()
        if classifier is None:
            return None

        verdicts: list[Verdict] = []
        for photo in photos:
            data = await self.storage.read_upload(photo, rewind=True)
            # The semaphore bounds how much decoding happens at once. One
            # photograph costs about 70 MB while it is being decoded, and
            # Cloud Run puts 80 concurrent requests on an instance by
            # default; without a limit a burst of uploads runs the instance
            # out of memory and it is killed mid-request.
            async with _screening_slots():
                # Inference is CPU-bound and takes ~90 ms. Run inline it would
                # block the event loop, and with it every other request this
                # instance is serving.
                verdicts.append(await run_in_threadpool(classifier.classify, data))

        if all(v.decision is Decision.REJECT for v in verdicts):
            raise NotAPlantError([v.confidence for v in verdicts])

        return Screening(
            is_plant=any(v.decision is Decision.ACCEPT for v in verdicts),
            per_photo=verdicts,
        )

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

        Screening runs before the uploads for the same reason in reverse — a
        refused submission should cost nothing in storage, so the photos are
        scored while they are still only in the request body.
        """
        screening = await self.screen(photos)

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
            # NULL when screening did not run, which is distinct from False.
            ai_is_plant=screening.is_plant if screening else None,
            ai_top_matches=screening.as_record() if screening else None,
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

    async def _promote_photos(self, submission: Submission, plant_id: int) -> list[PlantImage]:
        """Copy a submission's photos into the curated plant library.

        Server-side copies, so the bytes never pass through this process. The
        originals stay in `submission-images` as the record of what the
        reviewer saw — the approval refers to those exact files.
        """
        promoted: list[PlantImage] = []
        for image in sorted(submission.images, key=lambda i: (not i.is_primary, str(i.id))):
            source_key = self.storage.key_from_url(image.image_url)
            destination_key = build_object_key(f"plants/{plant_id}", source_key)
            stored = await self.storage.copy_object(
                source_key=source_key,
                destination_key=destination_key,
                source_bucket=settings.SUPABASE_SUBMISSION_BUCKET,
                destination_bucket=settings.SUPABASE_PLANT_BUCKET,
            )
            promoted.append(
                PlantImage(
                    plant_id=plant_id,
                    image_url=stored.url or stored.key,
                    caption=submission.user_description,
                    is_primary=image.is_primary,
                )
            )
        return promoted

    async def _create_plant(
        self, identification: PlantIdentification, submission: Submission
    ) -> Plant:
        """Create the `plants_v2` row a reviewer approved into existence.

        Two things this has to do beyond copying the reviewer's fields.

        The observed location is folded into `distribution` when the reviewer
        did not supply one: it is the single piece of botanical information a
        submission genuinely carries. In v1 this went into
        `distribution_observed`, which kept it distinguishable from a formal
        listing; v2 has one merged list, so that distinction is lost — a
        contributor's sighting now sits alongside curated distribution data.

        Then every column left blank is filled with the dataset's `'Unknown'`
        convention. Every column in `plants_v2` is NOT NULL, so an omitted
        field is an IntegrityError rather than a NULL, and the reviewer is only
        obliged to supply `botanical_name`. Filling here rather than leaning on
        the server defaults keeps the ORM instance and the row identical: the
        approval response and the notification email both read this object
        back, and a column the database defaulted would be missing from it
        until a refresh.
        """
        fields = identification.model_dump(exclude_unset=True, exclude_none=True)
        if not fields.get("distribution") and submission.location_state:
            fields["distribution"] = [submission.location_state]

        plant = Plant(**{**_unknown_defaults(), **fields})
        self.db.add(plant)
        await self.db.flush()
        return plant
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
            url = await self._store_photo(photo, submission_id)
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
        # The row stores a public URL; the storage API takes a key. Before
        # storage was implemented, delete_file ignored its argument entirely,
        # so passing the URL was harmless — it no longer is.
        await self.storage.delete_file(
            self.storage.key_from_url(image.image_url),
            bucket=settings.SUPABASE_SUBMISSION_BUCKET,
        )
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
    ) -> tuple[Submission, Plant | None] | None:
        """Approve or reject a submission (admin only).

        Approving is a single transaction across three things: the submission
        is marked reviewed, a `plants_v2` row is created from the reviewer's
        identification, and the photos are copied into `plant-images-v2`. If the
        copy fails the whole request fails and nothing is committed, so an
        approved plant never ends up without its photographs.

        The status transition is a guarded UPDATE rather than a read-modify-
        write, so two reviewers acting at once cannot both succeed.
        """
        submission = await self.get_by_id(submission_id)
        if submission is None:
            return None

        # Checked before the status guard so the reviewer hears the useful
        # thing. Both are 409s, but "somebody edited this" is actionable and
        # "somebody else already reviewed it" is not.
        current_version = submission_fingerprint(submission)
        if payload.expected_version != current_version:
            raise ContentChangedError(current_version)

        guard = await self.db.execute(
            update(Submission)
            .where(Submission.id == submission_id, Submission.status == PENDING_STATUS)
            .values(
                status=payload.status,
                reviewer_notes=payload.reviewer_notes,
                reviewed_by=reviewed_by,
                reviewed_at=datetime.now(timezone.utc),
            )
            .returning(Submission.id)
        )
        if guard.first() is None:
            raise AlreadyReviewedError(submission.status)

        plant_id: int | None = None
        if payload.status == "approved" and payload.plant is not None:
            plant = await self._create_plant(payload.plant, submission)
            plant_id = plant.id
            for image in await self._promote_photos(submission, plant_id):
                self.db.add(image)
            await self.db.flush()

        await self.db.flush()
        # expire_all() marks every loaded attribute stale, including those of
        # the Plant just created. Handing that instance back would make the
        # caller's first attribute access a lazy load, which under asyncio
        # raises MissingGreenlet rather than fetching -- the endpoint reads
        # plant.id to build its response, so approving always returned 500.
        # Reload explicitly, inside the async context where IO is allowed.
        self.db.expire_all()
        refreshed = await self.get_by_id(submission_id)
        reloaded = await self.db.get(Plant, plant_id) if plant_id is not None else None
        return (refreshed or submission), reloaded
