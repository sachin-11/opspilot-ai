"""Repository application services. Every query begins with a workspace predicate."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, MonitoredService
from opspilot_api.incidents.service import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from opspilot_api.repositories.correlation import NOTICE, rank_suspect_changes
from opspilot_api.repositories.models import (
    CommitFileChange,
    GitHubInstallation,
    InstallationStatus,
    Repository,
    RepositoryCommit,
    RepositoryDeployment,
    RepositorySnapshot,
    RepositoryStatus,
    ServiceRepositoryMapping,
    SnapshotStatus,
)
from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    RepositoryProvider,
)
from opspilot_api.repositories.queue import configuration_hint
from opspilot_api.repositories.retrieval import CodeRetrievalService
from opspilot_api.repositories.schemas import (
    CodeSearchRequest,
    CommitFileSummary,
    CommitResponse,
    DeploymentResponse,
    InstallationListResponse,
    InstallationResponse,
    MappedServiceSummary,
    MappingCreate,
    MappingResponse,
    RepositoryDetail,
    RepositorySummary,
    SnapshotSummary,
    SuspectChange,
    SuspectChangesResponse,
)
from opspilot_api.repositories.sync import InstallationConflictError, upsert_installation


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class ProviderRateLimitedError(DomainError):
    status_code = 429
    code = "provider_rate_limited"


class ProviderUnavailableAPIError(DomainError):
    status_code = 503
    code = "provider_unavailable"


class ProviderRejectedError(DomainError):
    status_code = 502
    code = "provider_rejected"


def translate_provider_error(exc: ProviderError) -> DomainError:
    """Sanitized, credential-free API errors for provider failures."""
    if isinstance(exc, ProviderNotFoundError):
        return NotFoundError("GitHub resource not found")
    if isinstance(exc, ProviderRateLimitError):
        return ProviderRateLimitedError(
            f"GitHub rate limit reached; retry in about {exc.retry_after_seconds} seconds"
        )
    if isinstance(exc, ProviderAuthenticationError):
        return ProviderRejectedError("GitHub rejected the App credentials or installation access")
    return ProviderUnavailableAPIError("GitHub is temporarily unavailable")


# -- installations ---------------------------------------------------------------------------
def connect_installation(
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    provider_installation_id: int,
) -> UUID:
    """Validate and upsert an installation (one read-only provider call), then return its ID."""
    from sqlalchemy import create_engine

    allowed = settings.GITHUB_ALLOWED_INSTALLATION_IDS
    if allowed and provider_installation_id not in allowed:
        raise ForbiddenError("installation is not in the configured allow-list")
    try:
        info = provider.get_installation(provider_installation_id)
    except ProviderError as exc:
        raise translate_provider_error(exc) from exc
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            try:
                installation = upsert_installation(session, workspace_id, info)
            except InstallationConflictError as exc:
                raise ConflictError(str(exc)) from exc
            session.commit()
            return installation.id
    finally:
        engine.dispose()


async def list_installations(
    session: AsyncSession, settings: Settings, workspace_id: UUID
) -> InstallationListResponse:
    rows = (
        await session.scalars(
            select(GitHubInstallation)
            .where(GitHubInstallation.workspace_id == workspace_id)
            .order_by(GitHubInstallation.account_login, GitHubInstallation.id)
        )
    ).all()
    stats = {
        row[0]: (row[1], row[2])
        for row in (
            await session.execute(
                select(
                    Repository.github_installation_id,
                    func.count(Repository.id),
                    func.max(Repository.last_synced_at),
                )
                .where(
                    Repository.workspace_id == workspace_id,
                    Repository.status != RepositoryStatus.REMOVED.value,
                )
                .group_by(Repository.github_installation_id)
            )
        ).all()
    }
    items = []
    for row in rows:
        count, last = stats.get(row.id, (0, None))
        items.append(
            InstallationResponse.model_validate(row).model_copy(
                update={"repository_count": count, "last_synced_at": last}
            )
        )
    return InstallationListResponse(
        provider=settings.GITHUB_PROVIDER,
        provider_configured=settings.GITHUB_PROVIDER == "github"
        or settings.GITHUB_PROVIDER == "fixture",
        configuration_hint=configuration_hint(settings),
        installations=items,
    )


# -- repositories ----------------------------------------------------------------------------
async def _snapshot(session: AsyncSession, workspace_id: UUID, snapshot_id: UUID | None) -> Any:
    if snapshot_id is None:
        return None
    row = await session.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.id == snapshot_id, RepositorySnapshot.workspace_id == workspace_id
        )
    )
    return SnapshotSummary.model_validate(row) if row else None


async def _connected(
    session: AsyncSession, workspace_id: UUID, repository_ids: list[UUID]
) -> dict[UUID, list[MappedServiceSummary]]:
    if not repository_ids:
        return {}
    rows = await session.execute(
        select(ServiceRepositoryMapping, MonitoredService.name)
        .join(
            MonitoredService,
            (MonitoredService.id == ServiceRepositoryMapping.service_id)
            & (MonitoredService.workspace_id == workspace_id),
        )
        .where(
            ServiceRepositoryMapping.workspace_id == workspace_id,
            ServiceRepositoryMapping.repository_id.in_(repository_ids),
        )
        .order_by(MonitoredService.name, ServiceRepositoryMapping.id)
    )
    result: dict[UUID, list[MappedServiceSummary]] = {}
    for mapping, name in rows.all():
        result.setdefault(mapping.repository_id, []).append(
            MappedServiceSummary(
                mapping_id=mapping.id,
                service_id=mapping.service_id,
                service_name=name,
                environment=mapping.environment,
                branch_pattern=mapping.branch_pattern,
                path_prefix=mapping.path_prefix,
            )
        )
    return result


async def list_repositories(
    session: AsyncSession,
    workspace_id: UUID,
    status: RepositoryStatus | None = None,
    service_id: UUID | None = None,
) -> list[RepositorySummary]:
    statement = select(Repository).where(Repository.workspace_id == workspace_id)
    if status is not None:
        statement = statement.where(Repository.status == status.value)
    else:
        statement = statement.where(Repository.status != RepositoryStatus.REMOVED.value)
    if service_id is not None:
        statement = statement.where(
            Repository.id.in_(
                select(ServiceRepositoryMapping.repository_id).where(
                    ServiceRepositoryMapping.workspace_id == workspace_id,
                    ServiceRepositoryMapping.service_id == service_id,
                )
            )
        )
    repositories = (await session.scalars(statement.order_by(Repository.full_name))).all()
    connected = await _connected(session, workspace_id, [item.id for item in repositories])
    summaries = []
    for item in repositories:
        summary = RepositorySummary.model_validate(item)
        summary.active_snapshot = await _snapshot(session, workspace_id, item.active_snapshot_id)
        summary.connected_services = connected.get(item.id, [])
        summaries.append(summary)
    return summaries


async def require_repository(
    session: AsyncSession, workspace_id: UUID, repository_id: UUID
) -> Repository:
    repository = await session.scalar(
        select(Repository).where(
            Repository.id == repository_id, Repository.workspace_id == workspace_id
        )
    )
    if repository is None:
        raise NotFoundError("repository not found")
    return repository


async def repository_detail(
    session: AsyncSession, workspace_id: UUID, repository_id: UUID
) -> RepositoryDetail:
    repository = await require_repository(session, workspace_id, repository_id)
    summary = RepositorySummary.model_validate(repository)
    snapshots = (
        await session.scalars(
            select(RepositorySnapshot)
            .where(
                RepositorySnapshot.workspace_id == workspace_id,
                RepositorySnapshot.repository_id == repository.id,
            )
            .order_by(RepositorySnapshot.created_at.desc())
            .limit(10)
        )
    ).all()
    commit_count = await session.scalar(
        select(func.count(RepositoryCommit.id)).where(
            RepositoryCommit.workspace_id == workspace_id,
            RepositoryCommit.repository_id == repository.id,
        )
    )
    deployment_count = await session.scalar(
        select(func.count(RepositoryDeployment.id)).where(
            RepositoryDeployment.workspace_id == workspace_id,
            RepositoryDeployment.repository_id == repository.id,
        )
    )
    connected = await _connected(session, workspace_id, [repository.id])
    return RepositoryDetail(
        **summary.model_dump(exclude={"active_snapshot", "connected_services"}),
        active_snapshot=await _snapshot(session, workspace_id, repository.active_snapshot_id),
        connected_services=connected.get(repository.id, []),
        recent_snapshots=[SnapshotSummary.model_validate(item) for item in snapshots],
        commit_count=commit_count or 0,
        deployment_count=deployment_count or 0,
    )


async def require_syncable(
    session: AsyncSession, workspace_id: UUID, repository_id: UUID
) -> Repository:
    repository = await require_repository(session, workspace_id, repository_id)
    installation = await session.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == repository.github_installation_id,
            GitHubInstallation.workspace_id == workspace_id,
        )
    )
    if installation is None or installation.status != InstallationStatus.ACTIVE.value:
        raise ConflictError("the GitHub installation is not active")
    if repository.status == RepositoryStatus.REMOVED.value:
        raise ConflictError("the repository was removed from the installation")
    return repository


async def require_installation_active(
    session: AsyncSession, workspace_id: UUID, installation_id: UUID
) -> GitHubInstallation:
    installation = await session.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == installation_id,
            GitHubInstallation.workspace_id == workspace_id,
        )
    )
    if installation is None:
        raise NotFoundError("installation not found")
    return installation


# -- service mappings ------------------------------------------------------------------------
async def create_mapping(
    session: AsyncSession, workspace_id: UUID, service_id: UUID, data: MappingCreate
) -> MappingResponse:
    service = await session.scalar(
        select(MonitoredService).where(
            MonitoredService.id == service_id, MonitoredService.workspace_id == workspace_id
        )
    )
    if service is None:
        raise NotFoundError("service not found")
    repository = await session.scalar(
        select(Repository).where(
            Repository.id == data.repository_id, Repository.workspace_id == workspace_id
        )
    )
    if repository is None:
        raise NotFoundError("repository not found")
    if repository.status == RepositoryStatus.REMOVED.value:
        raise ValidationError("a removed repository cannot be mapped")
    mapping = ServiceRepositoryMapping(
        workspace_id=workspace_id,
        service_id=service.id,
        repository_id=repository.id,
        environment=data.environment.value,
        branch_pattern=data.branch_pattern,
        path_prefix=data.path_prefix,
        path_prefix_key=data.path_prefix or "",
    )
    session.add(mapping)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("an identical service/repository mapping already exists") from exc
    await session.refresh(mapping)
    return _mapping_response(mapping, repository.full_name)


def _mapping_response(mapping: ServiceRepositoryMapping, full_name: str) -> MappingResponse:
    return MappingResponse(
        id=mapping.id,
        workspace_id=mapping.workspace_id,
        service_id=mapping.service_id,
        repository_id=mapping.repository_id,
        repository_full_name=full_name,
        environment=mapping.environment,
        branch_pattern=mapping.branch_pattern,
        path_prefix=mapping.path_prefix,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


async def list_mappings(
    session: AsyncSession, workspace_id: UUID, service_id: UUID
) -> list[MappingResponse]:
    service = await session.scalar(
        select(MonitoredService.id).where(
            MonitoredService.id == service_id, MonitoredService.workspace_id == workspace_id
        )
    )
    if service is None:
        raise NotFoundError("service not found")
    rows = await session.execute(
        select(ServiceRepositoryMapping, Repository.full_name)
        .join(
            Repository,
            (Repository.id == ServiceRepositoryMapping.repository_id)
            & (Repository.workspace_id == workspace_id),
        )
        .where(
            ServiceRepositoryMapping.workspace_id == workspace_id,
            ServiceRepositoryMapping.service_id == service_id,
        )
        .order_by(Repository.full_name, ServiceRepositoryMapping.id)
    )
    return [_mapping_response(mapping, name) for mapping, name in rows.all()]


async def delete_mapping(
    session: AsyncSession, workspace_id: UUID, service_id: UUID, mapping_id: UUID
) -> UUID:
    """Removes only the mapping row; repository data is untouched. Returns the repository ID."""
    mapping = await session.scalar(
        select(ServiceRepositoryMapping).where(
            ServiceRepositoryMapping.id == mapping_id,
            ServiceRepositoryMapping.service_id == service_id,
            ServiceRepositoryMapping.workspace_id == workspace_id,
        )
    )
    if mapping is None:
        raise NotFoundError("mapping not found")
    repository_id = mapping.repository_id
    await session.delete(mapping)
    await session.commit()
    return repository_id


# -- commits and deployments -----------------------------------------------------------------
async def list_commits(
    session: AsyncSession, workspace_id: UUID, repository_id: UUID, limit: int
) -> list[CommitResponse]:
    repository = await require_repository(session, workspace_id, repository_id)
    commits = (
        await session.scalars(
            select(RepositoryCommit)
            .where(
                RepositoryCommit.workspace_id == workspace_id,
                RepositoryCommit.repository_id == repository.id,
            )
            .order_by(RepositoryCommit.committed_at.desc(), RepositoryCommit.provider_commit_sha)
            .limit(limit)
        )
    ).all()
    files: dict[UUID, list[CommitFileSummary]] = {}
    if commits:
        for change in await session.scalars(
            select(CommitFileChange)
            .where(
                CommitFileChange.workspace_id == workspace_id,
                CommitFileChange.commit_id.in_([item.id for item in commits]),
            )
            .order_by(CommitFileChange.path)
        ):
            files.setdefault(change.commit_id, []).append(
                CommitFileSummary(
                    path=change.path,
                    previous_path=change.previous_path,
                    change_type=change.change_type,
                    additions=change.additions,
                    deletions=change.deletions,
                )
            )
    return [
        CommitResponse(
            id=item.id,
            repository_id=repository.id,
            repository_full_name=repository.full_name,
            sha=item.provider_commit_sha,
            author_name=item.author_name,
            message=item.message,
            committed_at=item.committed_at,
            changed_files=item.changed_files,
            additions=item.additions,
            deletions=item.deletions,
            provider_url=item.provider_url,
            files=files.get(item.id, [])[:30],
        )
        for item in commits
    ]


def deployment_response(item: RepositoryDeployment, full_name: str) -> DeploymentResponse:
    return DeploymentResponse(
        id=item.id,
        repository_id=item.repository_id,
        repository_full_name=full_name,
        provider_deployment_id=item.provider_deployment_id,
        commit_sha=item.commit_sha,
        environment=item.environment,
        status=item.status,
        description=item.description,
        deployed_at=item.deployed_at,
        completed_at=item.completed_at,
    )


async def list_deployments(
    session: AsyncSession, workspace_id: UUID, repository_id: UUID, limit: int
) -> list[DeploymentResponse]:
    repository = await require_repository(session, workspace_id, repository_id)
    rows = (
        await session.scalars(
            select(RepositoryDeployment)
            .where(
                RepositoryDeployment.workspace_id == workspace_id,
                RepositoryDeployment.repository_id == repository.id,
            )
            .order_by(RepositoryDeployment.deployed_at.desc(), RepositoryDeployment.id)
            .limit(limit)
        )
    ).all()
    return [deployment_response(item, repository.full_name) for item in rows]


# -- incident suspect changes ----------------------------------------------------------------
def suspect_changes(
    settings: Settings, workspace_id: UUID, incident_id: UUID
) -> SuspectChangesResponse:
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            incident = session.scalar(
                select(Incident).where(
                    Incident.id == incident_id, Incident.workspace_id == workspace_id
                )
            )
            if incident is None:
                raise NotFoundError("incident not found")
            result = rank_suspect_changes(session, settings, workspace_id, incident_id, limit=5)
            excerpts = []
            if result.has_repository_mapping and incident.service_id:
                query = f"{incident.title} {' '.join(result.candidates[0].changed_files)}".strip()
                request = CodeSearchRequest(
                    query=query[:900],
                    service_id=incident.service_id,
                    candidate_count=10,
                    top_k=3,
                )
                excerpts = (
                    CodeRetrievalService(settings).search(session, workspace_id, request).results
                    if result.candidates
                    else []
                )
            candidates = [
                SuspectChange(
                    kind=item.kind,
                    repository_id=item.repository_id,
                    repository_full_name=item.repository_full_name,
                    commit_sha=item.commit_sha,
                    commit_id=item.commit_id,
                    deployment_id=item.deployment_id,
                    title=item.title,
                    score=item.score,
                    confidence=item.confidence,
                    matched_signals=item.matched_signals,
                    contradicting_signals=item.contradicting_signals,
                    minutes_before_incident=item.minutes_before_incident,
                    changed_files=item.changed_files,
                    environment=item.environment,
                    citation_label=item.citation_label,
                )
                for item in result.candidates
            ]
            deployments = [
                deployment_response(item, result.repositories[item.repository_id].full_name)
                for item in result.deployments[:10]
                if item.repository_id in result.repositories
            ]
            return SuspectChangesResponse(
                incident_id=incident.id,
                opened_at=result.opened_at,
                service_id=result.service_id,
                has_repository_mapping=result.has_repository_mapping,
                candidates=candidates,
                deployments=deployments,
                code_excerpts=excerpts,
                notice=NOTICE,
            )
    finally:
        engine.dispose()


async def suspect_changes_async(
    settings: Settings, workspace_id: UUID, incident_id: UUID
) -> SuspectChangesResponse:
    return await asyncio.to_thread(suspect_changes, settings, workspace_id, incident_id)


async def recover_stale_repository_work(session: AsyncSession, settings: Settings) -> int:
    """Fail provably abandoned indexing/sync work; previous ready snapshots stay active."""
    from opspilot_api.incidents.service import utcnow

    now = utcnow()
    cutoff = now - timedelta(seconds=max(60, settings.REPO_INDEX_TIMEOUT_SECONDS * 2))
    snapshots = await session.execute(
        update(RepositorySnapshot)
        .where(
            RepositorySnapshot.status == SnapshotStatus.INDEXING.value,
            RepositorySnapshot.updated_at < cutoff,
        )
        .values(
            status=SnapshotStatus.FAILED.value,
            failed_at=now,
            failure_code="stale_worker",
            failure_message="Indexing worker stopped before completion.",
        )
        .returning(RepositorySnapshot.id)
    )
    repositories = await session.execute(
        update(Repository)
        .where(
            Repository.status == RepositoryStatus.SYNCING.value,
            Repository.updated_at < cutoff,
        )
        .values(
            status=RepositoryStatus.FAILED.value,
            failure_code="stale_worker",
            failure_message="Sync worker stopped before completion.",
        )
        .returning(Repository.id)
    )
    count = len(list(snapshots.scalars())) + len(list(repositories.scalars()))
    await session.commit()
    return count
