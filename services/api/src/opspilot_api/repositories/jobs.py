"""RQ job bodies for repository synchronization, snapshot indexing, and webhook processing.

Jobs are idempotent (snapshots are unique per commit, commits/deployments are upserted) and safe
to retry. Provider rate limits and transient outages reschedule the job with bounded attempts.
"""

import json
import logging
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.repositories.provider import (
    ProviderError,
    ProviderRateLimitError,
    RepositoryProvider,
)
from opspilot_api.repositories.queue import get_provider, schedule_retry
from opspilot_api.repositories.sync import (
    sync_installation_repositories,
    sync_repository,
)
from opspilot_api.repositories.webhooks import process_delivery

logger = logging.getLogger(__name__)
_BASE_DELAY_SECONDS = 30


def _retry_delay(exc: ProviderError, attempt: int) -> int:
    if isinstance(exc, ProviderRateLimitError):
        return min(exc.retry_after_seconds, 3600)
    return int(min(_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), 900))


def _guarded(
    settings: Settings,
    function: str,
    args: list[object],
    attempt: int,
    job_id: str,
    action: Callable[[Session, RepositoryProvider], object],
    provider: RepositoryProvider | None,
) -> bool:
    """Run ``action``; reschedule on a retryable provider failure. Returns True if it ran."""
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            try:
                action(session, provider or get_provider(settings))
            except ProviderError as exc:
                if exc.retryable and attempt <= settings.REPO_RATE_LIMIT_MAX_RETRIES:
                    delay = _retry_delay(exc, attempt)
                    logger.warning(
                        "repository job rescheduled",
                        extra={"error_class": type(exc).__name__, "delay_seconds": delay},
                    )
                    schedule_retry(settings, function, [*args, attempt + 1], delay, job_id)
                    return False
                raise
    finally:
        engine.dispose()
    return True


def run_installation_sync_job(
    settings: Settings,
    workspace_id: UUID,
    installation_id: UUID,
    attempt: int = 1,
    provider: RepositoryProvider | None = None,
) -> bool:
    return _guarded(
        settings,
        "opspilot_api.worker.jobs.run_installation_sync",
        [str(workspace_id), str(installation_id)],
        attempt,
        f"github-installation-sync-{installation_id}-{attempt + 1}",
        lambda session, prov: sync_installation_repositories(
            session, settings, prov, workspace_id, installation_id
        ),
        provider,
    )


def run_repository_sync_job(
    settings: Settings,
    workspace_id: UUID,
    repository_id: UUID,
    options: str,
    attempt: int = 1,
    provider: RepositoryProvider | None = None,
) -> bool:
    parsed = json.loads(options) if options else {}
    return _guarded(
        settings,
        "opspilot_api.worker.jobs.run_repository_sync",
        [str(workspace_id), str(repository_id), options],
        attempt,
        f"github-repository-retry-{repository_id}-{attempt + 1}",
        lambda session, prov: sync_repository(
            session,
            settings,
            prov,
            workspace_id,
            repository_id,
            shas=list(parsed.get("shas") or []),
            force_reindex=bool(parsed.get("force_reindex")),
        ),
        provider,
    )


def run_webhook_delivery_job(
    settings: Settings,
    delivery_id: UUID,
    attempt: int = 1,
    provider: RepositoryProvider | None = None,
) -> bool:
    return _guarded(
        settings,
        "opspilot_api.worker.jobs.run_webhook_delivery",
        [str(delivery_id)],
        attempt,
        f"github-webhook-retry-{delivery_id}-{attempt + 1}",
        lambda session, prov: process_delivery(session, settings, prov, delivery_id),
        provider,
    )
