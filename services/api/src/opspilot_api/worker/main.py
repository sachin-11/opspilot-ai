"""RQ worker entrypoint: ``python -m opspilot_api.worker.main``."""

import logging
import socket
from types import TracebackType

from redis import Redis
from rq import Worker
from rq.job import Job

from opspilot_api.config import Settings, get_settings
from opspilot_api.logging_config import configure_logging

logger = logging.getLogger("opspilot_api.worker")

QUEUES = ["default"]
WORKER_TTL_SECONDS = 60


def worker_name() -> str:
    return f"opspilot-worker-{socket.gethostname()}"


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_connect_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
    )


def log_job_failure(
    job: Job,
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType | None,
) -> bool:
    logger.error(
        "job failed",
        extra={"job_id": job.id, "job_func": job.func_name},
        exc_info=(exc_type, exc_value, traceback),
    )
    # True keeps the default handling, so the job still lands in the failed registry.
    return True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL.value)
    connection = create_redis(settings)
    worker = Worker(
        QUEUES,
        name=worker_name(),
        connection=connection,
        worker_ttl=WORKER_TTL_SECONDS,
        exception_handlers=[log_job_failure],
    )
    logger.info("worker starting", extra={"queues": QUEUES, "worker": worker.name})
    try:
        worker.work(with_scheduler=False)
    finally:
        logger.info("worker stopped", extra={"worker": worker.name})
        connection.close()


if __name__ == "__main__":
    main()
