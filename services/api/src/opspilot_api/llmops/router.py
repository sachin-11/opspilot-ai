"""Thin REST adapters for LLMOps. Permissions come from the central route policy."""

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from redis import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.audit import record_audit
from opspilot_api.auth.context import Actor, UnauthorizedError
from opspilot_api.auth.dependencies import CurrentActor, request_info
from opspilot_api.auth.models import AuditResult
from opspilot_api.config import Settings
from opspilot_api.llmops import service
from opspilot_api.llmops.breaker import RedisBreakerStore
from opspilot_api.llmops.queue import enqueue_eval_run
from opspilot_api.llmops.schemas import (
    BudgetOut,
    BudgetPatch,
    CostsResponse,
    DatasetOut,
    EvalActionResponse,
    EvalResultOut,
    EvalRunCreate,
    EvalRunDetail,
    EvalRunSummary,
    ExperimentCreate,
    ExperimentDetail,
    ExperimentOut,
    ExperimentStopRequest,
    HumanLabelCreate,
    HumanLabelOut,
    ModelConfigurationOut,
    PromptActionResponse,
    PromptDetail,
    PromptPublishRequest,
    PromptRollbackRequest,
    PromptSummary,
    PromptVersionCreate,
    PromptVersionOut,
    ProviderHealthOut,
    UsageResponse,
)
from opspilot_api.llmops.seed import ensure_golden_dataset, seed_platform

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/llmops", tags=["llmops"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def _user(actor: Actor) -> UUID:
    if actor.user_id is None:  # every LLMOps route is human-only; fail closed otherwise
        raise UnauthorizedError("a signed-in user is required")
    return actor.user_id


def _audit(request: Request, actor: Actor, workspace_id: UUID) -> service.AuditFn:
    async def emit(
        action: str, resource_type: str, resource_id: str | None, metadata: dict[str, Any]
    ) -> None:
        await record_audit(
            request.app.state.session_factory,
            request.app.state.settings,
            actor.principal,
            workspace_id,
            action,
            resource_type,
            resource_id,
            AuditResult.SUCCESS,
            request_info(request),
            metadata=metadata,
        )

    return emit


async def _seeded(session: AsyncSession) -> None:
    await session.run_sync(seed_platform)
    await session.run_sync(ensure_golden_dataset)


def _window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(days=days), now + timedelta(minutes=1)


# ------------------------------------------------------------------ prompts and models
@router.get("/prompts", response_model=list[PromptSummary])
async def get_prompts(
    workspace_id: UUID, session: Session, actor: CurrentActor
) -> list[PromptSummary]:
    return await service.list_prompts(session, workspace_id, _user(actor))


@router.get("/prompts/{prompt_id}", response_model=PromptDetail)
async def get_prompt(
    workspace_id: UUID, prompt_id: UUID, session: Session, actor: CurrentActor
) -> PromptDetail:
    await service.list_prompts(session, workspace_id, _user(actor))
    return await service.get_prompt(session, workspace_id, prompt_id)


