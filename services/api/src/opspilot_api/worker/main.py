"""RQ worker entrypoint: ``python -m opspilot_api.worker.main``."""

import asyncio
import logging
import socket
from types import TracebackType

from redis import Redis
from rq import Worker
from rq.job import Job
from sqlalchemy.exc import SQLAlchemyError

from opspilot_api.config import Settings, get_settings
from opspilot_api.infrastructure import create_db_engine, create_session_factory
from opspilot_api.investigations.service import recover_stale_investigations
from opspilot_api.knowledge.service import recover_stale_jobs
from opspilot_api.logging_config import configure_logging
from opspilot_api.repositories.service import recover_stale_repository_work

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


async def recover_stale_work(settings: Settings) -> tuple[int, int, int]:
    """Fail abandoned running investigations before accepting new jobs."""
    engine = create_db_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            investigations = await recover_stale_investigations(session, settings)
            ingestion_jobs = await recover_stale_jobs(session, settings)
            repository_work = await recover_stale_repository_work(session, settings)
            return investigations, ingestion_jobs, repository_work
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL.value)
    try:
        recovered_investigations, recovered_ingestions, recovered_repositories = asyncio.run(
            recover_stale_work(settings)
        )
    except SQLAlchemyError as exc:
        # On the first boot Compose starts the worker before Alembic creates the
        # investigation tables. Recovery is retried on the next worker start.
        logger.warning(
            "stale investigation recovery deferred",
            extra={"error_class": type(exc).__name__},
        )
    else:
        if recovered_investigations:
            logger.warning(
                "stale investigations recovered",
                extra={"count": recovered_investigations},
            )
        if recovered_ingestions:
            logger.warning(
                "stale knowledge ingestions recovered",
                extra={"count": recovered_ingestions},
            )
        if recovered_repositories:
            logger.warning(
                "stale repository work recovered",
                extra={"count": recovered_repositories},
            )
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
        # The scheduler releases rate-limited repository jobs at their provider reset time.
        worker.work(with_scheduler=True)
    finally:
        logger.info("worker stopped", extra={"worker": worker.name})
        connection.close()


if __name__ == "__main__":
    main()
