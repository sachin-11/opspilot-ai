"""Installation/repository synchronization, snapshot indexing, and atomic snapshot activation.

All functions use synchronous sessions (worker context) and begin every query with a workspace
predicate. Provider calls are read-only and are made only after the installation and repository
state has been checked. A snapshot becomes searchable only when it is fully persisted and the
repository's ``active_snapshot_id`` is switched in the same transaction; a failed or cancelled
run leaves the previous ready snapshot active.
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.service import utcnow
from opspilot_api.investigations.safety import sanitize_error
from opspilot_api.knowledge.embeddings import EmbeddingRouter
from opspilot_api.repositories.code_chunking import chunk_file
from opspilot_api.repositories.events import publish_repository_event
from opspilot_api.repositories.models import (
    CodeChunk,
    CommitFileChange,
    GitHubInstallation,
    InstallationStatus,
    Repository,
    RepositoryCommit,
    RepositoryDeployment,
    RepositoryFile,
    RepositoryPullRequest,
    RepositorySnapshot,
    RepositoryStatus,
    SnapshotStatus,
)
from opspilot_api.repositories.policy import (
    decode_text,
    evaluate_path,
    is_generated,
    redact_code,
)
from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderCommit,
    ProviderDeployment,
    ProviderError,
    ProviderInstallation,
    ProviderPullRequest,
    ProviderRepository,
    RepositoryProvider,
)

logger = logging.getLogger(__name__)
MAX_COMMIT_FILES = 60
MAX_PULL_REQUESTS = 20
MAX_TREE_ENTRIES = 100_000
SNAPSHOTS_TO_KEEP = 2


class RepositoryStateError(Exception):
    """The installation or repository is not in a state that allows provider access."""

    code = "repository_unavailable"


class InstallationConflictError(Exception):
    code = "installation_conflict"


class IndexingCancelled(Exception):
    code = "cancelled"


class IndexingFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- state guards ---------------------------------------------------------------------------
def load_installation(
    session: Session, workspace_id: UUID, installation_id: UUID
) -> GitHubInstallation:
    installation = session.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == installation_id,
            GitHubInstallation.workspace_id == workspace_id,
        )
    )
    if installation is None:
        raise LookupError("installation not found in workspace")
    return installation


def load_repository(session: Session, workspace_id: UUID, repository_id: UUID) -> Repository:
    repository = session.scalar(
        select(Repository).where(
            Repository.id == repository_id, Repository.workspace_id == workspace_id
        )
    )
    if repository is None:
        raise LookupError("repository not found in workspace")
    return repository


def ensure_provider_access(installation: GitHubInstallation, repository: Repository | None) -> None:
    """Checked before every provider call."""
    if installation.status != InstallationStatus.ACTIVE.value:
        raise RepositoryStateError(f"installation is {installation.status}")
    if repository is not None and repository.status in {
        RepositoryStatus.REMOVED.value,
    }:
        raise RepositoryStateError("repository was removed from the installation")


def _record_rate_limit(installation: GitHubInstallation, provider: RepositoryProvider) -> None:
    info = provider.last_rate_limit()
    if info is not None:
        installation.rate_limit = {
            "limit": info.limit,
            "remaining": info.remaining,
            "reset_at": info.reset_at.isoformat() if info.reset_at else None,
            "observed_at": utcnow().isoformat(),
        }


# -- installations --------------------------------------------------------------------------
def upsert_installation(
    session: Session, workspace_id: UUID, info: ProviderInstallation
) -> GitHubInstallation:
    existing = session.scalar(
        select(GitHubInstallation).where(GitHubInstallation.provider_installation_id == info.id)
    )
    if existing is not None and existing.workspace_id != workspace_id:
        raise InstallationConflictError("installation is connected to another workspace")
    now = utcnow()
    status = InstallationStatus.SUSPENDED if info.suspended else InstallationStatus.ACTIVE
    if existing is None:
        existing = GitHubInstallation(
            id=uuid4(),
            workspace_id=workspace_id,
            provider_installation_id=info.id,
            account_login=info.account_login,
            account_type=info.account_type,
            status=status.value,
            repository_selection=info.repository_selection,
            permissions=dict(info.permissions),
            rate_limit={},
            installed_at=info.installed_at,
            suspended_at=now if info.suspended else None,
        )
        session.add(existing)
    else:
        if existing.status == InstallationStatus.REMOVED.value and not info.suspended:
            existing.removed_at = None
        existing.account_login = info.account_login
        existing.account_type = info.account_type
        existing.repository_selection = info.repository_selection
        existing.permissions = dict(info.permissions)
        if info.suspended and existing.status != InstallationStatus.SUSPENDED.value:
            existing.suspended_at = now
        if not info.suspended:
            existing.suspended_at = None
        existing.status = status.value
    session.flush()
    return existing


def sync_installation_repositories(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    installation_id: UUID,
) -> dict[str, int]:
    installation = load_installation(session, workspace_id, installation_id)
    ensure_provider_access(installation, None)
    info = provider.get_installation(installation.provider_installation_id)
    upsert_installation(session, workspace_id, info)
    session.commit()
    if installation.status != InstallationStatus.ACTIVE.value:
        raise RepositoryStateError(f"installation is {installation.status}")
    repositories = provider.list_installation_repositories(installation.provider_installation_id)
    _record_rate_limit(installation, provider)
    created = updated = conflicts = 0
    seen: set[int] = set()
    for item in repositories:
        seen.add(item.id)
        existing = session.scalar(
            select(Repository).where(Repository.provider_repository_id == item.id)
        )
        if existing is not None and existing.workspace_id != workspace_id:
            conflicts += 1  # never attach a repository already owned by another workspace
            continue
        if existing is None:
            session.add(_new_repository(workspace_id, installation, item))
            created += 1
        else:
            _apply_repository(existing, item, installation)
            updated += 1
    removed = 0
    for repository in session.scalars(
        select(Repository).where(
            Repository.workspace_id == workspace_id,
            Repository.github_installation_id == installation.id,
            Repository.status != RepositoryStatus.REMOVED.value,
        )
    ):
        if repository.provider_repository_id not in seen:
            repository.status = RepositoryStatus.REMOVED.value
            removed += 1
    session.commit()
    return {"created": created, "updated": updated, "removed": removed, "conflicts": conflicts}


def _new_repository(
    workspace_id: UUID, installation: GitHubInstallation, item: ProviderRepository
) -> Repository:
    return Repository(
        id=uuid4(),
        workspace_id=workspace_id,
        github_installation_id=installation.id,
        provider_repository_id=item.id,
        owner=item.owner,
        name=item.name,
        full_name=item.full_name,
        default_branch=item.default_branch,
        is_private=item.private,
        is_archived=item.archived,
        status=(RepositoryStatus.ARCHIVED if item.archived else RepositoryStatus.ACTIVE).value,
    )


def _apply_repository(
    repository: Repository, item: ProviderRepository, installation: GitHubInstallation
) -> None:
    repository.github_installation_id = installation.id
    repository.owner = item.owner
    repository.name = item.name
    repository.full_name = item.full_name
    repository.default_branch = item.default_branch
    repository.is_private = item.private
    repository.is_archived = item.archived
    if item.archived:
        repository.status = RepositoryStatus.ARCHIVED.value
    elif repository.status in {RepositoryStatus.REMOVED.value, RepositoryStatus.ARCHIVED.value}:
        repository.status = RepositoryStatus.ACTIVE.value


# -- commits, deployments, pull requests ----------------------------------------------------
def _store_commit(
    session: Session, settings: Settings, repository: Repository, commit: ProviderCommit
) -> RepositoryCommit:
    existing = session.scalar(
        select(RepositoryCommit).where(
            RepositoryCommit.repository_id == repository.id,
            RepositoryCommit.workspace_id == repository.workspace_id,
            RepositoryCommit.provider_commit_sha == commit.sha,
        )
    )
    if existing is not None and existing.changed_files > 0:
        return existing
    if existing is None:
        email = commit.author_email.strip().lower() if commit.author_email else None
        existing = RepositoryCommit(
            id=uuid4(),
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            provider_commit_sha=commit.sha,
            author_name=redact_code(commit.author_name)[:200] if commit.author_name else None,
            author_email_hash=hashlib.sha256(email.encode()).hexdigest() if email else None,
            message=redact_code(commit.message)[:2000],
            committed_at=commit.committed_at,
            parent_shas=list(commit.parents),
            changed_files=len(commit.files),
            additions=commit.additions,
            deletions=commit.deletions,
            provider_url=commit.url[:500],
        )
        session.add(existing)
        session.flush()
    else:
        existing.changed_files = len(commit.files)
        existing.additions = commit.additions
        existing.deletions = commit.deletions
    known = set(
        session.scalars(
            select(CommitFileChange.path).where(
                CommitFileChange.workspace_id == repository.workspace_id,
                CommitFileChange.commit_id == existing.id,
            )
        )
    )
    for change in commit.files[:MAX_COMMIT_FILES]:
        if change.path in known:
            continue
        patch = redact_code(change.patch or "")
        truncated = len(patch) > settings.REPO_MAX_PATCH_EXCERPT_CHARS or (
            change.patch is not None and len(change.patch) >= 19_000
        )
        excerpt = patch[: settings.REPO_MAX_PATCH_EXCERPT_CHARS] if change.patch else None
        session.add(
            CommitFileChange(
                id=uuid4(),
                workspace_id=repository.workspace_id,
                repository_id=repository.id,
                commit_id=existing.id,
                path=change.path[:1000],
                previous_path=change.previous_path[:1000] if change.previous_path else None,
                change_type=change.change_type,
                additions=change.additions,
                deletions=change.deletions,
                patch_excerpt=excerpt,
                patch_truncated=truncated,
                content_hash=hashlib.sha256(f"{change.path}\n{excerpt or ''}".encode()).hexdigest(),
            )
        )
    return existing


def _deployment_state(deployment: ProviderDeployment) -> tuple[str, datetime | None]:
    if not deployment.statuses:
        return "pending", None
    latest = max(deployment.statuses, key=lambda item: item.created_at)
    completed = (
        latest.created_at if latest.state in {"success", "failure", "error", "inactive"} else None
    )
    return latest.state, completed


def _store_deployment(
    session: Session, repository: Repository, deployment: ProviderDeployment
) -> RepositoryDeployment:
    state, completed = _deployment_state(deployment)
    existing = session.scalar(
        select(RepositoryDeployment).where(
            RepositoryDeployment.repository_id == repository.id,
            RepositoryDeployment.workspace_id == repository.workspace_id,
            RepositoryDeployment.provider_deployment_id == deployment.id,
        )
    )
    description = redact_code(deployment.description)[:500] if deployment.description else None
    if existing is None:
        existing = RepositoryDeployment(
            id=uuid4(),
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            provider_deployment_id=deployment.id,
            commit_sha=deployment.sha,
            environment=deployment.environment[:100],
            status=state,
            description=description,
            deployed_at=deployment.created_at,
            completed_at=completed,
        )
        session.add(existing)
    else:
        existing.status = state
        existing.description = description
        existing.completed_at = completed
    return existing


def store_pull_request(
    session: Session, settings: Settings, repository: Repository, pr: ProviderPullRequest
) -> RepositoryPullRequest:
    existing = session.scalar(
        select(RepositoryPullRequest).where(
            RepositoryPullRequest.repository_id == repository.id,
            RepositoryPullRequest.workspace_id == repository.workspace_id,
            RepositoryPullRequest.number == pr.number,
        )
    )
    files = [
        {
            "path": item.path,
            "change_type": item.change_type,
            "additions": item.additions,
            "deletions": item.deletions,
        }
        for item in pr.files[:MAX_COMMIT_FILES]
    ]
    values: dict[str, Any] = {
        "title": redact_code(pr.title)[:500],
        "state": pr.state,
        "is_merged": pr.merged,
        "author_login": pr.author_login,
        "base_branch": pr.base_branch,
        "head_sha": pr.head_sha,
        "changed_files": files,
        "provider_url": pr.url[:500],
        "opened_at": pr.opened_at,
        "closed_at": pr.closed_at,
        "merged_at": pr.merged_at,
    }
    if existing is None:
        existing = RepositoryPullRequest(
            id=uuid4(),
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            number=pr.number,
            **values,
        )
        session.add(existing)
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    return existing


# -- repository sync ------------------------------------------------------------------------
def _lock_key(repository_id: UUID) -> int:
    return repository_id.int & 0x7FFFFFFFFFFFFFFF


def sync_repository(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    repository_id: UUID,
    *,
    shas: list[str] | None = None,
    force_reindex: bool = False,
    index: bool = True,
) -> dict[str, Any]:
    """Refresh metadata, commits, deployments, then (re)build the default-branch snapshot."""
    repository = load_repository(session, workspace_id, repository_id)
    installation = load_installation(session, workspace_id, repository.github_installation_id)
    ensure_provider_access(installation, repository)
    locked = session.scalar(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": _lock_key(repository.id)}
    )
    if not locked:
        return {"status": "already_running"}
    try:
        repository.status = RepositoryStatus.SYNCING.value
        session.commit()
        publish_repository_event(
            settings,
            workspace_id,
            "repository.sync_started",
            {"repository_id": repository.id, "status": "syncing"},
        )
        try:
            result = _sync_repository_body(
                session,
                settings,
                provider,
                workspace_id,
                repository,
                installation,
                shas,
                force_reindex,
                index,
            )
        except Exception as exc:
            session.rollback()
            repository = load_repository(session, workspace_id, repository_id)
            installation = load_installation(
                session, workspace_id, repository.github_installation_id
            )
            _record_rate_limit(installation, provider)
            repository.status = (
                RepositoryStatus.FAILED.value
                if repository.status != RepositoryStatus.REMOVED.value
                else repository.status
            )
            repository.failure_code = getattr(exc, "code", type(exc).__name__.lower())[:80]
            repository.failure_message = sanitize_error(exc)
            session.commit()
            publish_repository_event(
                settings,
                workspace_id,
                "repository.sync_failed",
                {
                    "repository_id": repository.id,
                    "status": "failed",
                    "failure_code": repository.failure_code,
                },
            )
            raise
        else:
            return result
    finally:
        session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _lock_key(repository_id)})
        session.commit()


def _sync_repository_body(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    repository: Repository,
    installation: GitHubInstallation,
    shas: list[str] | None,
    force_reindex: bool,
    index: bool,
) -> dict[str, Any]:
    pid = installation.provider_installation_id
    info = provider.get_repository(pid, repository.owner, repository.name)
    _apply_repository(repository, info, installation)
    # First sync takes the newest bounded page; later syncs overlap the newest stored commit.
    newest = session.scalar(
        select(func.max(RepositoryCommit.committed_at)).where(
            RepositoryCommit.workspace_id == workspace_id,
            RepositoryCommit.repository_id == repository.id,
        )
    )
    since = newest - timedelta(days=1) if newest else None
    listed = provider.list_commits(
        pid,
        repository.owner,
        repository.name,
        repository.default_branch,
        since,
        settings.REPO_MAX_COMMITS_PER_SYNC,
    )
    wanted = [item.sha for item in listed]
    for sha in shas or []:
        if sha not in wanted:
            wanted.append(sha)
    stored = 0
    for sha in wanted[: settings.REPO_MAX_COMMITS_PER_SYNC]:
        known = session.scalar(
            select(RepositoryCommit.changed_files).where(
                RepositoryCommit.workspace_id == workspace_id,
                RepositoryCommit.repository_id == repository.id,
                RepositoryCommit.provider_commit_sha == sha,
            )
        )
        if known:
            continue
        detail = provider.get_commit(pid, repository.owner, repository.name, sha, MAX_COMMIT_FILES)
        _store_commit(session, settings, repository, detail)
        stored += 1
    deployments = provider.list_deployments(pid, repository.owner, repository.name, 30)
    for deployment in deployments:
        _store_deployment(session, repository, deployment)
    for listed_pull in provider.list_pull_requests(
        pid, repository.owner, repository.name, MAX_PULL_REQUESTS
    ):
        pull = provider.get_pull_request(
            pid, repository.owner, repository.name, listed_pull.number, MAX_COMMIT_FILES
        )
        store_pull_request(session, settings, repository, pull)
    _record_rate_limit(installation, provider)
    session.commit()

    snapshot_id: UUID | None = None
    if index:
        branch = provider.get_branch(
            pid, repository.owner, repository.name, repository.default_branch
        )
        snapshot = prepare_snapshot(
            session, workspace_id, repository, branch.head_sha, branch.name, force_reindex
        )
        snapshot_id = snapshot.id
        if snapshot.status != SnapshotStatus.READY.value:
            index_snapshot(session, settings, provider, workspace_id, snapshot.id)
    repository = load_repository(session, workspace_id, repository.id)
    repository.status = (
        RepositoryStatus.ARCHIVED.value if repository.is_archived else RepositoryStatus.ACTIVE.value
    )
    repository.failure_code = None
    repository.failure_message = None
    repository.last_synced_at = utcnow()
    session.commit()
    publish_repository_event(
        settings,
        workspace_id,
        "repository.sync_completed",
        {"repository_id": repository.id, "status": repository.status},
    )
    return {"status": "completed", "commits_stored": stored, "snapshot_id": snapshot_id}


# -- snapshots ------------------------------------------------------------------------------
def prepare_snapshot(
    session: Session,
    workspace_id: UUID,
    repository: Repository,
    commit_sha: str,
    branch: str,
    force: bool,
) -> RepositorySnapshot:
    """Create (or reuse) the unique snapshot for a commit; idempotent across retries."""
    snapshot = session.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.workspace_id == workspace_id,
            RepositorySnapshot.repository_id == repository.id,
            RepositorySnapshot.commit_sha == commit_sha,
            RepositorySnapshot.branch == branch,
        )
    )
    if snapshot is None:
        snapshot = RepositorySnapshot(
            id=uuid4(),
            workspace_id=workspace_id,
            repository_id=repository.id,
            commit_sha=commit_sha,
            branch=branch,
            status=SnapshotStatus.QUEUED.value,
            stats={},
        )
        session.add(snapshot)
    elif snapshot.status == SnapshotStatus.READY.value:
        if force:
            snapshot.status = SnapshotStatus.QUEUED.value
            snapshot.attempt += 1
        elif repository.active_snapshot_id != snapshot.id:
            repository.active_snapshot_id = snapshot.id
    elif snapshot.status in {SnapshotStatus.FAILED.value, SnapshotStatus.CANCELLED.value}:
        snapshot.status = SnapshotStatus.QUEUED.value
        snapshot.attempt += 1
        snapshot.failure_code = None
        snapshot.failure_message = None
        snapshot.failed_at = None
    session.commit()
    return snapshot


def _check_cancelled(session: Session, snapshot: RepositorySnapshot) -> None:
    session.refresh(snapshot)
    if snapshot.status == SnapshotStatus.CANCELLED.value:
        raise IndexingCancelled


def index_snapshot(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    snapshot_id: UUID,
) -> None:
    snapshot = session.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.id == snapshot_id, RepositorySnapshot.workspace_id == workspace_id
        )
    )
    if snapshot is None:
        raise LookupError("snapshot not found in workspace")
    if snapshot.status in {SnapshotStatus.READY.value, SnapshotStatus.CANCELLED.value}:
        return
    repository = load_repository(session, workspace_id, snapshot.repository_id)
    installation = load_installation(session, workspace_id, repository.github_installation_id)
    try:
        ensure_provider_access(installation, repository)
        snapshot.status = SnapshotStatus.INDEXING.value
        snapshot.started_at = snapshot.started_at or utcnow()
        session.commit()
        publish_repository_event(
            settings,
            workspace_id,
            "repository.indexing_started",
            {"repository_id": repository.id, "snapshot_id": snapshot.id, "status": "indexing"},
        )
        _index_snapshot_body(
            session, settings, provider, workspace_id, snapshot, repository, installation
        )
    except IndexingCancelled:
        session.rollback()
        publish_repository_event(
            settings,
            workspace_id,
            "repository.indexing_failed",
            {"repository_id": repository.id, "snapshot_id": snapshot_id, "status": "cancelled"},
        )
    except Exception as exc:
        session.rollback()
        failed = session.scalar(
            select(RepositorySnapshot).where(
                RepositorySnapshot.id == snapshot_id,
                RepositorySnapshot.workspace_id == workspace_id,
            )
        )
        code = getattr(exc, "code", type(exc).__name__.lower())
        if failed is not None and failed.status != SnapshotStatus.CANCELLED.value:
            failed.status = SnapshotStatus.FAILED.value
            failed.failed_at = utcnow()
            failed.failure_code = str(code)[:80]
            failed.failure_message = sanitize_error(exc)
            session.commit()
        publish_repository_event(
            settings,
            workspace_id,
            "repository.indexing_failed",
            {
                "repository_id": repository.id,
                "snapshot_id": snapshot_id,
                "status": "failed",
                "failure_code": str(code),
            },
        )
        raise


def _index_snapshot_body(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    workspace_id: UUID,
    snapshot: RepositorySnapshot,
    repository: Repository,
    installation: GitHubInstallation,
) -> None:
    deadline = time.monotonic() + settings.REPO_INDEX_TIMEOUT_SECONDS
    pid = installation.provider_installation_id
    tree = provider.get_tree(
        pid, repository.owner, repository.name, snapshot.commit_sha, MAX_TREE_ENTRIES
    )
    if tree.truncated:
        raise IndexingFailure("tree_truncated", "repository tree exceeds the supported size")
    skipped: dict[str, int] = {}
    eligible = []
    for entry in sorted(tree.entries, key=lambda item: item.path):
        decision = evaluate_path(entry.path, entry.kind, entry.size, settings.REPO_MAX_FILE_BYTES)
        if decision.include:
            eligible.append((entry, decision.language))
        else:
            skipped[decision.reason] = skipped.get(decision.reason, 0) + 1
    if len(eligible) > settings.REPO_MAX_FILES:
        raise IndexingFailure("too_many_files", "repository exceeds the configured file limit")

    files: list[RepositoryFile] = []
    drafts: list[tuple[RepositoryFile, Any]] = []
    seen_hashes: dict[str, int] = {}
    duplicates = parse_failures = parsed_files = total_bytes = 0
    for number, (entry, language) in enumerate(eligible, start=1):
        if time.monotonic() > deadline:
            raise IndexingFailure("index_timeout", "indexing exceeded the configured time limit")
        if number % 25 == 1:
            _check_cancelled(session, snapshot)
            publish_repository_event(
                settings,
                workspace_id,
                "repository.indexing_progress",
                {
                    "repository_id": repository.id,
                    "snapshot_id": snapshot.id,
                    "status": "indexing",
                    "stage": "reading",
                    "files_processed": number - 1,
                    "total_files": len(eligible),
                },
            )
        ensure_provider_access(installation, repository)
        raw = provider.get_file_content(
            pid,
            repository.owner,
            repository.name,
            entry.path,
            snapshot.commit_sha,
            settings.REPO_MAX_FILE_BYTES,
        )
        total_bytes += len(raw)
        if total_bytes > settings.REPO_MAX_TOTAL_BYTES:
            raise IndexingFailure("max_total_bytes", "repository exceeds the configured byte limit")
        text_content = decode_text(raw)
        if text_content is None:
            skipped["binary"] = skipped.get("binary", 0) + 1
            continue
        if is_generated(text_content):
            skipped["generated"] = skipped.get("generated", 0) + 1
            continue
        redacted = redact_code(text_content)
        result = chunk_file(
            entry.path,
            language,
            redacted,
            settings.REPO_CHUNK_MAX_LINES,
            settings.REPO_CHUNK_OVERLAP_LINES,
        )
        if language == "python":
            parsed_files += 1
            parse_failures += int(result.parse_failed)
        file_row = RepositoryFile(
            id=uuid4(),
            workspace_id=workspace_id,
            repository_id=repository.id,
            snapshot_id=snapshot.id,
            path=entry.path,
            language=language,
            content_hash=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
            line_count=text_content.count("\n") + (0 if text_content.endswith("\n") else 1),
            is_generated=False,
            is_binary=False,
        )
        files.append(file_row)
        for draft in result.chunks:
            if draft.content_hash in seen_hashes:
                duplicates += 1
                continue
            seen_hashes[draft.content_hash] = 1
            drafts.append((file_row, draft))
        if len(drafts) > settings.REPO_MAX_CHUNKS:
            raise IndexingFailure(
                "too_many_chunks", "repository exceeds the configured chunk limit"
            )
    if parsed_files and parse_failures / parsed_files > settings.REPO_MAX_PARSE_FAILURE_RATIO:
        raise IndexingFailure(
            "parse_failure_threshold",
            f"{parse_failures} of {parsed_files} parseable files failed to parse",
        )
    if not drafts:
        raise IndexingFailure("no_indexable_content", "no indexable source files were found")

    _check_cancelled(session, snapshot)
    texts = {
        draft.content_hash: _embedding_text(draft.path, draft.symbol_name, draft.content)
        for _, draft in drafts
    }
    keys = {hash_: hashlib.sha256(value.encode()).hexdigest() for hash_, value in texts.items()}
    cache = _cached_embeddings(session, settings, workspace_id, list(keys.values()))
    missing = [hash_ for hash_, key in keys.items() if key not in cache]
    router = EmbeddingRouter(settings)
    for start in range(0, len(missing), 256):
        if time.monotonic() > deadline:
            raise IndexingFailure("index_timeout", "indexing exceeded the configured time limit")
        batch = missing[start : start + 256]
        vectors = router.embed([texts[hash_] for hash_ in batch])
        for hash_, vector in zip(batch, vectors, strict=True):
            cache[keys[hash_]] = vector
        _check_cancelled(session, snapshot)

    session.execute(
        delete(CodeChunk).where(
            CodeChunk.snapshot_id == snapshot.id, CodeChunk.workspace_id == workspace_id
        )
    )
    session.execute(
        delete(RepositoryFile).where(
            RepositoryFile.snapshot_id == snapshot.id, RepositoryFile.workspace_id == workspace_id
        )
    )
    session.add_all(files)
    session.flush()
    for file_row, draft in drafts:
        session.add(
            CodeChunk(
                id=uuid4(),
                workspace_id=workspace_id,
                repository_id=repository.id,
                snapshot_id=snapshot.id,
                repository_file_id=file_row.id,
                path=draft.path,
                language=draft.language,
                symbol_name=draft.symbol_name,
                symbol_type=draft.symbol_type,
                start_line=draft.start_line,
                end_line=draft.end_line,
                content=draft.content,
                content_hash=draft.content_hash,
                token_count=draft.token_count,
                embedding=cache[keys[draft.content_hash]],
                embedding_provider=settings.EMBEDDING_PROVIDER,
                embedding_model=settings.EMBEDDING_MODEL,
                chunk_metadata={**draft.metadata, "embedding_key": keys[draft.content_hash]},
            )
        )
    now = utcnow()
    snapshot.file_count = len(files)
    snapshot.chunk_count = len(drafts)
    snapshot.stats = {
        "skipped": skipped,
        "parse_failures": parse_failures,
        "duplicate_chunks": duplicates,
        "total_bytes": total_bytes,
        "tree_entries": len(tree.entries),
    }
    snapshot.status = SnapshotStatus.READY.value
    snapshot.completed_at = now
    snapshot.failure_code = None
    snapshot.failure_message = None
    # Atomic activation: ready state, rows, and the active pointer commit together.
    current = (
        session.get(RepositorySnapshot, repository.active_snapshot_id)
        if repository.active_snapshot_id
        else None
    )
    if (
        current is None
        or current.workspace_id != workspace_id
        or current.created_at <= snapshot.created_at
    ):
        repository.active_snapshot_id = snapshot.id
    session.commit()
    prune_snapshots(session, workspace_id, repository.id)
    publish_repository_event(
        settings,
        workspace_id,
        "repository.indexing_completed",
        {
            "repository_id": repository.id,
            "snapshot_id": snapshot.id,
            "status": "ready",
            "files_processed": len(files),
            "chunks_processed": len(drafts),
        },
    )


def _embedding_text(path: str, symbol: str | None, content: str) -> str:
    return f"{path} {symbol or ''}\n{content}"


def _cached_embeddings(
    session: Session, settings: Settings, workspace_id: UUID, keys: list[str]
) -> dict[str, list[float]]:
    if not keys:
        return {}
    key_column = CodeChunk.chunk_metadata["embedding_key"].astext
    rows = session.execute(
        select(key_column, CodeChunk.embedding).where(
            CodeChunk.workspace_id == workspace_id,
            key_column.in_(keys),
            CodeChunk.embedding_provider == settings.EMBEDDING_PROVIDER,
            CodeChunk.embedding_model == settings.EMBEDDING_MODEL,
        )
    )
    return {str(row[0]): list(row[1]) for row in rows}


def prune_snapshots(session: Session, workspace_id: UUID, repository_id: UUID) -> int:
    """Drop heavy rows of old/failed snapshots, keeping the active one and one rollback."""
    repository = load_repository(session, workspace_id, repository_id)
    ready = list(
        session.scalars(
            select(RepositorySnapshot)
            .where(
                RepositorySnapshot.workspace_id == workspace_id,
                RepositorySnapshot.repository_id == repository_id,
                RepositorySnapshot.status == SnapshotStatus.READY.value,
            )
            .order_by(RepositorySnapshot.created_at.desc(), RepositorySnapshot.id)
        )
    )
    keep = {item.id for item in ready[:SNAPSHOTS_TO_KEEP]}
    if repository.active_snapshot_id:
        keep.add(repository.active_snapshot_id)
    stale = [
        item.id
        for item in session.scalars(
            select(RepositorySnapshot).where(
                RepositorySnapshot.workspace_id == workspace_id,
                RepositorySnapshot.repository_id == repository_id,
            )
        )
        if item.id not in keep and item.status != SnapshotStatus.QUEUED.value
    ]
    if not stale:
        return 0
    session.execute(
        delete(CodeChunk).where(
            CodeChunk.workspace_id == workspace_id, CodeChunk.snapshot_id.in_(stale)
        )
    )
    session.execute(
        delete(RepositoryFile).where(
            RepositoryFile.workspace_id == workspace_id, RepositoryFile.snapshot_id.in_(stale)
        )
    )
    session.commit()
    return len(stale)


def cancel_snapshot(session: Session, workspace_id: UUID, snapshot_id: UUID) -> bool:
    snapshot = session.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.id == snapshot_id, RepositorySnapshot.workspace_id == workspace_id
        )
    )
    if snapshot is None or snapshot.status not in {
        SnapshotStatus.QUEUED.value,
        SnapshotStatus.INDEXING.value,
    }:
        return False
    snapshot.status = SnapshotStatus.CANCELLED.value
    session.commit()
    return True


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable


def is_authentication_failure(exc: BaseException) -> bool:
    return isinstance(exc, ProviderAuthenticationError)
