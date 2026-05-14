"""Report generation Celery tasks."""

from app.worker.celery_app import celery


@celery.task(name="generate_report")
def generate_report_task(report_type: str, params: dict[str, object]) -> str:
    """Generate a report and return its storage key."""
    ...
