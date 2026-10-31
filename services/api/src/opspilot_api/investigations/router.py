"""Thin REST adapters for queued, human-reviewed investigations."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.dependencies import CurrentActor
from opspilot_api.config import Settings
from opspilot_api.incidents.service import publish_notification
from opspilot_api.investigations.models import InvestigationStatus, ReviewAction
from opspilot_api.investigations.queue import enqueue_investigation, enqueue_review_resume
from opspilot_api.investigations.schemas import (
    ActionResponse,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationQueuedResponse,
    InvestigationSummary,
    ReviewRequest,
)
from opspilot_api.investigations.service import (
    cancel_investigation,
    create_investigation,
    investigation_detail,
    list_investigations,
    require_investigation,
    retry_investigation,
    submit_review,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["investigations"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def notify(
    request: Request,
    workspace_id: UUID,
    incident_id: UUID,
    investigation_id: UUID,
    event_type: str,
) -> None:
    try:
        await publish_notification(
            request.app.state.redis,
            workspace_id,
            incident_id,
            event_type,
            {"investigation_id": str(investigation_id)},
        )
    except RedisError as exc:
        logger.warning(
            "investigation notification failed", extra={"error_class": type(exc).__name__}
        )


@router.post(
    "/workspaces/{workspace_id}/incidents/{incident_id}/investigations",
    response_model=InvestigationQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_investigation(
    request: Request,
    workspace_id: UUID,
    incident_id: UUID,
    data: InvestigationCreate,
    session: Session,
) -> InvestigationQueuedResponse:
    settings: Settings = request.app.state.settings
    investigation = await create_investigation(
        session, settings, workspace_id, incident_id, data.trigger
    )
    enqueue_investigation(settings, workspace_id, investigation.id)
    await notify(
        request,
        workspace_id,
        incident_id,
        investigation.id,
        "investigation.queued",
    )
    return InvestigationQueuedResponse(
        investigation_id=investigation.id,
        status="queued",
    )


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}/investigations",
    response_model=list[InvestigationSummary],
)
async def get_investigations(
    workspace_id: UUID, incident_id: UUID, session: Session
) -> list[InvestigationSummary]:
    return [
        InvestigationSummary.model_validate(item)
        for item in await list_investigations(session, workspace_id, incident_id)
    ]


@router.get(
    "/workspaces/{workspace_id}/investigations/{investigation_id}",
    response_model=InvestigationDetail,
)
async def get_investigation(
    workspace_id: UUID, investigation_id: UUID, session: Session
) -> InvestigationDetail:
    return await investigation_detail(session, workspace_id, investigation_id)


@router.post(
    "/workspaces/{workspace_id}/investigations/{investigation_id}/review",
    response_model=ActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_review(
    request: Request,
    workspace_id: UUID,
    investigation_id: UUID,
    data: ReviewRequest,
    session: Session,
    actor: CurrentActor,
) -> ActionResponse:
    settings: Settings = request.app.state.settings
    investigation = await require_investigation(session, workspace_id, investigation_id)
    review = await submit_review(
        session,
        settings,
        workspace_id,
        investigation_id,
        data,
        actor,
    )
    enqueue_review_resume(settings, workspace_id, investigation_id, review.id)
    event_type = (
        "investigation.revision_requested"
        if data.action is ReviewAction.REVISION_REQUESTED
        else "investigation.review_submitted"
    )
    await notify(
        request,
        workspace_id,
        investigation.incident_id,
        investigation_id,
        event_type,
    )
    updated = await require_investigation(session, workspace_id, investigation_id)
    return ActionResponse(
        investigation_id=investigation_id,
        status=InvestigationStatus(updated.status),
        version=updated.version,
    )


@router.post(
    "/workspaces/{workspace_id}/investigations/{investigation_id}/cancel",
    response_model=ActionResponse,
)
async def post_cancel(
    request: Request,
    workspace_id: UUID,
    investigation_id: UUID,
    session: Session,
) -> ActionResponse:
    investigation = await cancel_investigation(session, workspace_id, investigation_id)
    await notify(
        request,
        workspace_id,
        investigation.incident_id,
        investigation.id,
        "investigation.cancelled",
    )
    return ActionResponse(
        investigation_id=investigation.id,
        status=InvestigationStatus(investigation.status),
        version=investigation.version,
    )


@router.post(
    "/workspaces/{workspace_id}/investigations/{investigation_id}/retry",
    response_model=InvestigationQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_retry(
    request: Request,
    workspace_id: UUID,
    investigation_id: UUID,
    session: Session,
) -> InvestigationQueuedResponse:
    settings: Settings = request.app.state.settings
    investigation = await retry_investigation(session, settings, workspace_id, investigation_id)
    enqueue_investigation(settings, workspace_id, investigation.id)
    await notify(
        request,
        workspace_id,
        investigation.incident_id,
        investigation.id,
        "investigation.queued",
    )
    return InvestigationQueuedResponse(
        investigation_id=investigation.id,
        status="queued",
    )
