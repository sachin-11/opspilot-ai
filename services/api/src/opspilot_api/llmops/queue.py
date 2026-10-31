"""RQ enqueue helper for evaluation runs (kept out of HTTP handlers)."""

from uuid import UUID

from redis import Redis
from rq import Queue

from opspilot_api.config import Settings


def enqueue_eval_run(settings: Settings, eval_run_id: UUID) -> None:
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        Queue("default", connection=connection).enqueue(
            "opspilot_api.llmops.jobs.run_eval_job",
            str(eval_run_id),
            job_id=f"eval-run-{eval_run_id}",
            job_timeout=900,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()
