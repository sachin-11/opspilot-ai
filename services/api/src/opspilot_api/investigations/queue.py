"""RQ enqueue helpers kept separate from HTTP handlers and workflow logic."""

from uuid import UUID

from redis import Redis
from rq import Queue

from opspilot_api.config import Settings


def _queue(settings: Settings) -> tuple[Queue, Redis]:
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    return Queue("default", connection=connection), connection


def enqueue_investigation(settings: Settings, workspace_id: UUID, investigation_id: UUID) -> None:
    queue, connection = _queue(settings)
    try:
        queue.enqueue(
            "opspilot_api.worker.jobs.run_investigation",
            str(workspace_id),
            str(investigation_id),
            job_id=f"investigation-{investigation_id}",
            job_timeout=settings.AI_MAX_RUNTIME_SECONDS + 60,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()


def enqueue_review_resume(
    settings: Settings, workspace_id: UUID, investigation_id: UUID, review_id: UUID
) -> None:
    queue, connection = _queue(settings)
    try:
        queue.enqueue(
            "opspilot_api.worker.jobs.resume_investigation",
            str(workspace_id),
            str(investigation_id),
            str(review_id),
            job_id=f"investigation-review-{review_id}",
            job_timeout=settings.AI_MAX_RUNTIME_SECONDS + 60,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()
