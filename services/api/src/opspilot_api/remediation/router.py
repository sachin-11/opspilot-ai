"""Thin REST adapters for evidence-backed remediation. Business logic lives in ``service``."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.config import Settings
from opspilot_api.incidents.service import ConflictError
from opspilot_api.logging_config import correlation_id_var
from opspilot_api.remediation.audit import emit_event_async
from opspilot_api.remediation.models import ApprovalAction, RemediationProposal
from opspilot_api.remediation.queue import (
    enqueue_publish,
    enqueue_remediation,
    enqueue_remediation_resume,
    new_thread_id,
)
from opspilot_api.remediation.schemas import (
    ActionResponse,
    PublishQueuedResponse,
    PullRequestResponse,
    RemediationDetail,
    RemediationQueuedResponse,
    RemediationSummary,
    ReviewRequest,
    SandboxRunResponse,
    VerificationResponse,
)
from opspilot_api.remediation.service import (
    cancel_remediation,
    create_remediation,
    list_remediations,
    pull_request,
    remediation_detail,
    request_publish,
    retry_remediation,
    sandbox_runs,
    submit_review,
    verification,
)
from opspilot_api.repositories.provider import RepositoryProvider
from opspilot_api.repositories.queue import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["remediation"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_repo_provider(request: Request) -> RepositoryProvider:
    return get_provider(request.app.state.settings)


Session = Annotated[AsyncSession, Depends(get_session)]
Provider = Annotated[RepositoryProvider, Depends(get_repo_provider)]


async def notify(
    request: Request,
    proposal: RemediationProposal,
    event_type: str,
    data: dict[str, object] | None = None,
) -> None:
    await emit_event_async(
        request.app.state.redis,
        proposal.workspace_id,
        proposal.incident_id,
        proposal.id,
        event_type,
        {"status": proposal.status, **(data or {})},
    )


def _action(proposal: RemediationProposal) -> ActionResponse:
    return ActionResponse(
        remediation_id=proposal.id,
        status=proposal.status,
        version=proposal.version,
    )


@router.post(
    "/workspaces/{workspace_id}/investigations/{investigation_id}/remediations",
    response_model=RemediationQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_remediation(
    request: Request,
    workspace_id: UUID,
    investigation_id: UUID,
    session: Session,
    provider: Provider,
) -> RemediationQueuedResponse:
    settings: Settings = request.app.state.settings
    proposal = await create_remediation(
        session,
        settings,
        workspace_id,
        investigation_id,
        provider,
        correlation_id=correlation_id_var.get(),
    )
    enqueue_remediation(settings, workspace_id, proposal.id, proposal.graph_thread_id, "start")
    await notify(request, proposal, "remediation.queued")
    return RemediationQueuedResponse(remediation_id=proposal.id, status=proposal.status)


@router.get("/workspaces/{workspace_id}/remediations", response_model=list[RemediationSummary])
async def get_remediations(
    workspace_id: UUID,
    session: Session,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    investigation_id: UUID | None = None,
) -> list[RemediationSummary]:
    rows = await list_remediations(session, workspace_id, status_filter, investigation_id)
    return [RemediationSummary.model_validate(item) for item in rows]


@router.get(
    "/workspaces/{workspace_id}/remediations/{remediation_id}", response_model=RemediationDetail
)
async def get_remediation(
    request: Request, workspace_id: UUID, remediation_id: UUID, session: Session
) -> RemediationDetail:
    return await remediation_detail(
        session, request.app.state.settings, workspace_id, remediation_id
    )


@router.post(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/cancel", response_model=ActionResponse
)
async def post_cancel(
    request: Request, workspace_id: UUID, remediation_id: UUID, session: Session
) -> ActionResponse:
    proposal = await cancel_remediation(
        session, workspace_id, remediation_id, correlation_id_var.get()
    )
    await notify(request, proposal, "remediation.cancelled")
    return _action(proposal)


@router.post(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/retry",
    response_model=RemediationQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_retry(
    request: Request,
    workspace_id: UUID,
    remediation_id: UUID,
    session: Session,
    provider: Provider,
) -> RemediationQueuedResponse:
    settings: Settings = request.app.state.settings
    proposal = await retry_remediation(
        session, settings, workspace_id, remediation_id, provider, correlation_id_var.get()
    )
    enqueue_remediation(settings, workspace_id, proposal.id, proposal.graph_thread_id, "start")
    await notify(request, proposal, "remediation.queued")
    return RemediationQueuedResponse(remediation_id=proposal.id, status=proposal.status)


@router.post(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/review",
    response_model=ActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_review(
    request: Request,
    workspace_id: UUID,
    remediation_id: UUID,
    data: ReviewRequest,
    session: Session,
    provider: Provider,
) -> ActionResponse:
    settings: Settings = request.app.state.settings
    outcome = await submit_review(
        session,
        settings,
        workspace_id,
        remediation_id,
        data,
        provider,
        correlation_id_var.get(),
    )
    if outcome.replay:
        return _action(outcome.proposal)
    enqueue_remediation_resume(
        settings, workspace_id, remediation_id, outcome.thread_id, outcome.approval.id
    )
    event = {
        ApprovalAction.APPROVED: "remediation.approved",
        ApprovalAction.REJECTED: "remediation.rejected",
        ApprovalAction.REVISION_REQUESTED: "remediation.revision_requested",
    }[data.action]
    await notify(request, outcome.proposal, event)
    return _action(outcome.proposal)


@router.get(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/sandbox-runs",
    response_model=list[SandboxRunResponse],
)
async def get_sandbox_runs(
    workspace_id: UUID, remediation_id: UUID, session: Session
) -> list[SandboxRunResponse]:
    return await sandbox_runs(session, workspace_id, remediation_id)


@router.get(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/verification",
    response_model=VerificationResponse,
)
async def get_verification(
    workspace_id: UUID, remediation_id: UUID, session: Session
) -> VerificationResponse:
    return await verification(session, workspace_id, remediation_id)


@router.post(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/publish",
    response_model=PublishQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_publish(
    request: Request, workspace_id: UUID, remediation_id: UUID, session: Session
) -> PublishQueuedResponse:
    settings: Settings = request.app.state.settings
    result = await request_publish(
        session, settings, workspace_id, remediation_id, correlation_id_var.get()
    )
    if result.reverify:
        thread = new_thread_id(remediation_id)
        result.proposal.graph_thread_id = thread
        await session.commit()
        enqueue_remediation(settings, workspace_id, remediation_id, thread, "reverify")
        await notify(request, result.proposal, "remediation.approval_invalidated")
        raise ConflictError(
            "approval is no longer valid for the current artifact; re-verification started"
        )
    if result.enqueue and result.approval is not None:
        enqueue_publish(settings, workspace_id, remediation_id, result.approval.id)
        await notify(request, result.proposal, "remediation.publish_queued")
    return PublishQueuedResponse(
        remediation_id=remediation_id,
        status=result.proposal.status,
    )


@router.get(
    "/workspaces/{workspace_id}/remediations/{remediation_id}/pull-request",
    response_model=PullRequestResponse,
)
async def get_pull_request(
    workspace_id: UUID, remediation_id: UUID, session: Session
) -> PullRequestResponse:
    return PullRequestResponse.model_validate(
        await pull_request(session, workspace_id, remediation_id)
    )
