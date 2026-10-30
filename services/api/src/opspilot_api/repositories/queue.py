"""Provider factory and RQ enqueue helpers for repository synchronization work."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import Redis
from rq import Queue

from opspilot_api.config import Settings
from opspilot_api.repositories.fixture_provider import FIXTURE_ANCHOR, FixtureProvider
from opspilot_api.repositories.github_provider import GitHubProvider
from opspilot_api.repositories.provider import RepositoryProvider

_PROVIDERS: dict[str, RepositoryProvider] = {}
JOB_TIMEOUT_PADDING = 120


def get_provider(settings: Settings) -> RepositoryProvider:
    """One provider per process so GitHub installation tokens are cached for their lifetime."""
    key = f"{settings.GITHUB_PROVIDER}:{settings.GITHUB_APP_ID}"
    provider = _PROVIDERS.get(key)
    if provider is None:
        if settings.GITHUB_PROVIDER == "github":
            provider = GitHubProvider(settings)
        else:
            shift = (
                datetime.now(UTC) - FIXTURE_ANCHOR
                if settings.GITHUB_FIXTURE_REBASE_TO_NOW
                else timedelta(0)
            )
            provider = FixtureProvider(shift=shift)
        _PROVIDERS[key] = provider
    return provider


def configuration_hint(settings: Settings) -> str | None:
    if settings.GITHUB_PROVIDER == "fixture":
        return (
            "Running with the local fixture provider (no GitHub credentials). Set "
            "GITHUB_PROVIDER=github with GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY[_FILE], and "
            "GITHUB_WEBHOOK_SECRET to connect a real GitHub App."
        )
    return None


def _enqueue(settings: Settings, function: str, args: list[object], job_id: str) -> None:
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        Queue("default", connection=connection).enqueue(
            function,
            *args,
            job_id=job_id,
            job_timeout=int(settings.REPO_INDEX_TIMEOUT_SECONDS) + JOB_TIMEOUT_PADDING,
            result_ttl=86400,
            failure_ttl=604800,
        )
    finally:
        connection.close()


def enqueue_installation_sync(
    settings: Settings, workspace_id: UUID, installation_id: UUID, attempt: int = 1
) -> None:
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.run_installation_sync",
        [str(workspace_id), str(installation_id), attempt],
        f"github-installation-sync-{installation_id}-{attempt}",
    )


def enqueue_repository_sync(
    settings: Settings,
    workspace_id: UUID,
    repository_id: UUID,
    *,
    force_reindex: bool = False,
    shas: list[str] | None = None,
    attempt: int = 1,
) -> None:
    options = json.dumps({"force_reindex": force_reindex, "shas": shas or []})
    mode = "reindex" if force_reindex else "sync"
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.run_repository_sync",
        [str(workspace_id), str(repository_id), options, attempt],
        f"github-repository-{mode}-{repository_id}-{attempt}",
    )


def enqueue_webhook(settings: Settings, delivery_id: UUID, attempt: int = 1) -> None:
    _enqueue(
        settings,
        "opspilot_api.worker.jobs.run_webhook_delivery",
        [str(delivery_id), attempt],
        f"github-webhook-{delivery_id}-{attempt}",
    )


def schedule_retry(
    settings: Settings, function: str, args: list[object], delay_seconds: int, job_id: str
) -> None:
    """Rate-limit-aware retry; requires the worker to run with the RQ scheduler enabled."""
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        Queue("default", connection=connection).enqueue_in(
            timedelta(seconds=delay_seconds),
            function,
            *args,
            job_id=job_id,
            job_timeout=int(settings.REPO_INDEX_TIMEOUT_SECONDS) + JOB_TIMEOUT_PADDING,
        )
    finally:
        connection.close()
