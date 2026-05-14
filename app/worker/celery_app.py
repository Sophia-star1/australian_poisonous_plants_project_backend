"""Celery application instance configuration (loaded only when broker is configured)."""

from celery import Celery

from app.config import settings

celery: Celery | None = None

if settings.celery_enabled:
    celery = Celery(
        "apl_worker",
        broker=settings.CELERY_BROKER_URL,
        include=[
            "app.worker.tasks.email",
            "app.worker.tasks.report",
        ],
    )

    celery.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
    )
