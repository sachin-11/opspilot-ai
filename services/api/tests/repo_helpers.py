"""Shared helpers for repository-integration tests (live PostgreSQL, fixture provider only)."""

import copy
import json
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings, get_settings
from opspilot_api.incidents.models import (
    Incident,
    IngestedEvent,
    MonitoredService,
    Workspace,
)
from opspilot_api.incidents.service import utcnow
from opspilot_api.repositories.fixture_provider import DEFAULT_FIXTURE, FixtureProvider
from opspilot_api.repositories.models import (
    GitHubInstallation,
    Repository,
    ServiceRepositoryMapping,
)
from opspilot_api.repositories.sync import (
    sync_installation_repositories,
    sync_repository,
    upsert_installation,
)


def live_settings(**overrides: Any) -> Settings:
    get_settings.cache_clear()
    return get_settings().model_copy(update=overrides)


def make_provider(offset: int | None = None) -> FixtureProvider:
    """A fixture provider whose installation/repository IDs are unique to this test run."""
    offset = offset if offset is not None else secrets.randbelow(10**9) * 1000
    data = copy.deepcopy(json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8")))
    for installation in data["installations"]:
        installation["id"] += offset
        for repo in installation["repositories"]:
            repo["id"] += offset
    provider = FixtureProvider(data=data)
    provider.offset = offset  # type: ignore[attr-defined]
    return provider


def installation_id(provider: FixtureProvider, base: int) -> int:
    return base + int(provider.offset)  # type: ignore[attr-defined]


def new_workspace(session: Session, label: str = "repo") -> Workspace:
    suffix = secrets.token_hex(5)
    workspace = Workspace(name=f"{label} {suffix}", slug=f"{label}-{suffix}")
    session.add(workspace)
    session.flush()
    return workspace


def new_service(
    session: Session, workspace: Workspace, name: str, environment: str = "production"
) -> MonitoredService:
    service = MonitoredService(
        workspace_id=workspace.id,
        name=name,
        slug=name.lower().replace(" ", "-"),
        environment=environment,
    )
    session.add(service)
    session.flush()
    return service


def connect_installation(
    session: Session,
    settings: Settings,
    provider: FixtureProvider,
    workspace: Workspace,
    base_installation: int = 1001,
) -> tuple[GitHubInstallation, dict[str, Repository]]:
    info = provider.get_installation(installation_id(provider, base_installation))
    installation = upsert_installation(session, workspace.id, info)
    session.commit()
    sync_installation_repositories(session, settings, provider, workspace.id, installation.id)
    repositories = {
        item.full_name: item
        for item in session.scalars(
            select(Repository).where(Repository.workspace_id == workspace.id)
        )
    }
    return installation, repositories


def sync(
    session: Session,
    settings: Settings,
    provider: FixtureProvider,
    workspace: Workspace,
    repository: Repository,
    **kwargs: Any,
) -> dict[str, Any]:
    return sync_repository(session, settings, provider, workspace.id, repository.id, **kwargs)


def map_service(
    session: Session,
    workspace: Workspace,
    service: MonitoredService,
    repository: Repository,
    path_prefix: str | None = None,
    environment: str = "production",
    branch_pattern: str = "main",
) -> ServiceRepositoryMapping:
    mapping = ServiceRepositoryMapping(
        workspace_id=workspace.id,
        service_id=service.id,
        repository_id=repository.id,
        environment=environment,
        branch_pattern=branch_pattern,
        path_prefix=path_prefix,
        path_prefix_key=path_prefix or "",
    )
    session.add(mapping)
    session.commit()
    return mapping


def new_incident(
    session: Session,
    workspace: Workspace,
    service: MonitoredService | None,
    title: str,
    opened_at: datetime,
    description: str | None = None,
    events: list[tuple[str, str | None]] | None = None,
) -> Incident:
    incident = Incident(
        workspace_id=workspace.id,
        service_id=service.id if service else None,
        title=title,
        description=description,
        severity="sev2",
        status="open",
        source="manual",
        opened_at=opened_at,
    )
    session.add(incident)
    session.flush()
    for index, (event_type, signature) in enumerate(events or []):
        assert service is not None
        session.add(
            IngestedEvent(
                workspace_id=workspace.id,
                service_id=service.id,
                incident_id=incident.id,
                idempotency_key=f"{incident.id}-{index}",
                source="test",
                event_type=event_type,
                severity="sev2",
                summary=event_type,
                error_signature=signature,
                attributes={},
                fingerprint="f" * 64,
                occurred_at=opened_at,
            )
        )
    session.commit()
    return incident


def reload_repository(session: Session, repository_id: UUID) -> Repository:
    session.expire_all()
    return session.get(Repository, repository_id)  # type: ignore[return-value]


__all__ = [
    "connect_installation",
    "installation_id",
    "live_settings",
    "make_provider",
    "map_service",
    "new_incident",
    "new_service",
    "new_workspace",
    "reload_repository",
    "sync",
    "utcnow",
]
