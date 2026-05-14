"""Email-related Celery tasks."""

from app.worker.celery_app import celery


@celery.task(name="send_email")
def send_email_task(to: str, subject: str, body: str) -> bool:
    """Send a transactional email asynchronously."""
    ...
