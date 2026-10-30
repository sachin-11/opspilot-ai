"""RQ enqueue helpers for bounded knowledge ingestion jobs."""

from uuid import UUID

from redis import Redis
from rq import Queue

from opspilot_api.config import Settings


def enqueue_ingestion(settings: Settings, workspace_id: UUID, job_id: UUID) -> None:
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        Queue("default", connection=connection).enqueue(
            "opspilot_api.worker.jobs.run_knowledge_ingestion",
            str(workspace_id),
            str(job_id),
            job_id=f"knowledge-ingestion-{job_id}",
            job_timeout=settings.AI_MAX_RUNTIME_SECONDS + 120,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()
