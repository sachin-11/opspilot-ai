"""Deterministic, explainable incident-to-change correlation. No LLM, no causation claims.

Score = sum of documented, bounded signal weights (so 0..1 by construction):

  deployed_before_incident   0.30  successful deploy of the change shortly before the incident
                                   (full credit within 2h, linear decay to 0 at 24h)
  term_overlap               0.30  incident terms (title, description, event types, error
                                   signatures) found in changed paths, commit message, or changed
                                   lines; saturates at 3 distinct terms
  commit_recency             0.15  linear decay over the configured lookback window
  path_prefix_match          0.10  monorepo mapping prefix matches a changed file
  revert_after_incident      0.10  a revert of the change was committed after the incident
  revert_relationship        0.05  the commit itself is a revert/rollback
  environment_match          0.05  deployment environment equals the service environment

Contradicting signals (reverted before the incident, failed deployment, other-environment-only
deployment) never raise a score; a revert before the incident multiplies the score by 0.3.
Changes outside a mapped monorepo prefix, or with a score below ``MIN_SCORE``, are excluded.
"""

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, IngestedEvent, MonitoredService
from opspilot_api.repositories.models import (
    CommitFileChange,
    Repository,
    RepositoryCommit,
    RepositoryDeployment,
    RepositoryStatus,
    ServiceRepositoryMapping,
)

MIN_SCORE = 0.15
REVERT_LOOKAHEAD = timedelta(hours=24)
FULL_CREDIT_MINUTES = 120
DEPLOY_DECAY_MINUTES = 1440
MAX_EVENTS = 100
MAX_COMMITS_PER_REPOSITORY = 300
NOTICE = (
    "Correlation is based on timing, ownership, and changed-code signals. It does not prove "
    "that any listed change caused the incident."
)
_REVERT_SHA = re.compile(r"\b([0-9a-f]{40})\b")
_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "has", "have",
    "not", "but", "its", "into", "when", "after", "before", "all", "any", "new", "use", "add",
    "fix", "update", "change", "changes", "error", "errors", "failure", "failures", "failed",
    "failing", "issue", "incident", "service", "request", "requests", "high", "low", "spike",
    "production", "staging", "development", "alert", "alerts", "increase", "increased", "rate",
    "revert", "reverts", "commit", "src", "lib", "libs", "services", "web", "test", "tests",
    "return", "import", "def", "class", "self", "none", "true", "false", "str", "int",
}  # fmt: skip


def tokenize(text: str) -> set[str]:
    """Lower-cased word stems from identifiers, paths, and prose (deterministic)."""
    tokens: set[str] = set()
    for raw in _SPLIT.split(_CAMEL.sub(r"\1 \2", text)):
        word = raw.lower()
        if len(word) < 3 or word in _STOPWORDS or word.isdigit():
            continue
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 4:
            word = word[:-1]
        tokens.add(word)
    return tokens


def confidence_category(score: float) -> str:
    if score >= 0.6:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


@dataclass
class ChangeCandidate:
    kind: str
    repository_id: UUID
    repository_full_name: str
    commit_sha: str
    commit_id: UUID | None
    deployment_id: UUID | None
    title: str
    score: float
    matched_signals: list[str]
    contradicting_signals: list[str]
    minutes_before_incident: int
    changed_files: list[str]
    environment: str | None
    committed_at: datetime | None
    signal_scores: dict[str, float] = field(default_factory=dict)

    @property
    def confidence(self) -> str:
        return confidence_category(self.score)

    @property
    def citation_label(self) -> str:
        short = self.commit_sha[:7]
        if self.kind == "deployment":
            return f"[github.com/{self.repository_full_name}@{short}: deployment {self.title}]"
        return f"[github.com/{self.repository_full_name}@{short}: commit {self.title[:80]}]"


@dataclass
class CorrelationResult:
    incident_id: UUID
    opened_at: datetime
    service_id: UUID | None
    has_repository_mapping: bool
    candidates: list[ChangeCandidate]
    deployments: list[RepositoryDeployment]
    repositories: dict[UUID, Repository]


@dataclass
class _Mapping:
    repository: Repository
    path_prefix: str | None


def _deployment_credit(minutes: float) -> float:
    if minutes <= FULL_CREDIT_MINUTES:
        return 1.0
    return max(
        0.0, 1.0 - (minutes - FULL_CREDIT_MINUTES) / (DEPLOY_DECAY_MINUTES - FULL_CREDIT_MINUTES)
    )


