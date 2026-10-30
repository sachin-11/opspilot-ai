"""Read-only LangGraph tools for code-change evidence.

Workspace and incident scope come only from the trusted ``ToolContext``; the model supplies
bounded, validated arguments (never URLs, raw revisions, or SQL). Tools read the synchronized
database state, call no provider, and cannot mutate GitHub. Retrieved text is untrusted evidence.
"""

import logging
import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot_api.incidents.models import Incident, MonitoredService
from opspilot_api.investigations.tools import EvidenceDraft, ToolContext, ToolResult
from opspilot_api.knowledge.embeddings import EmbeddingError
from opspilot_api.repositories.correlation import (
    NOTICE,
    ChangeCandidate,
    candidate_summary,
    mapped_repositories,
)
from opspilot_api.repositories.correlation import (
    rank_suspect_changes as correlate,
)
from opspilot_api.repositories.models import (
    CommitFileChange,
    Repository,
    RepositoryCommit,
    RepositoryDeployment,
    RepositoryPullRequest,
)
from opspilot_api.repositories.policy import redact_code
from opspilot_api.repositories.retrieval import CodeRetrievalService, get_code_chunk
from opspilot_api.repositories.schemas import CodeSearchRequest

logger = logging.getLogger(__name__)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
EVIDENCE_EXCERPT_CHARS = 1800
EVIDENCE_NOTE = "Untrusted repository data; correlation only, not confirmed causation."


class RecentChangesInput(BaseModel):
    hours: int = Field(default=24, ge=1, le=168)
    limit: int = Field(default=10, ge=1, le=25)


class RankInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=10)


class CommitInput(BaseModel):
    commit_sha: str = Field(min_length=7, max_length=40)
    max_files: int = Field(default=5, ge=1, le=10)


class CodeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=10)
    language: str | None = Field(default=None, max_length=40)
    path_prefix: str | None = Field(default=None, max_length=500)
    repository_id: UUID | None = None


class CodeExcerptInput(BaseModel):
    chunk_id: UUID


class PullRequestInput(BaseModel):
    number: int = Field(ge=1, le=10_000_000)
    repository_id: UUID | None = None


class _Scope:
    def __init__(
        self, incident: Incident, service: MonitoredService, repositories: list[Repository]
    ):
        self.incident = incident
        self.service = service
        self.repositories = repositories
        self.ids = {item.id for item in repositories}


def _unavailable(tool: str, summary: str, **data: Any) -> ToolResult:
    return ToolResult(
        tool_name=tool,
        summary=summary,
        evidence=[],
        data={"evidence_count": 0, "unavailable": True, **data},
    )


def _no_mapping(tool: str) -> ToolResult:
    return ToolResult(
        tool_name=tool,
        summary="No repository is mapped to the affected service; no code evidence collected.",
        evidence=[],
        data={"evidence_count": 0, "has_repository_mapping": False},
    )


def _scope(session: Session, context: ToolContext) -> _Scope | None:
    incident = session.scalar(
        select(Incident).where(
            Incident.id == context.incident_id, Incident.workspace_id == context.workspace_id
        )
    )
    if incident is None:
        raise LookupError("incident not found in workspace")
    if incident.service_id is None:
        return None
    service = session.scalar(
        select(MonitoredService).where(
            MonitoredService.id == incident.service_id,
            MonitoredService.workspace_id == context.workspace_id,
        )
    )
    if service is None:
        return None
    mappings = mapped_repositories(session, context.workspace_id, service)
    unique: dict[UUID, Repository] = {item.repository.id: item.repository for item in mappings}
    if not unique:
        return None
    return _Scope(incident, service, list(unique.values()))


def _freshness(scope: _Scope) -> list[dict[str, Any]]:
    return [
        {
            "repository": item.full_name,
            "status": item.status,
            "last_synced_at": item.last_synced_at.isoformat() if item.last_synced_at else None,
            "failure_code": item.failure_code,
        }
        for item in scope.repositories
    ]