@router.post(
    "/prompts/{prompt_id}/versions",
    response_model=PromptVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_prompt_version(
    request: Request,
    workspace_id: UUID,
    prompt_id: UUID,
    body: PromptVersionCreate,
    session: Session,
    actor: CurrentActor,
) -> PromptVersionOut:
    return await service.create_prompt_version(
        session, workspace_id, prompt_id, body, actor, _audit(request, actor, workspace_id)
    )


@router.post("/prompts/{prompt_id}/publish", response_model=PromptActionResponse)
async def post_prompt_publish(
    request: Request,
    workspace_id: UUID,
    prompt_id: UUID,
    body: PromptPublishRequest,
    session: Session,
    actor: CurrentActor,
) -> PromptActionResponse:
    return await service.publish_prompt(
        session,
        workspace_id,
        prompt_id,
        body.version_id,
        body.eval_run_id,
        body.override_reason,
        actor,
        _audit(request, actor, workspace_id),
    )


@router.post("/prompts/{prompt_id}/rollback", response_model=PromptActionResponse)
async def post_prompt_rollback(
    request: Request,
    workspace_id: UUID,
    prompt_id: UUID,
    body: PromptRollbackRequest,
    session: Session,
    actor: CurrentActor,
) -> PromptActionResponse:
    return await service.rollback_prompt(
        session,
        workspace_id,
        prompt_id,
        body.target_version_id,
        body.reason,
        actor,
        _audit(request, actor, workspace_id),
    )


@router.get("/model-configurations", response_model=list[ModelConfigurationOut])
async def get_model_configurations(
    session: Session, actor: CurrentActor
) -> list[ModelConfigurationOut]:
    await _seeded(session)
    return await service.list_model_configurations(session)


# ------------------------------------------------------------------ evaluations
@router.get("/datasets", response_model=list[DatasetOut])
async def get_datasets(
    workspace_id: UUID, session: Session, actor: CurrentActor
) -> list[DatasetOut]:
    await _seeded(session)
    return await service.list_datasets(session, workspace_id)


@router.post("/eval-runs", response_model=EvalRunSummary, status_code=status.HTTP_202_ACCEPTED)
async def post_eval_run(
    request: Request, workspace_id: UUID, body: EvalRunCreate, session: Session, actor: CurrentActor
) -> EvalRunSummary:
    run = await service.create_eval_run(
        session, workspace_id, body, actor, _audit(request, actor, workspace_id)
    )
    settings: Settings = request.app.state.settings
    try:
        enqueue_eval_run(settings, run.id)
    except Exception as exc:
        run.status, run.error = "failed", "enqueue_failed"
        await session.commit()
        logger.error("eval enqueue failed", extra={"error_class": type(exc).__name__})
        raise
    return EvalRunSummary.model_validate(run)


@router.get("/eval-runs", response_model=list[EvalRunSummary])
async def get_eval_runs(
    workspace_id: UUID,
    session: Session,
    actor: CurrentActor,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[EvalRunSummary]:
    return [
        EvalRunSummary.model_validate(r)
        for r in await service.list_eval_runs(session, workspace_id, limit)
    ]


@router.get("/eval-runs/{eval_run_id}", response_model=EvalRunDetail)
async def get_eval_run(
    workspace_id: UUID, eval_run_id: UUID, session: Session, actor: CurrentActor
) -> EvalRunDetail:
    return await service.get_eval_run(session, workspace_id, eval_run_id)


@router.post("/eval-runs/{eval_run_id}/cancel", response_model=EvalActionResponse)
async def post_eval_cancel(
    request: Request, workspace_id: UUID, eval_run_id: UUID, session: Session, actor: CurrentActor
) -> EvalActionResponse:
    run = await service.cancel_eval_run(
        session, workspace_id, eval_run_id, actor, _audit(request, actor, workspace_id)
    )
    return EvalActionResponse(eval_run_id=run.id, status=run.status)


@router.post("/eval-results/{result_id}/human-label", response_model=HumanLabelOut)
async def post_human_label(
    request: Request,
    workspace_id: UUID,
    result_id: UUID,
    body: HumanLabelCreate,
    session: Session,
    actor: CurrentActor,
) -> HumanLabelOut:
    label = await service.add_human_label(
        session, workspace_id, result_id, body, actor, _audit(request, actor, workspace_id)
    )
    return HumanLabelOut.model_validate(label)


@router.get("/eval-runs/{eval_run_id}/label-queue", response_model=list[EvalResultOut])
async def get_label_queue(
    workspace_id: UUID, eval_run_id: UUID, session: Session, actor: CurrentActor
) -> list[EvalResultOut]:
    return (await service.get_eval_run(session, workspace_id, eval_run_id)).label_queue


# ------------------------------------------------------------------ experiments
@router.post("/experiments", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED)
async def post_experiment(
    request: Request,
    workspace_id: UUID,
    body: ExperimentCreate,
    session: Session,
    actor: CurrentActor,
) -> ExperimentOut:
    row = await service.create_experiment(
        session, workspace_id, body, actor, _audit(request, actor, workspace_id)
    )
    return ExperimentOut.model_validate(row)


@router.get("/experiments", response_model=list[ExperimentOut])
async def get_experiments(
    workspace_id: UUID, session: Session, actor: CurrentActor
) -> list[ExperimentOut]:
    return [
        ExperimentOut.model_validate(e)
        for e in await service.list_experiments(session, workspace_id)
    ]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(
    workspace_id: UUID, experiment_id: UUID, session: Session, actor: CurrentActor
) -> ExperimentDetail:
    return await service.get_experiment(session, workspace_id, experiment_id)


async def _experiment_action(
    request: Request,
    workspace_id: UUID,
    experiment_id: UUID,
    action: str,
    session: AsyncSession,
    actor: Actor,
    body: ExperimentStopRequest | None = None,
) -> ExperimentOut:
    row = await service.change_experiment(
        session,
        workspace_id,
        experiment_id,
        action,
        actor,
        _audit(request, actor, workspace_id),
        reason=body.reason if body else None,
        safety=bool(body and body.safety),
    )
    return ExperimentOut.model_validate(row)


@router.post("/experiments/{experiment_id}/start", response_model=ExperimentOut)
async def post_experiment_start(
    request: Request, workspace_id: UUID, experiment_id: UUID, session: Session, actor: CurrentActor
) -> ExperimentOut:
    return await _experiment_action(request, workspace_id, experiment_id, "start", session, actor)


@router.post("/experiments/{experiment_id}/pause", response_model=ExperimentOut)
async def post_experiment_pause(
    request: Request, workspace_id: UUID, experiment_id: UUID, session: Session, actor: CurrentActor
) -> ExperimentOut:
    return await _experiment_action(request, workspace_id, experiment_id, "pause", session, actor)


@router.post("/experiments/{experiment_id}/stop", response_model=ExperimentOut)
async def post_experiment_stop(
    request: Request,
    workspace_id: UUID,
    experiment_id: UUID,
    body: ExperimentStopRequest,
    session: Session,
    actor: CurrentActor,
) -> ExperimentOut:
    return await _experiment_action(
        request, workspace_id, experiment_id, "stop", session, actor, body
    )


# ------------------------------------------------------------------ usage, costs, budgets, health
GroupBy = Literal["workflow", "model", "provider", "node", "user"]


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    workspace_id: UUID,
    session: Session,
    actor: CurrentActor,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    group_by: GroupBy = "workflow",
    workflow: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> UsageResponse:
    since, until = _window(days)
    return await service.usage_report(
        session,
        workspace_id,
        since=since,
        until=until,
        group_by=group_by,
        workflow=workflow,
        model=model,
        provider=provider,
    )


@router.get("/costs", response_model=CostsResponse)
async def get_costs(
    workspace_id: UUID,
    session: Session,
    actor: CurrentActor,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    workflow: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> CostsResponse:
    since, until = _window(days)
    return await service.costs_report(
        session,
        workspace_id,
        since=since,
        until=until,
        workflow=workflow,
        model=model,
        provider=provider,
    )


@router.get("/budgets", response_model=list[BudgetOut])
async def get_budgets(workspace_id: UUID, session: Session, actor: CurrentActor) -> list[BudgetOut]:
    return await service.list_budgets(session, workspace_id)


@router.patch("/budgets/{budget_id}", response_model=BudgetOut)
async def patch_budget(
    request: Request,
    workspace_id: UUID,
    budget_id: UUID,
    body: BudgetPatch,
    session: Session,
    actor: CurrentActor,
) -> BudgetOut:
    return await service.patch_budget(
        session, workspace_id, budget_id, body, actor, _audit(request, actor, workspace_id)
    )


@router.get("/providers/health", response_model=list[ProviderHealthOut])
async def get_provider_health(
    request: Request, workspace_id: UUID, session: Session, actor: CurrentActor
) -> list[ProviderHealthOut]:
    await _seeded(session)
    settings: Settings = request.app.state.settings
    client = None
    store: RedisBreakerStore | None = None
    try:
        client = Redis.from_url(settings.REDIS_URL.get_secret_value(), socket_timeout=2)
        store = RedisBreakerStore(client)
    except Exception as exc:
        logger.warning("breaker store unavailable", extra={"error_class": type(exc).__name__})
    try:
        rows = await service.providers_health(session, workspace_id, store, time.time)
    finally:
        if client is not None:
            client.close()
    return [ProviderHealthOut.model_validate(r) for r in rows]