def _reverted_sha(commit: RepositoryCommit) -> str | None:
    if not commit.message.lower().startswith("revert"):
        return None
    match = _REVERT_SHA.search(commit.message)
    return match.group(1) if match else None


def mapped_repositories(
    session: Session, workspace_id: UUID, service: MonitoredService
) -> list[_Mapping]:
    rows = session.execute(
        select(ServiceRepositoryMapping, Repository)
        .join(
            Repository,
            (Repository.id == ServiceRepositoryMapping.repository_id)
            & (Repository.workspace_id == workspace_id),
        )
        .where(
            ServiceRepositoryMapping.workspace_id == workspace_id,
            ServiceRepositoryMapping.service_id == service.id,
            ServiceRepositoryMapping.environment == service.environment,
            Repository.status.notin_(
                [RepositoryStatus.REMOVED.value, RepositoryStatus.ARCHIVED.value]
            ),
        )
        .order_by(Repository.full_name, ServiceRepositoryMapping.path_prefix_key)
    ).all()
    # The first integration tracks the default branch only, so a mapping whose branch pattern
    # cannot match it contributes no changes.
    return [
        _Mapping(repository, mapping.path_prefix)
        for mapping, repository in rows
        if fnmatch.fnmatch(repository.default_branch, mapping.branch_pattern)
    ]


def _incident_terms(
    session: Session, workspace_id: UUID, incident: Incident, service: MonitoredService
) -> set[str]:
    parts = [incident.title, incident.description or ""]
    events = session.scalars(
        select(IngestedEvent)
        .where(
            IngestedEvent.workspace_id == workspace_id,
            IngestedEvent.incident_id == incident.id,
        )
        .order_by(IngestedEvent.occurred_at)
        .limit(MAX_EVENTS)
    )
    for event in events:
        parts.extend([event.event_type, event.error_signature or "", event.summary])
    terms = tokenize(" ".join(parts))
    # The service's own name appears in every one of its paths and carries no signal.
    return terms - tokenize(f"{service.name} {service.slug}")