def _guard(tool: str, session: Session, run: Any) -> ToolResult:
    """Provider/database failures become explicit unavailable observations, never evidence."""
    try:
        result: ToolResult = run()
        return result
    except (SQLAlchemyError, EmbeddingError) as exc:
        session.rollback()
        logger.warning("code tool unavailable", extra={"error_class": type(exc).__name__})
        return _unavailable(tool, "Repository data was unavailable; the investigation continues.")
    except (LookupError, ValueError) as exc:
        # Rejected arguments (unknown/ambiguous revision, unmapped repository) are observations,
        # not fabricated evidence and not a reason to fail the investigation.
        return _unavailable(tool, f"Tool request rejected: {type(exc).__name__}")


def _deployment_evidence(item: RepositoryDeployment, repository: Repository) -> EvidenceDraft:
    label = (
        f"[github.com/{repository.full_name}@{item.commit_sha[:7]}: "
        f"deployment #{item.provider_deployment_id} {item.environment}]"
    )
    when = item.deployed_at.isoformat() if item.deployed_at else "unknown time"
    return EvidenceDraft(
        evidence_type="change",
        source_type="deployment",
        source_id=str(item.id),
        title=label,
        excerpt=(
            f"Deployment #{item.provider_deployment_id} of commit {item.commit_sha[:7]} to "
            f"{item.environment} at {when}; status {item.status}. {EVIDENCE_NOTE}"
        ),
        metadata={
            "citation_label": label,
            "repository": repository.full_name,
            "commit_sha": item.commit_sha,
            "environment": item.environment,
            "status": item.status,
            "untrusted": True,
        },
        collected_at=item.deployed_at or item.created_at,
    )


def _commit_evidence(
    commit: RepositoryCommit, repository: Repository, files: list[CommitFileChange]
) -> EvidenceDraft:
    first = commit.message.splitlines()[0] if commit.message else ""
    label = (
        f"[github.com/{repository.full_name}@{commit.provider_commit_sha[:7]}: commit {first[:80]}]"
    )
    summary = "; ".join(
        f"{item.change_type} {item.path} (+{item.additions}/-{item.deletions})"
        for item in files[:10]
    )
    return EvidenceDraft(
        evidence_type="change",
        source_type="repository_commit",
        source_id=str(commit.id),
        title=label,
        excerpt=redact_code(f"{first}. Files: {summary}. {EVIDENCE_NOTE}")[:EVIDENCE_EXCERPT_CHARS],
        metadata={
            "citation_label": label,
            "repository": repository.full_name,
            "commit_sha": commit.provider_commit_sha,
            "committed_at": commit.committed_at.isoformat(),
            "untrusted": True,
        },
        collected_at=commit.committed_at,
    )


def _diff_evidence(
    commit: RepositoryCommit,
    repository: Repository,
    files: list[CommitFileChange],
    max_files: int,
) -> EvidenceDraft:
    label = f"[github.com/{repository.full_name}@{commit.provider_commit_sha[:7]}: diff]"
    parts: list[str] = []
    truncated = False
    for item in files[:max_files]:
        patch = redact_code(item.patch_excerpt or "(no patch available)")
        parts.append(f"--- {item.path}\n{patch[:600]}")
        truncated = truncated or item.patch_truncated or len(patch) > 600
    excerpt = "\n".join(parts)
    return EvidenceDraft(
        evidence_type="change",
        source_type="commit_diff",
        source_id=str(commit.id),
        title=label,
        excerpt=excerpt[:EVIDENCE_EXCERPT_CHARS],
        metadata={
            "citation_label": label,
            "repository": repository.full_name,
            "commit_sha": commit.provider_commit_sha,
            "files": [item.path for item in files[:max_files]],
            "truncated": truncated or len(excerpt) > EVIDENCE_EXCERPT_CHARS,
            "untrusted": True,
        },
        collected_at=commit.committed_at,
    )


