"""Workspace-scoped investigation application services."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.context import Actor
from opspilot_api.auth.permissions import Permission
from opspilot_api.config import Settings
from opspilot_api.incidents.service import ConflictError, NotFoundError, require_incident, utcnow
from opspilot_api.investigations.models import (
    EvidenceItem,
    Investigation,
    InvestigationReview,
    InvestigationStatus,
    InvestigationStep,
    InvestigationTrigger,
    ReviewAction,
)
from opspilot_api.investigations.prompts import PROMPT_VERSION
from opspilot_api.investigations.schemas import (
    EvidenceResponse,
    InvestigationDetail,
    InvestigationReport,
    InvestigationSummary,
    ReviewRequest,
    ReviewResponse,
    StepResponse,
)

ACTIVE = {
    InvestigationStatus.QUEUED.value,
    InvestigationStatus.RUNNING.value,
    InvestigationStatus.AWAITING_REVIEW.value,
}
TERMINAL = {
    InvestigationStatus.COMPLETED.value,
    InvestigationStatus.REJECTED.value,
    InvestigationStatus.FAILED.value,
    InvestigationStatus.CANCELLED.value,
}


async def require_investigation(
    session: AsyncSession, workspace_id: UUID, investigation_id: UUID
) -> Investigation:
    investigation = await session.scalar(
        select(Investigation).where(
            Investigation.id == investigation_id,
            Investigation.workspace_id == workspace_id,
        )
    )
    if investigation is None:
        raise NotFoundError("investigation not found")
    return investigation


async def create_investigation(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    incident_id: UUID,
    trigger: InvestigationTrigger = InvestigationTrigger.MANUAL,
    retry_of_id: UUID | None = None,
) -> Investigation:
    await require_incident(session, workspace_id, incident_id)
    investigation_id = uuid4()
    investigation = Investigation(
        id=investigation_id,
        workspace_id=workspace_id,
        incident_id=incident_id,
        retry_of_id=retry_of_id,
        graph_thread_id=str(investigation_id),
        status=InvestigationStatus.QUEUED.value,
        trigger=trigger.value,
        prompt_version=PROMPT_VERSION,
        primary_provider=settings.AI_PRIMARY_PROVIDER,
        primary_model=settings.AI_PRIMARY_MODEL,
        fallback_provider=settings.AI_FALLBACK_PROVIDER,
        fallback_model=settings.AI_FALLBACK_MODEL,
        version=1,
    )
    session.add(investigation)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        active = await session.scalar(
            select(Investigation).where(
                Investigation.workspace_id == workspace_id,
                Investigation.incident_id == incident_id,
                Investigation.status.in_(ACTIVE),
            )
        )
        detail = (
            f"active investigation already exists: {active.id}"
            if active
            else "active investigation already exists"
        )
        raise ConflictError(detail) from exc
    await session.refresh(investigation)
    return investigation


async def list_investigations(
    session: AsyncSession, workspace_id: UUID, incident_id: UUID
) -> list[Investigation]:
    await require_incident(session, workspace_id, incident_id)
    return list(
        (
            await session.scalars(
                select(Investigation)
                .where(
                    Investigation.workspace_id == workspace_id,
                    Investigation.incident_id == incident_id,
                )
                .order_by(Investigation.created_at.desc(), Investigation.id.desc())
            )
        ).all()
    )


async def investigation_detail(
    session: AsyncSession, workspace_id: UUID, investigation_id: UUID
) -> InvestigationDetail:
    investigation = await require_investigation(session, workspace_id, investigation_id)
    steps = list(
        (
            await session.scalars(
                select(InvestigationStep)
                .where(
                    InvestigationStep.workspace_id == workspace_id,
                    InvestigationStep.investigation_id == investigation_id,
                )
                .order_by(InvestigationStep.created_at, InvestigationStep.id)
            )
        ).all()
    )
    evidence = list(
        (
            await session.scalars(
                select(EvidenceItem)
                .where(
                    EvidenceItem.workspace_id == workspace_id,
                    EvidenceItem.investigation_id == investigation_id,
                )
                .order_by(EvidenceItem.collected_at, EvidenceItem.id)
            )
        ).all()
    )
    reviews = list(
        (
            await session.scalars(
                select(InvestigationReview)
                .where(
                    InvestigationReview.workspace_id == workspace_id,
                    InvestigationReview.investigation_id == investigation_id,
                )
                .order_by(InvestigationReview.created_at, InvestigationReview.id)
            )
        ).all()
    )
    report = None
    report_steps = [
        step
        for step in steps
        if step.node_name in {"produce_report", "revision"} and step.structured_output
    ]
    if report_steps:
        report = InvestigationReport.model_validate(report_steps[-1].structured_output)
    base = InvestigationSummary.model_validate(investigation).model_dump()
    return InvestigationDetail(
        **base,
        steps=[StepResponse.model_validate(step) for step in steps],
        evidence=[EvidenceResponse.model_validate(item) for item in evidence],
        reviews=[ReviewResponse.model_validate(review) for review in reviews],
        report=report,
    )


async def submit_review(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    investigation_id: UUID,
    data: ReviewRequest,
    actor: Actor,
) -> InvestigationReview:
    actor.require(Permission.INVESTIGATIONS_REVIEW)
    investigation = await require_investigation(session, workspace_id, investigation_id)
    if investigation.status != InvestigationStatus.AWAITING_REVIEW.value:
        raise ConflictError("investigation is not awaiting review")
    if (
        data.action is ReviewAction.REVISION_REQUESTED
        and investigation.revision_count >= settings.AI_MAX_REVISIONS
    ):
        raise ConflictError("maximum revision count reached")
    values: dict[str, object] = {
        "version": data.version + 1,
        "updated_at": utcnow(),
        "status": InvestigationStatus.RUNNING.value,
    }
    if data.action is ReviewAction.REVISION_REQUESTED:
        values["revision_count"] = investigation.revision_count + 1
    result = await session.execute(
        update(Investigation)
        .where(
            Investigation.id == investigation_id,
            Investigation.workspace_id == workspace_id,
            Investigation.status == InvestigationStatus.AWAITING_REVIEW.value,
            Investigation.version == data.version,
        )
        .values(**values)
        .returning(Investigation.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise ConflictError("investigation version is stale")
    review = InvestigationReview(
        id=uuid4(),
        workspace_id=workspace_id,
        investigation_id=investigation_id,
        action=data.action.value,
        comment=data.comment.strip() if data.comment else None,
        actor_id=str(actor.user_id),
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return review


async def cancel_investigation(
    session: AsyncSession, workspace_id: UUID, investigation_id: UUID
) -> Investigation:
    investigation = await require_investigation(session, workspace_id, investigation_id)
    if investigation.status == InvestigationStatus.CANCELLED.value:
        return investigation
    if investigation.status in TERMINAL:
        raise ConflictError("terminal investigation cannot be cancelled")
    now = utcnow()
    await session.execute(
        update(Investigation)
        .where(
            Investigation.id == investigation_id,
            Investigation.workspace_id == workspace_id,
            Investigation.status.in_(ACTIVE),
        )
        .values(
            status=InvestigationStatus.CANCELLED.value,
            cancelled_at=now,
            updated_at=now,
            version=Investigation.version + 1,
        )
    )
    await session.commit()
    return await require_investigation(session, workspace_id, investigation_id)


async def retry_investigation(
    session: AsyncSession, settings: Settings, workspace_id: UUID, investigation_id: UUID
) -> Investigation:
    previous = await require_investigation(session, workspace_id, investigation_id)
    if previous.status != InvestigationStatus.FAILED.value:
        raise ConflictError("only failed investigations can be retried")
    return await create_investigation(
        session,
        settings,
        workspace_id,
        previous.incident_id,
        InvestigationTrigger.MANUAL,
        retry_of_id=previous.id,
    )


async def recover_stale_investigations(session: AsyncSession, settings: Settings) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.AI_MAX_RUNTIME_SECONDS * 2)
    result = await session.execute(
        update(Investigation)
        .where(
            Investigation.status == InvestigationStatus.RUNNING.value,
            Investigation.updated_at < cutoff,
        )
        .values(
            status=InvestigationStatus.FAILED.value,
            failed_at=utcnow(),
            failure_code="stale_worker",
            failure_message="Investigation worker stopped before completing the workflow.",
            version=Investigation.version + 1,
        )
        .returning(Investigation.id)
    )
    ids = list(result.scalars())
    await session.commit()
    return len(ids)