def rank_suspect_changes(
    session: Session,
    settings: Settings,
    workspace_id: UUID,
    incident_id: UUID,
    limit: int = 5,
) -> CorrelationResult:
    incident = session.scalar(
        select(Incident).where(Incident.id == incident_id, Incident.workspace_id == workspace_id)
    )
    if incident is None:
        raise LookupError("incident not found in workspace")
    service = (
        session.scalar(
            select(MonitoredService).where(
                MonitoredService.id == incident.service_id,
                MonitoredService.workspace_id == workspace_id,
            )
        )
        if incident.service_id
        else None
    )
    empty = CorrelationResult(
        incident.id, incident.opened_at, incident.service_id, False, [], [], {}
    )
    if service is None:
        return empty
    mappings = mapped_repositories(session, workspace_id, service)
    if not mappings:
        return empty

    opened = incident.opened_at
    window_start = opened - timedelta(hours=settings.REPO_CORRELATION_LOOKBACK_HOURS)
    terms = _incident_terms(session, workspace_id, incident, service)
    needed = max(1, min(3, len(terms)))
    candidates: list[ChangeCandidate] = []
    deployments_out: list[RepositoryDeployment] = []
    repositories: dict[UUID, Repository] = {}

    by_repository: dict[UUID, list[_Mapping]] = {}
    for mapping in mappings:
        by_repository.setdefault(mapping.repository.id, []).append(mapping)

    for repository_id, repo_mappings in by_repository.items():
        repository = repo_mappings[0].repository
        repositories[repository.id] = repository
        commits = list(
            session.scalars(
                select(RepositoryCommit)
                .where(
                    RepositoryCommit.workspace_id == workspace_id,
                    RepositoryCommit.repository_id == repository_id,
                    RepositoryCommit.committed_at >= window_start,
                    RepositoryCommit.committed_at <= opened + REVERT_LOOKAHEAD,
                )
                .order_by(
                    RepositoryCommit.committed_at.desc(), RepositoryCommit.provider_commit_sha
                )
                .limit(MAX_COMMITS_PER_REPOSITORY)
            )
        )
        deployments = list(
            session.scalars(
                select(RepositoryDeployment)
                .where(
                    RepositoryDeployment.workspace_id == workspace_id,
                    RepositoryDeployment.repository_id == repository_id,
                    RepositoryDeployment.deployed_at >= window_start,
                    RepositoryDeployment.deployed_at <= opened,
                )
                .order_by(RepositoryDeployment.deployed_at.desc(), RepositoryDeployment.id)
            )
        )
        deployments_out.extend(
            item for item in deployments if item.environment.lower() == service.environment.lower()
        )
        changes: dict[UUID, list[CommitFileChange]] = {}
        commit_ids = [item.id for item in commits]
        for change in session.scalars(
            select(CommitFileChange).where(
                CommitFileChange.workspace_id == workspace_id,
                CommitFileChange.repository_id == repository_id,
                CommitFileChange.commit_id.in_(commit_ids),
            )
        ):
            changes.setdefault(change.commit_id, []).append(change)
        reverted_by: dict[str, RepositoryCommit] = {}
        for commit in commits:
            target = _reverted_sha(commit)
            if target:
                reverted_by.setdefault(target, commit)
        deployed_shas = {item.commit_sha for item in deployments}

        for commit in commits:
            if commit.committed_at > opened:
                continue  # post-incident commits are only used to detect later reverts
            files = sorted(changes.get(commit.id, []), key=lambda item: item.path)
            paths = [item.path for item in files]
            candidate = _score_commit(
                repository,
                repo_mappings,
                commit,
                files,
                paths,
                deployments,
                reverted_by,
                terms,
                needed,
                service,
                opened,
                settings,
            )
            if candidate is not None:
                candidates.append(candidate)
        known_commits = {item.provider_commit_sha for item in commits}
        for deployment in deployments:
            if deployment.commit_sha in known_commits or deployment.commit_sha not in deployed_shas:
                continue
            candidate = _score_orphan_deployment(repository, deployment, service, opened)
            if candidate is not None:
                candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -item.score,
            item.minutes_before_incident,
            item.repository_full_name,
            item.commit_sha,
        )
    )
    return CorrelationResult(
        incident.id,
        opened,
        service.id,
        True,
        candidates[:limit],
        sorted(deployments_out, key=lambda item: item.deployed_at or opened, reverse=True),
        repositories,
    )