def _candidate_evidence(candidate: ChangeCandidate) -> EvidenceDraft:
    excerpt = (
        f"Correlation score {candidate.score:.2f} ({candidate.confidence}), "
        f"{candidate.minutes_before_incident} minutes before the incident. "
        f"Matched: {'; '.join(candidate.matched_signals) or 'none'}. "
        f"Contradicting: {'; '.join(candidate.contradicting_signals) or 'none'}. "
        f"Files: {', '.join(candidate.changed_files[:8]) or 'n/a'}. {NOTICE}"
    )
    return EvidenceDraft(
        evidence_type="change",
        source_type="deployment" if candidate.kind == "deployment" else "repository_commit",
        source_id=str(candidate.deployment_id or candidate.commit_id),
        title=candidate.citation_label,
        excerpt=redact_code(excerpt)[:EVIDENCE_EXCERPT_CHARS],
        metadata={
            "citation_label": candidate.citation_label,
            "repository": candidate.repository_full_name,
            "commit_sha": candidate.commit_sha,
            "correlation_score": candidate.score,
            "confidence": candidate.confidence,
            "matched_signals": candidate.matched_signals,
            "contradicting_signals": candidate.contradicting_signals,
            "untrusted": True,
        },
        collected_at=candidate.committed_at or _epoch(),
    )


