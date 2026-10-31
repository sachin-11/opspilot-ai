"""Workspace-scoped, transactional remediation application services (async, API side)."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.context import Actor, ForbiddenError
from opspilot_api.auth.permissions import Permission
from opspilot_api.auth.service import active_membership
from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, MonitoredService
from opspilot_api.incidents.service import (
    ConflictError,
    DomainError,
    NotFoundError,
    require_workspace,
    utcnow,
)
from opspilot_api.investigations.models import (
    EvidenceItem,
    Investigation,
    InvestigationReview,
    InvestigationStatus,
    ReviewAction,
)
from opspilot_api.remediation.approval import ApprovalContext
from opspilot_api.remediation.audit import emit_event_async, record_audit
from opspilot_api.remediation.binding import (
    check_binding,
    current_binding,
    current_revision,
    latest_approval,
    latest_run,
)
from opspilot_api.remediation.generator import PROMPT_VERSION
from opspilot_api.remediation.lifecycle import transition
from opspilot_api.remediation.models import (
    ACTIVE_STATUSES,
    ApprovalAction,
    FixPullRequest,
    PolicyDecision,
    PolicyEvaluation,
    RemediationApproval,
    RemediationAuditEvent,
    RemediationProposal,
    RemediationRevision,
    RemediationStatus,
    RemediationStep,
    RiskLevel,
    SandboxRun,
    VerificationCheck,
)
from opspilot_api.remediation.policy import POLICY_VERSION
from opspilot_api.remediation.schemas import (
    ApprovalBindingResponse,
    ApprovalResponse,
    CheckResponse,
    PolicyEvaluationResponse,
    PullRequestResponse,
    RemediationDetail,
    RemediationSummary,
    ReviewRequest,
    RevisionResponse,
    SandboxRunResponse,
    StepResponse,
    VerificationComparison,
    VerificationResponse,
)
from opspilot_api.remediation.state import RETRYABLE, TERMINAL
from opspilot_api.remediation.writer import write_enabled_for
from opspilot_api.repositories.models import (
    GitHubInstallation,
    Repository,
    RepositorySnapshot,
    ServiceRepositoryMapping,
)
from opspilot_api.repositories.provider import ProviderError, RepositoryProvider

S = RemediationStatus


class IneligibleError(DomainError):
    status_code = 422
    code = "remediation_ineligible"


class DependencyUnavailableError(DomainError):
    status_code = 503
    code = "dependency_unavailable"


@dataclass(frozen=True)
class EligibleScope:
    incident_id: UUID
    repository: Repository
    installation: GitHubInstallation
    path_prefix: str | None
    base_commit_sha: str


async def require_remediation(
    session: AsyncSession, workspace_id: UUID, remediation_id: UUID
) -> RemediationProposal:
    proposal = await session.scalar(
        select(RemediationProposal).where(
            RemediationProposal.id == remediation_id,
            RemediationProposal.workspace_id == workspace_id,
        )
    )
    if proposal is None:
        raise NotFoundError("remediation not found")
    return proposal


async def resolve_eligibility(
    session: AsyncSession, workspace_id: UUID, investigation_id: UUID
) -> tuple[Investigation, EligibleScope]:
    """Every start condition from the module contract; any doubt means ineligible."""
    await require_workspace(session, workspace_id)
    investigation = await session.scalar(
        select(Investigation).where(
            Investigation.id == investigation_id, Investigation.workspace_id == workspace_id
        )
    )
    if investigation is None:
        raise NotFoundError("investigation not found")
    if investigation.status != InvestigationStatus.COMPLETED.value:
        raise IneligibleError("investigation is not completed")
    review = await session.scalar(
        select(InvestigationReview)
        .where(
            InvestigationReview.investigation_id == investigation_id,
            InvestigationReview.workspace_id == workspace_id,
        )
        .order_by(InvestigationReview.created_at.desc(), InvestigationReview.id)
    )
    if review is None or review.action != ReviewAction.APPROVED.value:
        raise IneligibleError("investigation has not been approved by a human")
    if not (investigation.root_cause or "").strip():
        raise IneligibleError("investigation has no root cause")
    evidence = await session.scalar(
        select(EvidenceItem.id)
        .where(
            EvidenceItem.investigation_id == investigation_id,
            EvidenceItem.workspace_id == workspace_id,
        )
        .limit(1)
    )
    if evidence is None:
        raise IneligibleError("investigation has no supporting evidence")
    incident = await session.scalar(
        select(Incident).where(
            Incident.id == investigation.incident_id, Incident.workspace_id == workspace_id
        )
    )
    if incident is None or incident.service_id is None:
        raise IneligibleError("incident is not linked to a service")
    service = await session.scalar(
        select(MonitoredService).where(
            MonitoredService.id == incident.service_id,
            MonitoredService.workspace_id == workspace_id,
        )
    )
    ordering: list[Any] = [ServiceRepositoryMapping.created_at, ServiceRepositoryMapping.id]
    if service is not None:
        # Prefer the mapping whose environment matches the affected service.
        ordering.insert(
            0, case((ServiceRepositoryMapping.environment == service.environment, 0), else_=1)
        )
    mapping = await session.scalar(
        select(ServiceRepositoryMapping)
        .where(
            ServiceRepositoryMapping.workspace_id == workspace_id,
            ServiceRepositoryMapping.service_id == incident.service_id,
        )
        .order_by(*ordering)
    )
    if mapping is None:
        raise IneligibleError("no repository is mapped to the affected service")
    repository = await session.scalar(
        select(Repository).where(
            Repository.id == mapping.repository_id, Repository.workspace_id == workspace_id
        )
    )
    if repository is None or repository.status != "active" or repository.is_archived:
        raise IneligibleError("mapped repository is not active")
    installation = await session.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == repository.github_installation_id,
            GitHubInstallation.workspace_id == workspace_id,
        )
    )
    if installation is None or installation.status != "active":
        raise IneligibleError("GitHub installation is not active")
    snapshot = (
        await session.scalar(
            select(RepositorySnapshot).where(
                RepositorySnapshot.id == repository.active_snapshot_id,
                RepositorySnapshot.workspace_id == workspace_id,
                RepositorySnapshot.status == "ready",
            )
        )
        if repository.active_snapshot_id
        else None
    )
    if snapshot is None:
        raise IneligibleError("no concrete base commit: repository has no ready snapshot")
    return investigation, EligibleScope(
        incident.id, repository, installation, mapping.path_prefix, snapshot.commit_sha
    )


async def create_remediation(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    investigation_id: UUID,
    provider: RepositoryProvider,
    actor: Actor,
    retry_of_id: UUID | None = None,
    correlation_id: str | None = None,
) -> RemediationProposal:
    actor.require(Permission.REMEDIATIONS_CREATE)
    investigation, scope = await resolve_eligibility(session, workspace_id, investigation_id)
    head = await _fetch_head(
        provider, scope.installation.provider_installation_id, scope.repository
    )
    if head != scope.base_commit_sha:
        raise IneligibleError(
            "the indexed snapshot is behind the default branch; sync the repository first"
        )
    remediation_id = uuid4()
    proposal = RemediationProposal(
        id=remediation_id,
        workspace_id=workspace_id,
        incident_id=scope.incident_id,
        investigation_id=investigation.id,
        repository_id=scope.repository.id,
        retry_of_id=retry_of_id,
        graph_thread_id=f"remediation-{remediation_id}-0",
        base_commit_sha=scope.base_commit_sha,
        status=S.DRAFTING.value,
        title=(investigation.title or "Remediation proposal")[:240],
        risk_level=RiskLevel.LOW.value,
        generator_prompt_version=PROMPT_VERSION,
        generator_provider=settings.AI_PRIMARY_PROVIDER,
        generator_model=settings.AI_PRIMARY_MODEL,
        structured_output={"scope": {"path_prefix": scope.path_prefix}},
        requested_by_user_id=actor.user_id,
        version=1,
    )
    session.add(proposal)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("an active remediation already exists for this investigation") from exc
    record_audit(
        session,
        workspace_id,
        remediation_id,
        "remediation_requested",
        "investigation",
        investigation.id,
        "success",
        correlation_id=correlation_id,
        details={
            "repository": scope.repository.full_name,
            "base_commit_sha": scope.base_commit_sha,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("an active remediation already exists for this investigation") from exc
    await session.refresh(proposal)
    return proposal


async def list_remediations(
    session: AsyncSession,
    workspace_id: UUID,
    status: str | None = None,
    investigation_id: UUID | None = None,
    limit: int = 100,
) -> list[RemediationProposal]:
    await require_workspace(session, workspace_id)
    query = select(RemediationProposal).where(RemediationProposal.workspace_id == workspace_id)
    if status:
        query = query.where(RemediationProposal.status == status)
    if investigation_id:
        query = query.where(RemediationProposal.investigation_id == investigation_id)
    rows = await session.scalars(
        query.order_by(RemediationProposal.created_at.desc(), RemediationProposal.id).limit(limit)
    )
    return list(rows.all())


async def _rows(session: AsyncSession, model: Any, *conditions: Any, order: Any) -> list[Any]:
    return list((await session.scalars(select(model).where(*conditions).order_by(*order))).all())


async def sandbox_runs(
    session: AsyncSession, workspace_id: UUID, remediation_id: UUID
) -> list[SandboxRunResponse]:
    await require_remediation(session, workspace_id, remediation_id)
    runs = await _rows(
        session,
        SandboxRun,
        SandboxRun.workspace_id == workspace_id,
        SandboxRun.remediation_id == remediation_id,
        order=(SandboxRun.created_at, SandboxRun.id),
    )
    result: list[SandboxRunResponse] = []
    for run in runs:
        checks = await _rows(
            session,
            VerificationCheck,
            VerificationCheck.workspace_id == workspace_id,
            VerificationCheck.sandbox_run_id == run.id,
            order=(VerificationCheck.created_at, VerificationCheck.id),
        )
        item = SandboxRunResponse.model_validate(run)
        item.checks = [CheckResponse.model_validate(check) for check in checks]
        result.append(item)
    return result


async def verification(
    session: AsyncSession, workspace_id: UUID, remediation_id: UUID
) -> VerificationResponse:
    proposal = await require_remediation(session, workspace_id, remediation_id)
    runs = await sandbox_runs(session, workspace_id, remediation_id)
    current = [run for run in runs if run.revision_id == proposal.current_revision_id]
    baseline = next((run for run in reversed(current) if run.run_kind == "baseline"), None)
    patched = next((run for run in reversed(current) if run.run_kind == "patched"), None)
    raw = proposal.structured_output.get("verification")
    return VerificationResponse(
        remediation_id=remediation_id,
        baseline=baseline,
        patched=patched,
        comparison=VerificationComparison.model_validate(raw) if raw else None,
        verification_hash=proposal.verification_hash,
    )


async def pull_request(
    session: AsyncSession, workspace_id: UUID, remediation_id: UUID
) -> FixPullRequest:
    await require_remediation(session, workspace_id, remediation_id)
    row = await session.scalar(
        select(FixPullRequest)
        .where(
            FixPullRequest.workspace_id == workspace_id,
            FixPullRequest.remediation_id == remediation_id,
        )
        .order_by(FixPullRequest.created_at.desc())
    )
    if row is None:
        raise NotFoundError("no pull request for this remediation")
    return row


async def remediation_detail(
    session: AsyncSession, settings: Settings, workspace_id: UUID, remediation_id: UUID
) -> RemediationDetail:
    proposal = await require_remediation(session, workspace_id, remediation_id)
    scope = {"workspace_id": workspace_id, "remediation_id": remediation_id}
    revisions = await _rows(
        session,
        RemediationRevision,
        RemediationRevision.workspace_id == workspace_id,
        RemediationRevision.remediation_id == remediation_id,
        order=(RemediationRevision.revision_number,),
    )
    evaluations = await _rows(
        session,
        PolicyEvaluation,
        PolicyEvaluation.workspace_id == workspace_id,
        PolicyEvaluation.remediation_id == remediation_id,
        order=(PolicyEvaluation.evaluated_at, PolicyEvaluation.id),
    )
    approvals = await _rows(
        session,
        RemediationApproval,
        RemediationApproval.workspace_id == workspace_id,
        RemediationApproval.remediation_id == remediation_id,
        order=(RemediationApproval.created_at, RemediationApproval.id),
    )
    steps = await _rows(
        session,
        RemediationStep,
        RemediationStep.workspace_id == workspace_id,
        RemediationStep.remediation_id == remediation_id,
        order=(RemediationStep.created_at, RemediationStep.id),
    )
    audit = await _rows(
        session,
        RemediationAuditEvent,
        RemediationAuditEvent.workspace_id == workspace_id,
        RemediationAuditEvent.remediation_id == remediation_id,
        order=(RemediationAuditEvent.created_at, RemediationAuditEvent.id),
    )
    pr = await session.scalar(
        select(FixPullRequest)
        .where(
            FixPullRequest.workspace_id == workspace_id,
            FixPullRequest.remediation_id == remediation_id,
        )
        .order_by(FixPullRequest.created_at.desc())
    )
    repository = await session.get(Repository, proposal.repository_id)
    investigation = await session.get(Investigation, proposal.investigation_id)
    cited = {str(item) for item in proposal.structured_output.get("evidence_ids", [])}
    evidence = [
        {
            "id": str(item.id),
            "title": item.title,
            "excerpt": item.excerpt,
            "source_type": item.source_type,
        }
        for item in await _rows(
            session,
            EvidenceItem,
            EvidenceItem.workspace_id == workspace_id,
            EvidenceItem.investigation_id == proposal.investigation_id,
            order=(EvidenceItem.collected_at, EvidenceItem.id),
        )
        if str(item.id) in cited
    ]
    binding = None
    if proposal.status == S.AWAITING_APPROVAL.value and proposal.verification_hash:
        revision = revisions[-1] if revisions else None
        if revision is not None and proposal.policy_version:
            binding = ApprovalBindingResponse(
                workspace_id=workspace_id,
                repository_id=proposal.repository_id,
                base_commit_sha=proposal.base_commit_sha,
                revision_id=revision.id,
                patch_hash=revision.patch_hash,
                policy_version=proposal.policy_version,
                verification_hash=proposal.verification_hash,
                expires_in_seconds=settings.REMEDIATION_APPROVAL_TTL_SECONDS,
            )
    summary = RemediationSummary.model_validate(proposal).model_dump()
    del scope
    return RemediationDetail(
        **summary,
        problem_statement=proposal.problem_statement,
        proposed_solution=proposal.proposed_solution,
        affected_components=proposal.affected_components,
        generator_provider=proposal.generator_provider,
        generator_model=proposal.generator_model,
        generator_prompt_version=proposal.generator_prompt_version,
        structured_output={k: v for k, v in proposal.structured_output.items() if k != "scope"},
        total_input_tokens=proposal.total_input_tokens,
        total_output_tokens=proposal.total_output_tokens,
        estimated_cost_usd=float(proposal.estimated_cost_usd)
        if proposal.estimated_cost_usd is not None
        else None,
        current_revision_id=proposal.current_revision_id,
        repository_full_name=repository.full_name if repository else None,
        root_cause=investigation.root_cause if investigation else None,
        evidence=evidence,
        revisions=[RevisionResponse.model_validate(item) for item in revisions],
        policy_evaluations=[PolicyEvaluationResponse.model_validate(item) for item in evaluations],
        approvals=[ApprovalResponse.model_validate(item) for item in approvals],
        steps=[StepResponse.model_validate(item) for item in steps],
        pull_request=PullRequestResponse.model_validate(pr) if pr else None,
        approval_binding=binding,
        write_enabled=bool(
            repository and write_enabled_for(settings, workspace_id, repository.full_name)
        ),
        audit_events=[
            {
                "id": str(item.id),
                "action": item.action,
                "result": item.result,
                "actor_type": item.actor_type,
                "actor_id": item.actor_id,
                "target_type": item.target_type,
                "correlation_id": item.correlation_id,
                "created_at": item.created_at.isoformat(),
            }
            for item in audit
        ],
    )


async def cancel_remediation(
    session: AsyncSession,
    workspace_id: UUID,
    remediation_id: UUID,
    correlation_id: str | None,
    actor: Actor,
) -> RemediationProposal:
    actor.require(Permission.REMEDIATIONS_CREATE)
    proposal = await require_remediation(session, workspace_id, remediation_id)
    if proposal.status == S.CANCELLED.value:
        return proposal
    if S(proposal.status) in TERMINAL:
        raise ConflictError("terminal remediation cannot be cancelled")
    await session.run_sync(
        lambda sync: transition(sync, proposal, S.CANCELLED, completed_at=utcnow())
    )
    record_audit(
        session,
        workspace_id,
        remediation_id,
        "remediation_cancelled",
        "remediation",
        remediation_id,
        "success",
        correlation_id=correlation_id,
    )
    await session.commit()
    return proposal


async def retry_remediation(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    provider: RepositoryProvider,
    actor: Actor,
    correlation_id: str | None,
) -> RemediationProposal:
    actor.require(Permission.REMEDIATIONS_CREATE)
    previous = await require_remediation(session, workspace_id, remediation_id)
    if S(previous.status) not in RETRYABLE:
        raise ConflictError("remediation cannot be retried from its current status")
    return await create_remediation(
        session,
        settings,
        workspace_id,
        previous.investigation_id,
        provider,
        actor,
        previous.id,
        correlation_id,
    )


async def _fetch_head(
    provider: RepositoryProvider, installation_id: int, repository: Repository
) -> str:
    def call() -> str:
        return provider.get_branch(
            installation_id, repository.owner, repository.name, repository.default_branch
        ).head_sha

    try:
        return await asyncio.to_thread(call)
    except ProviderError as exc:
        # Unable to confirm repository state: fail closed instead of approving blindly.
        raise DependencyUnavailableError("cannot confirm repository state") from exc


def _separation_of_duties(proposal: RemediationProposal, actor: Actor) -> None:
    """High-risk fixes need a second pair of eyes: the requester and last reviser cannot approve."""
    if proposal.risk_level != "high":
        return
    if actor.user_id is not None and actor.user_id in {
        proposal.requested_by_user_id,
        proposal.last_revised_by_user_id,
    }:
        raise ForbiddenError(
            "separation of duties: requester or last reviser cannot approve a high-risk fix"
        )


async def approver_still_valid(
    session: AsyncSession, proposal: RemediationProposal, approval: RemediationApproval
) -> bool:
    """The approving human must still be an active user holding the review permission."""
    try:
        approver = UUID(str(approval.actor_id))
    except ValueError:
        return False
    membership = await active_membership(session, approver, proposal.workspace_id)
    if membership is None:
        return False
    from opspilot_api.auth.permissions import permissions_for

    if Permission.REMEDIATIONS_REVIEW not in permissions_for(membership.role):
        return False
    conflicted = proposal.risk_level == "high" and approver in {
        proposal.requested_by_user_id,
        proposal.last_revised_by_user_id,
    }
    return not conflicted


@dataclass(frozen=True)
class ReviewOutcome:
    approval: RemediationApproval
    proposal: RemediationProposal
    thread_id: str
    resume: bool
    replay: bool = False


async def submit_review(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    data: ReviewRequest,
    provider: RepositoryProvider,
    actor: Actor,
    correlation_id: str | None = None,
) -> ReviewOutcome:
    # Rechecked here (not only at the route): the reviewer is the verified human actor, never a
    # client-supplied identity, and API keys or system actors can never review.
    actor.require(Permission.REMEDIATIONS_REVIEW)
    if actor.workspace_id != workspace_id:
        raise ForbiddenError("actor is bound to a different workspace")
    proposal = await require_remediation(session, workspace_id, remediation_id)
    comment = data.comment.strip()
    if data.action is ApprovalAction.APPROVED and proposal.status == S.APPROVED.value:
        # Idempotent replay: same artifact, version from before the approval was applied.
        existing = await session.run_sync(lambda sync: latest_approval(sync, proposal))
        if (
            existing is not None
            and existing.revision_id == data.revision_id
            and data.version + 1 == proposal.version
        ):
            return ReviewOutcome(existing, proposal, proposal.graph_thread_id, False, True)
    if proposal.status != S.AWAITING_APPROVAL.value:
        raise ConflictError("remediation is not awaiting approval")
    if data.version != proposal.version:
        raise ConflictError("remediation version is stale; reload and review again")
    revision = await session.run_sync(lambda sync: current_revision(sync, proposal))
    if revision is None or data.revision_id != revision.id:
        raise ConflictError("revision is not the current artifact; reload and review again")
    base = {
        "workspace_id": workspace_id,
        "remediation_id": remediation_id,
        "revision_id": revision.id,
        "actor_id": str(actor.user_id),
        "comment": comment,
        "approved_patch_hash": revision.patch_hash,
        "approved_base_commit_sha": revision.base_commit_sha,
    }
    if data.action is ApprovalAction.APPROVED:
        _separation_of_duties(proposal, actor)
        approval = await _approve(
            session, settings, proposal, revision, base, provider, correlation_id
        )
        return ReviewOutcome(approval, proposal, proposal.graph_thread_id, True)
    if data.action is ApprovalAction.REVISION_REQUESTED:
        if proposal.revision_count >= settings.REMEDIATION_MAX_REVISIONS:
            raise ConflictError("maximum revision count reached")
        target = S.DRAFTING
    else:
        target = S.REJECTED
    approval = RemediationApproval(
        id=uuid4(),
        action=data.action.value,
        approval_context={},
        approval_context_hash="",
        **base,
    )
    session.add(approval)
    await session.flush()
    fields: dict[str, Any] = {"completed_at": utcnow()} if target is S.REJECTED else {}
    if target is S.DRAFTING:
        fields["last_revised_by_user_id"] = actor.user_id
    await session.run_sync(lambda sync: transition(sync, proposal, target, **fields))
    record_audit(
        session,
        workspace_id,
        remediation_id,
        f"review_{data.action.value}",
        "revision",
        revision.id,
        "success",
        correlation_id=correlation_id,
    )
    await session.commit()
    return ReviewOutcome(approval, proposal, proposal.graph_thread_id, True)


async def _approve(
    session: AsyncSession,
    settings: Settings,
    proposal: RemediationProposal,
    revision: RemediationRevision,
    base: dict[str, Any],
    provider: RepositoryProvider,
    correlation_id: str | None,
) -> RemediationApproval:
    workspace_id = proposal.workspace_id
    if proposal.policy_decision not in {
        PolicyDecision.ALLOWED.value,
        PolicyDecision.ALLOWED_WITH_WARNINGS.value,
    }:
        raise ConflictError("policy has not allowed this patch")
    if proposal.policy_version != POLICY_VERSION:
        raise ConflictError("policy version changed; re-verification is required")
    if proposal.verification_state not in {"passed", "partial"}:
        raise ConflictError("verification did not succeed; approval is not possible")
    repository = await session.get(Repository, proposal.repository_id)
    installation = (
        await session.get(GitHubInstallation, repository.github_installation_id)
        if repository
        else None
    )
    if repository is None or installation is None or repository.workspace_id != workspace_id:
        raise ConflictError("repository is unavailable")
    head = await _fetch_head(provider, installation.provider_installation_id, repository)
    if head != proposal.base_commit_sha:
        await session.run_sync(
            lambda sync: transition(
                sync,
                proposal,
                S.STALE,
                stale_reason="default branch advanced",
                completed_at=utcnow(),
            )
        )
        record_audit(
            session,
            workspace_id,
            proposal.id,
            "remediation_stale",
            "remediation",
            proposal.id,
            "stale",
            correlation_id=correlation_id,
            details={"reason": "default branch advanced"},
        )
        await session.commit()
        raise ConflictError(
            "remediation is stale: the default branch advanced past the base commit"
        )
    binding = await session.run_sync(lambda sync: current_binding(sync, proposal))
    if (
        not binding["verification_hash"]
        or binding["verification_hash"] != proposal.verification_hash
    ):
        raise ConflictError("verification results do not match; re-verification is required")
    context = ApprovalContext(
        workspace_id=workspace_id,
        repository_id=proposal.repository_id,
        base_commit_sha=proposal.base_commit_sha,
        revision_id=revision.id,
        patch_hash=revision.patch_hash,
        policy_version=proposal.policy_version or "",
        verification_hash=binding["verification_hash"],
        expires_at=utcnow() + timedelta(seconds=settings.REMEDIATION_APPROVAL_TTL_SECONDS),
    )
    approval = RemediationApproval(
        id=uuid4(),
        action=ApprovalAction.APPROVED.value,
        approval_context=context.as_dict(),
        approval_context_hash=context.digest,
        expires_at=context.expires_at,
        **base,
    )
    session.add(approval)
    await session.flush()
    await session.run_sync(lambda sync: transition(sync, proposal, S.APPROVED))
    record_audit(
        session,
        workspace_id,
        proposal.id,
        "review_approved",
        "revision",
        revision.id,
        "success",
        correlation_id=correlation_id,
        details={"patch_hash": revision.patch_hash, "approval_context_hash": context.digest},
    )
    await session.commit()
    return approval


@dataclass(frozen=True)
class PublishRequest:
    proposal: RemediationProposal
    approval: RemediationApproval | None
    enqueue: bool
    reverify: bool = False


async def request_publish(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    actor: Actor,
    correlation_id: str | None = None,
) -> PublishRequest:
    actor.require(Permission.REMEDIATIONS_PUBLISH)
    proposal = await require_remediation(session, workspace_id, remediation_id)
    if proposal.status in {S.PUBLISHING.value, S.PULL_REQUEST_CREATED.value}:
        return PublishRequest(proposal, None, enqueue=False)  # idempotent repeat
    if proposal.status != S.APPROVED.value:
        raise ConflictError("only an approved remediation can be published")
    repository = await session.get(Repository, proposal.repository_id)
    if repository is None or not write_enabled_for(settings, workspace_id, repository.full_name):
        raise ConflictError("GitHub write access is not enabled for this workspace and repository")
    approval = await session.run_sync(lambda sync: latest_approval(sync, proposal))
    if approval is None:
        raise ConflictError("no approval exists for this remediation")
    problems = await session.run_sync(lambda sync: check_binding(sync, proposal, approval))
    if not await approver_still_valid(session, proposal, approval):
        problems = [*problems, "approver_not_authorized"]
    if problems:
        record_audit(
            session,
            workspace_id,
            remediation_id,
            "approval_invalidated",
            "remediation",
            remediation_id,
            "invalidated",
            correlation_id=correlation_id,
            details={"reasons": problems},
        )
        await session.run_sync(
            lambda sync: transition(
                sync, proposal, S.POLICY_CHECK, verification_hash=None, verification_state=None
            )
        )
        await session.commit()
        return PublishRequest(proposal, approval, enqueue=False, reverify=True)
    await session.run_sync(
        lambda sync: transition(
            sync, proposal, S.PUBLISHING, publish_requested_by_user_id=actor.user_id
        )
    )
    record_audit(
        session,
        workspace_id,
        remediation_id,
        "publication_requested",
        "approval",
        approval.id,
        "success",
        correlation_id=correlation_id,
    )
    await session.commit()
    return PublishRequest(proposal, approval, enqueue=True)


async def recover_stale_remediations(session: AsyncSession, settings: Settings) -> int:
    """Fail abandoned in-flight work; interrupted publication returns to approved for a retry."""
    cutoff = utcnow() - timedelta(seconds=settings.AI_MAX_RUNTIME_SECONDS * 2)
    running = [S.POLICY_CHECK.value, S.SANDBOX_QUEUED.value, S.SANDBOX_RUNNING.value]
    failed = await session.execute(
        update(RemediationProposal)
        .where(RemediationProposal.status.in_(running), RemediationProposal.updated_at < cutoff)
        .values(
            status=S.FAILED.value,
            failed_at=utcnow(),
            failure_code="stale_worker",
            failure_message="Remediation worker stopped before completing the workflow.",
            version=RemediationProposal.version + 1,
        )
        .returning(RemediationProposal.id)
    )
    publishing = await session.execute(
        update(RemediationProposal)
        .where(
            RemediationProposal.status == S.PUBLISHING.value,
            RemediationProposal.updated_at < cutoff,
        )
        .values(status=S.APPROVED.value, version=RemediationProposal.version + 1)
        .returning(RemediationProposal.id)
    )
    count = len(list(failed.scalars())) + len(list(publishing.scalars()))
    await session.commit()
    return count


__all__ = [
    "ACTIVE_STATUSES",
    "DependencyUnavailableError",
    "IneligibleError",
    "ReviewOutcome",
    "cancel_remediation",
    "create_remediation",
    "emit_event_async",
    "latest_run",
    "list_remediations",
    "pull_request",
    "remediation_detail",
    "request_publish",
    "retry_remediation",
    "sandbox_runs",
    "submit_review",
    "verification",
]