def _score_commit(
    repository: Repository,
    mappings: list[_Mapping],
    commit: RepositoryCommit,
    files: list[CommitFileChange],
    paths: list[str],
    deployments: list[RepositoryDeployment],
    reverted_by: dict[str, RepositoryCommit],
    terms: set[str],
    needed: int,
    service: MonitoredService,
    opened: datetime,
    settings: Settings,
) -> ChangeCandidate | None:
    matched: list[str] = []
    contradicting: list[str] = []
    scores: dict[str, float] = {}

    prefix_match = False
    prefixes = [item.path_prefix for item in mappings if item.path_prefix]
    if prefixes and len(prefixes) == len(mappings):
        prefix_match = any(path.startswith(prefix) for path in paths for prefix in prefixes)
        if not prefix_match:
            return None  # outside every mapped monorepo path
    elif prefixes:
        prefix_match = any(path.startswith(prefix) for path in paths for prefix in prefixes)
    if prefix_match:
        scores["path_prefix_match"] = 0.10
        matched.append("changed files are under the mapped monorepo path prefix")

    hours = max(0.0, (opened - commit.committed_at).total_seconds() / 3600)
    recency = max(0.0, 1.0 - hours / settings.REPO_CORRELATION_LOOKBACK_HOURS)
    if recency > 0:
        scores["commit_recency"] = round(0.15 * recency, 4)
        matched.append(f"committed {int(hours * 60)} minutes before the incident")
    minutes = int(hours * 60)

    own = [item for item in deployments if item.commit_sha == commit.provider_commit_sha]
    environment: str | None = None
    if own:
        wanted = service.environment.lower()
        in_env = [item for item in own if item.environment.lower() == wanted]
        succeeded = [item for item in in_env if item.status == "success"]
        if succeeded:
            deployment = max(succeeded, key=lambda item: item.deployed_at or opened)
            deployed_minutes = max(
                0, int(((opened - (deployment.deployed_at or opened)).total_seconds()) / 60)
            )
            credit = _deployment_credit(deployed_minutes)
            if credit > 0:
                scores["deployed_before_incident"] = round(0.30 * credit, 4)
                matched.append(
                    f"deployed to {deployment.environment} {deployed_minutes} minutes "
                    "before the incident"
                )
            scores["environment_match"] = 0.05
            matched.append("deployment environment matches the service environment")
            environment = deployment.environment
            minutes = min(minutes, deployed_minutes)
        elif in_env:
            contradicting.append(f"deployment to {in_env[0].environment} did not succeed")
            environment = in_env[0].environment
        else:
            contradicting.append(
                "only deployed to other environments: "
                + ", ".join(sorted({item.environment for item in own}))
            )
            environment = own[0].environment

    candidate_terms = tokenize(commit.message)
    for item in files:
        candidate_terms |= tokenize(item.path)
        if item.previous_path:
            candidate_terms |= tokenize(item.previous_path)
        if item.patch_excerpt:
            changed_lines = [
                line[1:]
                for line in item.patch_excerpt.splitlines()
                if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
            ]
            candidate_terms |= tokenize("\n".join(changed_lines))
    overlap = sorted(terms & candidate_terms)
    if overlap:
        scores["term_overlap"] = round(0.30 * min(1.0, len(overlap) / needed), 4)
        matched.append("incident terms found in the change: " + ", ".join(overlap[:6]))

    revert = reverted_by.get(commit.provider_commit_sha)
    penalty = 1.0
    if revert is not None:
        if revert.committed_at <= opened:
            penalty = 0.3
            contradicting.append(
                f"reverted by {revert.provider_commit_sha[:7]} before the incident"
            )
        else:
            scores["revert_after_incident"] = 0.10
            matched.append(
                f"reverted by {revert.provider_commit_sha[:7]} after the incident was opened"
            )
    target = _reverted_sha(commit)
    if target:
        scores["revert_relationship"] = 0.05
        matched.append(f"is a revert/rollback of {target[:7]}")

    total = round(min(1.0, sum(scores.values())) * penalty, 4)
    if total < MIN_SCORE:
        return None
    first_line = commit.message.splitlines()[0] if commit.message else commit.provider_commit_sha
    return ChangeCandidate(
        kind="commit",
        repository_id=repository.id,
        repository_full_name=repository.full_name,
        commit_sha=commit.provider_commit_sha,
        commit_id=commit.id,
        deployment_id=None,
        title=first_line,
        score=total,
        matched_signals=matched,
        contradicting_signals=contradicting,
        minutes_before_incident=minutes,
        changed_files=paths[:20],
        environment=environment,
        committed_at=commit.committed_at,
        signal_scores=scores,
    )


def _score_orphan_deployment(
    repository: Repository,
    deployment: RepositoryDeployment,
    service: MonitoredService,
    opened: datetime,
) -> ChangeCandidate | None:
    if (
        deployment.environment.lower() != service.environment.lower()
        or deployment.status != "success"
    ):
        return None
    minutes = max(0, int((opened - (deployment.deployed_at or opened)).total_seconds() / 60))
    credit = _deployment_credit(minutes)
    score = round(0.30 * credit + 0.05, 4) if credit > 0 else 0.0
    if score < MIN_SCORE:
        return None
    return ChangeCandidate(
        kind="deployment",
        repository_id=repository.id,
        repository_full_name=repository.full_name,
        commit_sha=deployment.commit_sha,
        commit_id=None,
        deployment_id=deployment.id,
        title=f"#{deployment.provider_deployment_id} {deployment.environment}",
        score=score,
        matched_signals=[
            f"deployed to {deployment.environment} {minutes} minutes before the incident",
            "deployment environment matches the service environment",
        ],
        contradicting_signals=["deployed commit metadata is not synchronized"],
        minutes_before_incident=minutes,
        changed_files=[],
        environment=deployment.environment,
        committed_at=None,
        signal_scores={
            "deployed_before_incident": round(0.30 * credit, 4),
            "environment_match": 0.05,
        },
    )


def candidate_summary(candidate: ChangeCandidate) -> dict[str, Any]:
    return {
        "kind": candidate.kind,
        "repository": candidate.repository_full_name,
        "commit_sha": candidate.commit_sha,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "matched_signals": candidate.matched_signals,
        "contradicting_signals": candidate.contradicting_signals,
        "minutes_before_incident": candidate.minutes_before_incident,
        "changed_files": candidate.changed_files[:10],
    }
