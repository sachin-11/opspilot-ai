"""RQ enqueue helpers. Remediation runs on its own queue so only sandbox-capable workers take it."""

from uuid import UUID, uuid4

from redis import Redis
from rq import Queue

from opspilot_api.config import Settings

REMEDIATION_QUEUE = "remediation"


def _enqueue(
    settings: Settings, function: str, args: list[object], job_id: str, timeout: int
) -> None:
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        Queue(REMEDIATION_QUEUE, connection=connection).enqueue(
            function,
            *args,
            job_id=job_id,
            job_timeout=timeout,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()


def job_timeout(settings: Settings) -> int:
    # baseline + patched runs, each bounded by the sandbox timeout per registered command.
    return settings.AI_MAX_RUNTIME_SECONDS + settings.REMEDIATION_SANDBOX_TIMEOUT_SECONDS * 8 + 120


def new_thread_id(remediation_id: UUID) -> str:
    return f"remediation-{remediation_id}-{uuid4().hex[:8]}"


def enqueue_remediation(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    thread_id: str,
    entry: str = "start",
) -> None:
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.run_remediation",
        [str(workspace_id), str(remediation_id), thread_id, entry],
        f"remediation-{remediation_id}-{thread_id[-8:]}",
        job_timeout(settings),
    )


def enqueue_remediation_resume(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    thread_id: str,
    approval_id: UUID,
) -> None:
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.resume_remediation",
        [str(workspace_id), str(remediation_id), thread_id, str(approval_id)],
        f"remediation-review-{approval_id}",
        job_timeout(settings),
    )


def enqueue_publish(
    settings: Settings, workspace_id: UUID, remediation_id: UUID, approval_id: UUID
) -> None:
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.run_remediation_publish",
        [str(workspace_id), str(remediation_id)],
        f"remediation-publish-{approval_id}-{uuid4().hex[:6]}",
        int(settings.REPO_INDEX_TIMEOUT_SECONDS) + 120,
    )