def _epoch() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def list_recent_changes(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = RecentChangesInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("list_recent_changes")
        opened = scope.incident.opened_at
        start = opened - timedelta(hours=params.hours)
        commits = list(
            session.execute(
                select(RepositoryCommit, Repository)
                .join(
                    Repository,
                    (Repository.id == RepositoryCommit.repository_id)
                    & (Repository.workspace_id == context.workspace_id),
                )
                .where(
                    RepositoryCommit.workspace_id == context.workspace_id,
                    RepositoryCommit.repository_id.in_(scope.ids),
                    RepositoryCommit.committed_at >= start,
                    RepositoryCommit.committed_at <= opened,
                )
                .order_by(
                    RepositoryCommit.committed_at.desc(), RepositoryCommit.provider_commit_sha
                )
                .limit(params.limit)
            )
        )
        deployments = list(
            session.execute(
                select(RepositoryDeployment, Repository)
                .join(
                    Repository,
                    (Repository.id == RepositoryDeployment.repository_id)
                    & (Repository.workspace_id == context.workspace_id),
                )
                .where(
                    RepositoryDeployment.workspace_id == context.workspace_id,
                    RepositoryDeployment.repository_id.in_(scope.ids),
                    RepositoryDeployment.deployed_at >= start,
                    RepositoryDeployment.deployed_at <= opened,
                )
                .order_by(RepositoryDeployment.deployed_at.desc(), RepositoryDeployment.id)
                .limit(params.limit)
            )
        )
        evidence = [_deployment_evidence(item, repository) for item, repository in deployments[:3]]
        return ToolResult(
            tool_name="list_recent_changes",
            summary=(
                f"Found {len(commits)} commits and {len(deployments)} deployments in the window."
            ),
            evidence=evidence,
            data={
                "has_repository_mapping": True,
                "evidence_count": len(evidence),
                "commits": [
                    {
                        "sha": item.provider_commit_sha,
                        "repository": repository.full_name,
                        "message": redact_code(
                            item.message.splitlines()[0] if item.message else ""
                        ),
                        "committed_at": item.committed_at.isoformat(),
                    }
                    for item, repository in commits
                ],
                "deployments": [
                    {
                        "id": item.provider_deployment_id,
                        "repository": repository.full_name,
                        "commit_sha": item.commit_sha,
                        "environment": item.environment,
                        "status": item.status,
                    }
                    for item, repository in deployments
                ],
                "repositories": _freshness(scope),
            },
        )

    return _guard("list_recent_changes", session, run)


def rank_suspect_changes(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = RankInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("rank_suspect_changes")
        result = correlate(
            session, context.settings, context.workspace_id, context.incident_id, params.limit
        )
        evidence: list[EvidenceDraft] = []
        for candidate in result.candidates[:3]:
            evidence.append(_candidate_evidence(candidate))
        for candidate in result.candidates[:2]:
            if candidate.commit_id is None:
                continue
            commit = session.scalar(
                select(RepositoryCommit).where(
                    RepositoryCommit.id == candidate.commit_id,
                    RepositoryCommit.workspace_id == context.workspace_id,
                )
            )
            repository = result.repositories.get(candidate.repository_id)
            if commit is None or repository is None:
                continue
            files = list(
                session.scalars(
                    select(CommitFileChange)
                    .where(
                        CommitFileChange.workspace_id == context.workspace_id,
                        CommitFileChange.commit_id == commit.id,
                    )
                    .order_by(CommitFileChange.path)
                )
            )
            evidence.append(_diff_evidence(commit, repository, files, 3))
        summary = (
            f"Ranked {len(result.candidates)} suspect changes (correlation, not causation)."
            if result.candidates
            else "No relevant recent code change was found for the mapped repositories."
        )
        return ToolResult(
            tool_name="rank_suspect_changes",
            summary=summary,
            evidence=evidence,
            data={
                "has_repository_mapping": True,
                "evidence_count": len(evidence),
                "candidates": [candidate_summary(item) for item in result.candidates],
                "notice": NOTICE,
                "repositories": _freshness(scope),
            },
        )

    return _guard("rank_suspect_changes", session, run)


def _resolve_commit(
    session: Session, context: ToolContext, scope: _Scope, sha: str
) -> tuple[RepositoryCommit, Repository]:
    sha = sha.lower()
    if not _SHA.match(sha):
        raise ValueError("commit_sha must be a 7-40 character hex revision")
    rows = list(
        session.execute(
            select(RepositoryCommit, Repository)
            .join(
                Repository,
                (Repository.id == RepositoryCommit.repository_id)
                & (Repository.workspace_id == context.workspace_id),
            )
            .where(
                RepositoryCommit.workspace_id == context.workspace_id,
                RepositoryCommit.repository_id.in_(scope.ids),
                RepositoryCommit.provider_commit_sha.startswith(sha, autoescape=True),
            )
            .limit(2)
        )
    )
    if not rows:
        raise LookupError("commit not found in the repositories mapped to this service")
    if len(rows) > 1:
        raise ValueError("commit_sha is ambiguous; provide more characters")
    return rows[0][0], rows[0][1]


def _commit_files(
    session: Session, context: ToolContext, commit: RepositoryCommit
) -> list[CommitFileChange]:
    return list(
        session.scalars(
            select(CommitFileChange)
            .where(
                CommitFileChange.workspace_id == context.workspace_id,
                CommitFileChange.commit_id == commit.id,
            )
            .order_by(CommitFileChange.path)
        )
    )


def get_commit_summary(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = CommitInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("get_commit_summary")
        commit, repository = _resolve_commit(session, context, scope, params.commit_sha)
        files = _commit_files(session, context, commit)
        return ToolResult(
            tool_name="get_commit_summary",
            summary="Loaded one mapped commit summary.",
            evidence=[_commit_evidence(commit, repository, files)],
            data={
                "evidence_count": 1,
                "commit_sha": commit.provider_commit_sha,
                "files": [
                    {"path": item.path, "change_type": item.change_type} for item in files[:20]
                ],
            },
        )

    return _guard("get_commit_summary", session, run)


def get_commit_diff(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = CommitInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("get_commit_diff")
        commit, repository = _resolve_commit(session, context, scope, params.commit_sha)
        files = _commit_files(session, context, commit)
        evidence = _diff_evidence(commit, repository, files, params.max_files)
        return ToolResult(
            tool_name="get_commit_diff",
            summary="Loaded bounded, redacted patch excerpts for one mapped commit.",
            evidence=[evidence],
            data={"evidence_count": 1, "commit_sha": commit.provider_commit_sha},
        )

    return _guard("get_commit_diff", session, run)


def search_repository_code(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    def run() -> ToolResult:
        params = CodeSearchInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("search_repository_code")
        if params.repository_id is not None and params.repository_id not in scope.ids:
            raise ValueError("repository is not mapped to the affected service")
        request = CodeSearchRequest(
            query=params.query,
            service_id=scope.service.id,
            repository_id=params.repository_id,
            language=params.language,
            path_prefix=params.path_prefix,
            candidate_count=max(5, params.top_k * 4),
            top_k=params.top_k,
        )
        response = CodeRetrievalService(context.settings).search(
            session, context.workspace_id, request, context.investigation_id
        )
        evidence = [
            EvidenceDraft(
                evidence_type="code",
                source_type="code_chunk",
                source_id=str(item.chunk_id),
                title=item.citation_label,
                excerpt=redact_code(item.excerpt)[:EVIDENCE_EXCERPT_CHARS],
                metadata={
                    "citation_label": item.citation_label,
                    "repository": item.repository_full_name,
                    "commit_sha": item.commit_sha,
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "rrf_score": item.rrf_score,
                    "untrusted": True,
                },
                collected_at=_epoch(),
            )
            for item in response.results
        ]
        return ToolResult(
            tool_name="search_repository_code",
            summary=(
                f"Found {len(evidence)} relevant code excerpts."
                if evidence
                else "No sufficiently relevant code was found."
            ),
            evidence=evidence,
            data={
                "evidence_count": len(evidence),
                "low_relevance": response.diagnostics.low_relevance,
                "results": [
                    {"chunk_id": str(item.chunk_id), "citation_label": item.citation_label}
                    for item in response.results
                ],
            },
        )

    return _guard("search_repository_code", session, run)


def get_code_excerpt(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = CodeExcerptInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("get_code_excerpt")
        item = get_code_chunk(session, context.workspace_id, params.chunk_id, scope.ids)
        evidence = EvidenceDraft(
            evidence_type="code",
            source_type="code_chunk",
            source_id=str(item.chunk_id),
            title=item.citation_label,
            excerpt=redact_code(item.excerpt)[:EVIDENCE_EXCERPT_CHARS],
            metadata={
                "citation_label": item.citation_label,
                "repository": item.repository_full_name,
                "commit_sha": item.commit_sha,
                "path": item.path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "untrusted": True,
            },
            collected_at=_epoch(),
        )
        return ToolResult(
            tool_name="get_code_excerpt",
            summary="Loaded one workspace-scoped code excerpt.",
            evidence=[evidence],
            data={"evidence_count": 1, "chunk_id": str(item.chunk_id)},
        )

    return _guard("get_code_excerpt", session, run)


def get_pull_request_context(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    def run() -> ToolResult:
        params = PullRequestInput.model_validate(raw)
        scope = _scope(session, context)
        if scope is None:
            return _no_mapping("get_pull_request_context")
        if params.repository_id is not None and params.repository_id not in scope.ids:
            raise ValueError("repository is not mapped to the affected service")
        rows = list(
            session.execute(
                select(RepositoryPullRequest, Repository)
                .join(
                    Repository,
                    (Repository.id == RepositoryPullRequest.repository_id)
                    & (Repository.workspace_id == context.workspace_id),
                )
                .where(
                    RepositoryPullRequest.workspace_id == context.workspace_id,
                    RepositoryPullRequest.repository_id.in_(
                        {params.repository_id} if params.repository_id else scope.ids
                    ),
                    RepositoryPullRequest.number == params.number,
                )
                .limit(2)
            )
        )
        if not rows:
            raise LookupError("pull request not found in the repositories mapped to this service")
        if len(rows) > 1:
            raise ValueError("pull request number is ambiguous; provide repository_id")
        pull, repository = rows[0]
        label = (
            f"[github.com/{repository.full_name}@{pull.head_sha[:7]}: pull request #{pull.number}]"
        )
        files = ", ".join(str(item.get("path")) for item in pull.changed_files[:10])
        evidence = EvidenceDraft(
            evidence_type="change",
            source_type="pull_request",
            source_id=str(pull.id),
            title=label,
            excerpt=redact_code(
                f"PR #{pull.number} {pull.title} ({pull.state}, merged={pull.is_merged}). "
                f"Files: {files}. {EVIDENCE_NOTE}"
            )[:EVIDENCE_EXCERPT_CHARS],
            metadata={
                "citation_label": label,
                "repository": repository.full_name,
                "number": pull.number,
                "untrusted": True,
            },
            collected_at=pull.merged_at or pull.opened_at,
        )
        return ToolResult(
            tool_name="get_pull_request_context",
            summary="Loaded one mapped pull request.",
            evidence=[evidence],
            data={"evidence_count": 1, "number": pull.number},
        )

    return _guard("get_pull_request_context", session, run)


CODE_TOOLS = {
    "list_recent_changes": list_recent_changes,
    "rank_suspect_changes": rank_suspect_changes,
    "get_commit_summary": get_commit_summary,
    "get_commit_diff": get_commit_diff,
    "search_repository_code": search_repository_code,
    "get_code_excerpt": get_code_excerpt,
    "get_pull_request_context": get_pull_request_context,
}
