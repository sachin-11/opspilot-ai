"""LLMOps business logic. Handlers stay thin; every query is scoped to the workspace."""

import secrets
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.context import Actor
from opspilot_api.auth.models import Role
from opspilot_api.auth.permissions import Permission
from opspilot_api.incidents.service import ConflictError, NotFoundError, ValidationError
from opspilot_api.investigations.prompts import PROMPTS
from opspilot_api.investigations.safety import SYSTEM_SAFETY_INSTRUCTION
from opspilot_api.llmops import experiments as ex
from opspilot_api.llmops import prompt_registry as pr
from opspilot_api.llmops.breaker import BreakerStore, CircuitBreaker
from opspilot_api.llmops.gates import GateReport
from opspilot_api.llmops.human import cohens_kappa
from opspilot_api.llmops.judge import CRITERIA, RUBRIC_VERSION
from opspilot_api.llmops.models import (
    BudgetCounter,
    BudgetEventRow,
    BudgetPolicyRow,
    EvalCaseResult,
    EvalCaseRow,
    EvalDataset,
    EvalRun,
    Experiment,
    ExperimentAssignmentRow,
    HumanLabel,
    ModelConfiguration,
    PricingRateRow,
    PromptDefinition,
    PromptVersion,
    UsageEventRow,
)
from opspilot_api.llmops.reference_agent import VARIANTS
from opspilot_api.llmops.schemas import (
    BudgetOut,
    BudgetPatch,
    CostsResponse,
    DailyCost,
    DatasetOut,
    EvalResultOut,
    EvalRunCreate,
    EvalRunDetail,
    ExperimentAnalysisOut,
    ExperimentCreate,
    ExperimentDetail,
    HumanLabelCreate,
    MetricComparisonOut,
    ModelConfigurationOut,
    PromptActionResponse,
    PromptDetail,
    PromptSummary,
    PromptVersionCreate,
    PromptVersionOut,
    UsageBucket,
    UsageResponse,
)

AuditFn = Callable[[str, str, str | None, dict[str, Any]], Awaitable[None]]
EXPERIMENT_METRICS: dict[str, Callable[[EvalCaseResult], bool]] = {
    "pass_rate": lambda r: bool(r.passed),
    "safety_rate": lambda r: not r.safety_failed,
    "schema_validity_rate": lambda r: r.deterministic_scores.get("schema_validity", 1.0) >= 1.0,
    "citation_validity_rate": lambda r: (
        r.deterministic_scores.get("evidence_id_validity", 1.0) >= 1.0
    ),
    "abstention_rate": lambda r: r.deterministic_scores.get("required_abstention", 1.0) >= 1.0,
}
ASSUMPTIONS = [
    "Units are independent and were assigned by a stable hash.",
    "Two-sided two-proportion z-test with Wald confidence intervals; no p-value is shown when an "
    "arm has fewer than 5 successes or failures.",
    "Guardrails use Bonferroni-adjusted levels.",
    "Fixed-horizon test: p-values are valid only at the pre-registered minimum sample size; "
    "repeated looks are counted and flagged.",
    "A significant primary metric never promotes a variant on its own; a person decides.",
]
DEFAULT_BUDGETS = [
    ("investigation", 2.0),
    ("remediation", 2.0),
    ("daily_workspace", 10.0),
    ("monthly_workspace", 100.0),
]


# ------------------------------------------------------------------ prompts
def _template(name: str) -> str:
    d = PROMPTS[name]
    return (
        f"{SYSTEM_SAFETY_INSTRUCTION}\n\nTask: {d.instruction}\n"
        f"Output must match {d.output_schema.__name__}."
    )


async def ensure_prompts_seeded(session: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    count = await session.scalar(
        select(func.count())
        .select_from(PromptDefinition)
        .where(PromptDefinition.workspace_id == workspace_id)
    )
    if count:
        return
    for name, definition in PROMPTS.items():
        pd = PromptDefinition(
            workspace_id=workspace_id,
            name=name,
            workflow="investigation",
            node_name=name,
            description=definition.instruction,
            status="active",
        )
        session.add(pd)
        await session.flush()
        system = _template(name)
        schema: dict[str, Any] = {"fields": {}}
        out_schema = definition.output_schema.model_json_schema()
        session.add(
            PromptVersion(
                prompt_definition_id=pd.id,
                semantic_version="1.0.0",
                status="published",
                is_default=True,
                system_template=system,
                developer_template=None,
                input_schema=schema,
                output_schema=out_schema,
                change_notes="Seeded from the built-in Module 2 prompt.",
                content_hash=pr.compute_hash(system, None, schema, out_schema),
                created_by_user_id=user_id,
                published_by_user_id=user_id,
                published_at=datetime.now(UTC),
            )
        )
    await session.commit()


def _version_out(v: PromptVersion) -> PromptVersionOut:
    return PromptVersionOut.model_validate(v)


async def _definition(
    session: AsyncSession, workspace_id: UUID, prompt_id: UUID
) -> PromptDefinition:
    pd = await session.scalar(
        select(PromptDefinition).where(
            PromptDefinition.id == prompt_id, PromptDefinition.workspace_id == workspace_id
        )
    )
    if pd is None:
        raise NotFoundError("prompt not found")
    return pd


async def list_prompts(
    session: AsyncSession, workspace_id: UUID, user_id: UUID
) -> list[PromptSummary]:
    await ensure_prompts_seeded(session, workspace_id, user_id)
    defs = (
        await session.scalars(
            select(PromptDefinition)
            .where(PromptDefinition.workspace_id == workspace_id)
            .order_by(PromptDefinition.name)
        )
    ).all()
    out: list[PromptSummary] = []
    for pd in defs:
        versions = (
            await session.scalars(
                select(PromptVersion).where(PromptVersion.prompt_definition_id == pd.id)
            )
        ).all()
        default = next((v.semantic_version for v in versions if v.is_default), None)
        out.append(
            PromptSummary(
                id=pd.id,
                name=pd.name,
                workflow=pd.workflow,
                node_name=pd.node_name,
                description=pd.description,
                status=pd.status,
                default_version=default,
                version_count=len(versions),
                updated_at=pd.updated_at,
            )
        )
    return out


async def get_prompt(session: AsyncSession, workspace_id: UUID, prompt_id: UUID) -> PromptDetail:
    pd = await _definition(session, workspace_id, prompt_id)
    versions = (
        await session.scalars(
            select(PromptVersion)
            .where(PromptVersion.prompt_definition_id == pd.id)
            .order_by(PromptVersion.created_at.desc())
        )
    ).all()
    default = next((v.semantic_version for v in versions if v.is_default), None)
    return PromptDetail(
        id=pd.id,
        name=pd.name,
        workflow=pd.workflow,
        node_name=pd.node_name,
        description=pd.description,
        status=pd.status,
        default_version=default,
        version_count=len(versions),
        updated_at=pd.updated_at,
        versions=[_version_out(v) for v in versions],
    )


def _to_domain(v: PromptVersion) -> pr.PromptVersion:
    return pr.PromptVersion(
        id=str(v.id),
        prompt_definition_id=str(v.prompt_definition_id),
        semantic_version=v.semantic_version,
        system_template=v.system_template,
        developer_template=v.developer_template,
        input_schema=v.input_schema,
        output_schema=v.output_schema,
        change_notes=v.change_notes,
        created_by_user_id=str(v.created_by_user_id),
        status=v.status,
        is_default=v.is_default,
        content_hash=v.content_hash,
    )


async def create_prompt_version(
    session: AsyncSession,
    workspace_id: UUID,
    prompt_id: UUID,
    body: PromptVersionCreate,
    actor: Actor,
    audit: AuditFn,
) -> PromptVersionOut:
    actor.require(Permission.LLMOPS_PROMPTS_MANAGE)
    pd = await _definition(session, workspace_id, prompt_id)
    existing = (
        await session.scalars(
            select(PromptVersion.semantic_version).where(
                PromptVersion.prompt_definition_id == pd.id
            )
        )
    ).all()
    try:
        domain = pr.create_version(
            version_id=str(UUID(int=0)),
            definition_id=str(pd.id),
            existing_versions=list(existing),
            system_template=body.system_template,
            developer_template=body.developer_template,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            change_notes=body.change_notes,
            user_id=str(actor.user_id),
            bump=body.bump,
        )
    except pr.PromptError as exc:
        raise ValidationError(str(exc)) from exc
    row = PromptVersion(
        prompt_definition_id=pd.id,
        semantic_version=domain.semantic_version,
        status="candidate",
        system_template=domain.system_template,
        developer_template=domain.developer_template,
        input_schema=domain.input_schema,
        output_schema=domain.output_schema,
        change_notes=domain.change_notes,
        content_hash=domain.content_hash,
        created_by_user_id=actor.user_id,
    )
    session.add(row)
    await session.commit()
    await audit(
        "prompt.version_create",
        "prompt_version",
        str(row.id),
        {"prompt": pd.name, "version": row.semantic_version, "hash": row.content_hash},
    )
    return _version_out(row)


async def _gate_for_run(session: AsyncSession, workspace_id: UUID, run_id: UUID) -> EvalRun:
    run = await session.scalar(
        select(EvalRun).where(EvalRun.id == run_id, EvalRun.workspace_id == workspace_id)
    )
    if run is None:
        raise NotFoundError("evaluation run not found")
    if run.status != "completed" or run.gate_report is None:
        raise ConflictError("the evaluation run has not completed with a gate report")
    return run


async def publish_prompt(
    session: AsyncSession,
    workspace_id: UUID,
    prompt_id: UUID,
    version_id: UUID,
    eval_run_id: UUID,
    override_reason: str | None,
    actor: Actor,
    audit: AuditFn,
) -> PromptActionResponse:
    actor.require(Permission.LLMOPS_PROMPTS_MANAGE)
    pd = await _definition(session, workspace_id, prompt_id)
    version = await session.scalar(
        select(PromptVersion).where(
            PromptVersion.id == version_id, PromptVersion.prompt_definition_id == pd.id
        )
    )
    if version is None:
        raise NotFoundError("prompt version not found")
    run = await _gate_for_run(session, workspace_id, eval_run_id)
    if str(version.id) not in {str(v) for v in (run.prompt_versions or {}).values()}:
        raise ConflictError("the evaluation run did not evaluate this prompt version")
    stored = run.gate_report or {}
    report = GateReport.model_validate(
        {"config_version": stored["config_version"], "results": stored["results"]}
    )
    siblings_rows = (
        await session.scalars(
            select(PromptVersion).where(PromptVersion.prompt_definition_id == pd.id)
        )
    ).all()
    try:
        published, cleared = pr.publish(
            _to_domain(version),
            [_to_domain(s) for s in siblings_rows if s.id != version.id],
            report,
            publisher_id=str(actor.user_id),
            override_privileged=actor.role == Role.OWNER.value,  # overrides are owner-only
            override_reason=override_reason,
        )
    except pr.PublicationBlocked as exc:
        await audit(
            "prompt.publish_blocked",
            "prompt_version",
            str(version.id),
            {"reason": str(exc)[:200], "eval_run_id": str(eval_run_id)},
        )
        raise ConflictError(str(exc)) from exc
    cleared_ids = {c.id for c in cleared if not c.is_default}
    for sib in siblings_rows:
        if sib.is_default and str(sib.id) in cleared_ids:
            sib.is_default = False
    await session.flush()  # clear the old default before setting the new one (unique index)
    version.status, version.is_default = "published", True
    version.published_by_user_id, version.published_at = actor.user_id, datetime.now(UTC)
    version.override_reason = published.override_reason
    version.gate_report, version.eval_run_id = run.gate_report, run.id
    await session.commit()
    overridden = [r["gate"] for r in run.gate_report["results"] if r["status"] == "fail"]  # type: ignore[index]
    await audit(
        "prompt.publish",
        "prompt_version",
        str(version.id),
        {
            "prompt": pd.name,
            "version": version.semantic_version,
            "eval_run_id": str(run.id),
            "override": bool(published.override_reason),
            "overridden_gates": overridden,
            "override_reason": (published.override_reason or "")[:200],
        },
    )
    return PromptActionResponse(
        prompt_id=pd.id,
        version_id=version.id,
        semantic_version=version.semantic_version,
        status="published",
        is_default=True,
        overridden_gates=overridden if published.override_reason else [],
    )


async def rollback_prompt(
    session: AsyncSession,
    workspace_id: UUID,
    prompt_id: UUID,
    target_id: UUID,
    reason: str,
    actor: Actor,
    audit: AuditFn,
) -> PromptActionResponse:
    actor.require(Permission.LLMOPS_PROMPTS_MANAGE)
    pd = await _definition(session, workspace_id, prompt_id)
    rows = (
        await session.scalars(
            select(PromptVersion).where(PromptVersion.prompt_definition_id == pd.id)
        )
    ).all()
    target = next((r for r in rows if r.id == target_id), None)
    if target is None:
        raise NotFoundError("prompt version not found")
    try:
        pr.rollback(_to_domain(target), [_to_domain(r) for r in rows])
    except pr.PromptError as exc:
        raise ConflictError(str(exc)) from exc
    for r in rows:
        if r.is_default and r.id != target.id:
            r.is_default = False
    await session.flush()
    target.is_default = True  # history (content, status, hash) is never rewritten
    await session.commit()
    await audit(
        "prompt.rollback",
        "prompt_version",
        str(target.id),
        {"prompt": pd.name, "to_version": target.semantic_version, "reason": reason[:200]},
    )
    return PromptActionResponse(
        prompt_id=pd.id,
        version_id=target.id,
        semantic_version=target.semantic_version,
        status=target.status,
        is_default=True,
    )


# ------------------------------------------------------------------ model configurations
async def list_model_configurations(session: AsyncSession) -> list[ModelConfigurationOut]:
    rows = (
        await session.execute(
            select(ModelConfiguration, PricingRateRow)
            .outerjoin(PricingRateRow, PricingRateRow.id == ModelConfiguration.pricing_rate_id)
            .order_by(ModelConfiguration.config_key, ModelConfiguration.version.desc())
        )
    ).all()
    out = []
    for cfg, rate in rows:
        data = {c.name: getattr(cfg, c.name) for c in ModelConfiguration.__table__.columns}
        out.append(
            ModelConfigurationOut(
                **{k: v for k, v in data.items() if k in ModelConfigurationOut.model_fields},
                pricing_rate_version=rate.version if rate else None,
                input_per_million=float(rate.input_per_million) if rate else None,
                output_per_million=float(rate.output_per_million) if rate else None,
            )
        )
    return out


# ------------------------------------------------------------------ datasets and eval runs
async def list_datasets(session: AsyncSession, workspace_id: UUID) -> list[DatasetOut]:
    rows = (
        await session.scalars(
            select(EvalDataset)
            .where((EvalDataset.workspace_id == workspace_id) | EvalDataset.workspace_id.is_(None))
            .order_by(EvalDataset.name, EvalDataset.version.desc())
        )
    ).all()
    out = []
    for d in rows:
        cases = (
            await session.execute(
                select(EvalCaseRow.suite, EvalCaseRow.split).where(EvalCaseRow.dataset_id == d.id)
            )
        ).all()
        suites: dict[str, int] = defaultdict(int)
        splits: dict[str, int] = defaultdict(int)
        for suite, split in cases:
            suites[suite] += 1
            splits[split] += 1
        out.append(
            DatasetOut(
                id=d.id,
                name=d.name,
                workflow=d.workflow,
                description=d.description,
                version=d.version,
                status=d.status,
                content_hash=d.content_hash,
                case_count=d.case_count,
                suite_counts=dict(suites),
                split_counts=dict(splits),
                published_at=d.published_at,
            )
        )
    return out


async def create_eval_run(
    session: AsyncSession,
    workspace_id: UUID,
    body: EvalRunCreate,
    actor: Actor,
    audit: AuditFn,
) -> EvalRun:
    actor.require(Permission.LLMOPS_EVALS_RUN)
    dataset = await session.scalar(
        select(EvalDataset).where(
            EvalDataset.id == body.dataset_id,
            (EvalDataset.workspace_id == workspace_id) | EvalDataset.workspace_id.is_(None),
        )
    )
    if dataset is None or dataset.status != "published":
        raise NotFoundError("published dataset not found")
    if body.variant not in VARIANTS:
        raise ValidationError("unknown variant")
    if body.run_type in ("ci", "candidate_comparison") and body.purpose != "gate":
        raise ValidationError("ci and candidate_comparison runs must use the gate purpose")
    if body.purpose == "development" and body.run_type in ("ci", "candidate_comparison"):
        raise ValidationError("the test split cannot be used for development runs")
    run = EvalRun(
        workspace_id=workspace_id,
        dataset_id=dataset.id,
        dataset_version=dataset.version,
        prompt_versions={k: str(v) for k, v in body.prompt_versions.items()},
        model_configuration_id=body.model_configuration_id,
        routing_policy_version=None,
        run_type=body.run_type,
        purpose=body.purpose,
        variant=body.variant,
        repeat_count=body.repeat_count,
        seed_configuration={
            "seed": 0,
            "control_variant": body.control_variant,
            "deterministic": True,
        },
        status="queued",
        created_by_user_id=actor.user_id,
    )
    session.add(run)
    await session.commit()
    await audit(
        "eval.run_create",
        "eval_run",
        str(run.id),
        {"variant": body.variant, "run_type": body.run_type, "purpose": body.purpose},
    )
    return run


async def list_eval_runs(
    session: AsyncSession, workspace_id: UUID, limit: int = 50
) -> list[EvalRun]:
    return list(
        (
            await session.scalars(
                select(EvalRun)
                .where((EvalRun.workspace_id == workspace_id))
                .order_by(EvalRun.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def _get_run(session: AsyncSession, workspace_id: UUID, run_id: UUID) -> EvalRun:
    run = await session.scalar(
        select(EvalRun).where(EvalRun.id == run_id, EvalRun.workspace_id == workspace_id)
    )
    if run is None:
        raise NotFoundError("evaluation run not found")
    return run


def _result_out(r: EvalCaseResult, labelled: set[UUID]) -> EvalResultOut:
    return EvalResultOut(
        id=r.id,
        case_key=r.case_key,
        suite=r.suite,
        variant=r.variant,
        repeat_index=r.repeat_index,
        passed=r.passed,
        safety_failed=r.safety_failed,
        failure_reasons=r.failure_reasons,
        deterministic_scores=r.deterministic_scores,
        llm_judge_scores=r.llm_judge_scores,
        tool_calls=r.tool_calls,
        evidence_citations=r.evidence_citations,
        latency_ms=r.latency_ms,
        tokens=r.tokens,
        estimated_cost_usd=float(r.estimated_cost_usd)
        if r.estimated_cost_usd is not None
        else None,
        labelled=r.id in labelled,
    )


async def get_eval_run(session: AsyncSession, workspace_id: UUID, run_id: UUID) -> EvalRunDetail:
    run = await _get_run(session, workspace_id, run_id)
    results = (
        await session.scalars(
            select(EvalCaseResult)
            .where(EvalCaseResult.eval_run_id == run.id)
            .order_by(EvalCaseResult.case_key, EvalCaseResult.repeat_index)
        )
    ).all()
    labelled = set(
        (
            await session.scalars(
                select(HumanLabel.result_id).where(HumanLabel.workspace_id == workspace_id)
            )
        ).all()
    )
    summary = run.summary or {}
    sums: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for grader, score in r.deterministic_scores.items():
            sums[grader].append(float(score))
    failed = [_result_out(r, labelled) for r in results if not r.passed and r.repeat_index == 0]
    queue = [
        _result_out(r, labelled)
        for r in results
        if r.repeat_index == 0 and r.id not in labelled and (not r.passed or r.safety_failed)
    ]
    base = {c.name: getattr(run, c.name) for c in EvalRun.__table__.columns}
    return EvalRunDetail(
        **{
            k: v
            for k, v in base.items()
            if k in EvalRunDetail.model_fields
            and k
            not in {
                "metrics",
                "violations",
                "by_suite",
                "by_tag",
                "flaky_cases",
                "gate_report",
                "comparison",
                "failed_results",
                "grader_breakdown",
                "label_queue",
                "markdown_report",
                "estimated_cost_usd",
            }
        },
        estimated_cost_usd=float(run.estimated_cost_usd)
        if run.estimated_cost_usd is not None
        else None,
        metrics=summary.get("metrics", {}),
        violations=summary.get("violations", {}),
        by_suite=summary.get("by_suite", {}),
        by_tag=summary.get("by_tag", {}),
        flaky_cases=summary.get("flaky_cases", []),
        gate_report=run.gate_report,
        comparison=summary.get("comparison"),
        failed_results=failed,
        grader_breakdown={k: round(sum(v) / len(v), 6) for k, v in sorted(sums.items())},
        label_queue=queue[:50],
        markdown_report=summary.get("markdown_report"),
    )


async def cancel_eval_run(
    session: AsyncSession, workspace_id: UUID, run_id: UUID, actor: Actor, audit: AuditFn
) -> EvalRun:
    actor.require(Permission.LLMOPS_EVALS_RUN)
    run = await _get_run(session, workspace_id, run_id)
    if run.status not in ("queued", "running"):
        raise ConflictError(f"a {run.status} run cannot be cancelled")
    run.status, run.completed_at = "cancelled", datetime.now(UTC)
    await session.commit()
    await audit("eval.run_cancel", "eval_run", str(run.id), {})
    return run


async def add_human_label(
    session: AsyncSession,
    workspace_id: UUID,
    result_id: UUID,
    body: HumanLabelCreate,
    actor: Actor,
    audit: AuditFn,
) -> HumanLabel:
    actor.require(Permission.LLMOPS_EVALS_LABEL)
    result = await session.scalar(
        select(EvalCaseResult)
        .join(EvalRun, EvalRun.id == EvalCaseResult.eval_run_id)
        .where(EvalCaseResult.id == result_id, EvalRun.workspace_id == workspace_id)
    )
    if result is None:
        raise NotFoundError("evaluation result not found")
    if body.rubric_version != RUBRIC_VERSION:
        raise ValidationError(f"rubric_version must be {RUBRIC_VERSION}")
    unknown = set(body.criterion_scores) - set(CRITERIA)
    if unknown or any(not 1 <= s <= 5 for s in body.criterion_scores.values()):
        raise ValidationError("criterion scores must use the rubric criteria on a 1-5 scale")
    # Repeats of one case are the same answer: one reviewer must not label them twice.
    sibling_ids = (
        await session.scalars(
            select(EvalCaseResult.id).where(
                EvalCaseResult.eval_run_id == result.eval_run_id,
                EvalCaseResult.case_key == result.case_key,
                EvalCaseResult.variant == result.variant,
                EvalCaseResult.id != result.id,
            )
        )
    ).all()
    if sibling_ids:
        dup = await session.scalar(
            select(HumanLabel.id).where(
                HumanLabel.result_id.in_(sibling_ids), HumanLabel.reviewer_user_id == actor.user_id
            )
        )
        if dup is not None:
            raise ConflictError("you already labelled another repeat of this case")
    existing = await session.scalar(
        select(HumanLabel).where(
            HumanLabel.result_id == result_id, HumanLabel.reviewer_user_id == actor.user_id
        )
    )
    before: dict[str, Any] | None = None
    if existing is None:
        label = HumanLabel(
            workspace_id=workspace_id, result_id=result_id, reviewer_user_id=actor.user_id
        )
        session.add(label)
    else:
        label = existing
        before = {"passed": label.passed, "scores": label.criterion_scores}
    label.rubric_version, label.criterion_scores = body.rubric_version, body.criterion_scores
    label.overall_preference, label.passed, label.notes = (
        body.overall_preference,
        body.passed,
        body.notes,
    )
    await session.commit()
    await session.refresh(label)  # server-side updated_at must be loaded before serialization
    await audit(
        "eval.label_update" if before else "eval.label_create",
        "human_label",
        str(label.id),
        {"result_id": str(result_id), "passed": body.passed, "previous": before or {}},
    )
    return label


async def label_agreement(
    session: AsyncSession, workspace_id: UUID
) -> dict[str, float | int | None]:
    """Human-vs-deterministic agreement across labelled results (a calibration signal)."""
    rows = (
        await session.execute(
            select(HumanLabel.passed, EvalCaseResult.passed)
            .join(EvalCaseResult, EvalCaseResult.id == HumanLabel.result_id)
            .where(HumanLabel.workspace_id == workspace_id)
        )
    ).all()
    if len(rows) < 2:
        return {"labelled": len(rows), "kappa": None}
    return {
        "labelled": len(rows),
        "kappa": cohens_kappa([r[0] for r in rows], [r[1] for r in rows]),
    }


# ------------------------------------------------------------------ experiments
def _spec(e: Experiment) -> ex.ExperimentSpec:
    return ex.ExperimentSpec(
        id=str(e.id),
        workflow=e.workflow,
        unit=e.unit,  # type: ignore[arg-type]
        allocation_percentage=e.allocation_percentage,
        primary_metric=e.primary_metric,
        guardrail_metrics=tuple(e.guardrail_metrics),
        minimum_sample_size=e.minimum_sample_size,
        status=e.status,
        allowed_risks=tuple(e.eligibility_rules.get("allowed_risks", [])),
        salt=e.salt,
    )


async def create_experiment(
    session: AsyncSession, workspace_id: UUID, body: ExperimentCreate, actor: Actor, audit: AuditFn
) -> Experiment:
    actor.require(Permission.LLMOPS_EXPERIMENTS_MANAGE)
    for metric in (body.primary_metric, *body.guardrail_metrics):
        if metric not in EXPERIMENT_METRICS:
            raise ValidationError(
                f"unknown metric {metric!r}; choose from {sorted(EXPERIMENT_METRICS)}"
            )
    if body.primary_metric in body.guardrail_metrics:
        raise ValidationError("the primary metric cannot also be a guardrail")
    row = Experiment(
        workspace_id=workspace_id,
        name=body.name,
        workflow=body.workflow,
        status="draft",
        control_variant=body.control_variant.model_dump(mode="json"),
        candidate_variant=body.candidate_variant.model_dump(mode="json"),
        allocation_percentage=body.allocation_percentage,
        unit=body.unit,
        eligibility_rules={"allowed_risks": list(body.allowed_risks), "shadow_first": True},
        primary_metric=body.primary_metric,
        guardrail_metrics=body.guardrail_metrics,
        minimum_sample_size=body.minimum_sample_size,
        salt=secrets.token_hex(8),
        created_by_user_id=actor.user_id,
    )
    try:
        ex.validate_for_start(_spec(row))
    except ex.ExperimentError as exc:
        raise ValidationError(str(exc)) from exc
    session.add(row)
    await session.commit()
    await audit("experiment.create", "experiment", str(row.id), {"name": body.name})
    return row


async def list_experiments(session: AsyncSession, workspace_id: UUID) -> list[Experiment]:
    return list(
        (
            await session.scalars(
                select(Experiment)
                .where(Experiment.workspace_id == workspace_id)
                .order_by(Experiment.created_at.desc())
            )
        ).all()
    )


async def _experiment(session: AsyncSession, workspace_id: UUID, experiment_id: UUID) -> Experiment:
    row = await session.scalar(
        select(Experiment).where(
            Experiment.id == experiment_id, Experiment.workspace_id == workspace_id
        )
    )
    if row is None:
        raise NotFoundError("experiment not found")
    return row


async def experiment_analysis(
    session: AsyncSession, workspace_id: UUID, e: Experiment, *, count_look: bool
) -> ExperimentAnalysisOut | None:
    arms_variants = {
        "control": e.control_variant["eval_variant"],
        "candidate": e.candidate_variant["eval_variant"],
    }
    rows = (
        await session.scalars(
            select(EvalCaseResult)
            .join(EvalRun, EvalRun.id == EvalCaseResult.eval_run_id)
            .where(
                EvalRun.workspace_id == workspace_id,
                EvalRun.status == "completed",
                EvalRun.run_type.in_(("shadow", "production_sample")),
                EvalCaseResult.repeat_index == 0,
            )
        )
    ).all()
    arms: dict[str, dict[ex.Variant, tuple[int, int]]] = {}
    for metric in (e.primary_metric, *e.guardrail_metrics):
        fn = EXPERIMENT_METRICS[metric]
        arm: dict[ex.Variant, tuple[int, int]] = {}
        for label in get_args(ex.Variant):
            picked = [r for r in rows if r.variant == arms_variants[label]]
            arm[label] = (sum(1 for r in picked if fn(r)), len(picked))
        arms[metric] = arm
    if count_look:
        e.analyses_count += 1
        await session.commit()
    safety_bad = sum(1 for r in rows if r.variant == arms_variants["candidate"] and r.safety_failed)
    analysis = ex.analyze(
        _spec(e),
        arms,
        safety_violations_candidate=safety_bad,
        analyses_so_far=max(1, e.analyses_count),
    )
    return ExperimentAnalysisOut(
        control_n=analysis.sample.control_n,
        candidate_n=analysis.sample.candidate_n,
        minimum_sample_size=e.minimum_sample_size,
        sample_size_reached=analysis.sample.reached,
        metrics=[
            MetricComparisonOut(
                metric=m.metric,
                guardrail=m.guardrail,
                control_rate=m.comparison.control_rate,
                candidate_rate=m.comparison.candidate_rate,
                difference=m.comparison.difference,
                ci_low=m.comparison.interval.low,
                ci_high=m.comparison.interval.high,
                p_value=m.comparison.p_value,
                significant=m.significant,
                regressed=m.regressed,
                note=m.comparison.note,
            )
            for m in analysis.metrics
        ],
        safety_stop=analysis.safety_stop,
        warnings=analysis.warnings,
        promotable_by_data=analysis.promotable_by_data,
        promotion_note="Even when the data supports the candidate, promotion is a manual, audited "
        "decision; this system never promotes a variant automatically.",
        assumptions=ASSUMPTIONS,
    )


async def get_experiment(
    session: AsyncSession, workspace_id: UUID, experiment_id: UUID
) -> ExperimentDetail:
    e = await _experiment(session, workspace_id, experiment_id)
    counts = dict(
        (
            await session.execute(
                select(ExperimentAssignmentRow.variant, func.count())
                .where(ExperimentAssignmentRow.experiment_id == e.id)
                .group_by(ExperimentAssignmentRow.variant)
            )
        ).all()
    )
    analysis = await experiment_analysis(session, workspace_id, e, count_look=False)
    base = ExperimentDetail.model_validate(
        {
            **{
                c.name: getattr(e, c.name)
                for c in Experiment.__table__.columns
                if c.name in ExperimentDetail.model_fields
            },
            "assignment_counts": counts,
            "analysis": analysis,
        }
    )
    return base


async def change_experiment(
    session: AsyncSession,
    workspace_id: UUID,
    experiment_id: UUID,
    action: str,
    actor: Actor,
    audit: AuditFn,
    *,
    reason: str | None = None,
    safety: bool = False,
) -> Experiment:
    actor.require(Permission.LLMOPS_EXPERIMENTS_MANAGE)
    e = await _experiment(session, workspace_id, experiment_id)
    target = {
        "start": "running",
        "pause": "paused",
        "stop": "cancelled" if safety else "completed",
    }[action]
    try:
        if action == "start":
            ex.validate_for_start(_spec(e))
        ex.transition(e.status, target)
    except ex.ExperimentError as exc:
        raise ConflictError(str(exc)) from exc
    e.status = target
    now = datetime.now(UTC)
    if action == "start" and e.started_at is None:
        e.started_at = now
    if action == "stop":
        e.ended_at, e.stop_reason = now, (("SAFETY: " if safety else "") + (reason or ""))[:200]
    await session.commit()
    await audit(
        f"experiment.{action}",
        "experiment",
        str(e.id),
        {"status": e.status, "safety": safety, "reason": (reason or "")[:200]},
    )
    return e


async def assign_for_unit(
    session: AsyncSession, e: Experiment, unit_id: str, *, node: str, risk: str
) -> ex.Assignment | None:
    """Persist-once assignment used by the runtime. Returns None when ineligible."""
    spec = _spec(e)
    existing = await session.scalar(
        select(ExperimentAssignmentRow).where(
            ExperimentAssignmentRow.experiment_id == e.id,
            ExperimentAssignmentRow.unit_id == unit_id,
        )
    )
    if existing is not None:
        return ex.Assignment(str(e.id), unit_id, existing.variant, existing.assignment_hash)  # type: ignore[arg-type]
    if not ex.is_eligible(spec, workflow=e.workflow, node=node, risk=risk):
        return None
    variant = ex.assign_variant(spec, unit_id)
    await session.execute(
        pg_insert(ExperimentAssignmentRow)
        .values(
            experiment_id=e.id,
            workspace_id=e.workspace_id,
            unit_type=e.unit,
            unit_id=unit_id,
            variant=variant,
            assignment_hash=ex.assignment_hash(spec, unit_id),
        )
        .on_conflict_do_nothing(index_elements=["experiment_id", "unit_id"])
    )
    await session.commit()
    row = await session.scalar(
        select(ExperimentAssignmentRow).where(
            ExperimentAssignmentRow.experiment_id == e.id,
            ExperimentAssignmentRow.unit_id == unit_id,
        )
    )
    if row is None:
        raise ConflictError("assignment could not be persisted")
    return ex.Assignment(str(e.id), unit_id, row.variant, row.assignment_hash)  # type: ignore[arg-type]


# ------------------------------------------------------------------ usage and costs
def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def _bucket(key: str, rows: list[UsageEventRow]) -> UsageBucket:
    return UsageBucket(
        key=key,
        requests=sum(r.request_count for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cost_usd=round(
            sum(float(r.estimated_cost_usd) for r in rows if r.estimated_cost_usd is not None), 10
        ),
        cost_unavailable_requests=sum(
            r.request_count for r in rows if r.estimated_cost_usd is None
        ),
    )


async def _usage_rows(
    session: AsyncSession,
    workspace_id: UUID,
    since: datetime,
    until: datetime,
    workflow: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> list[UsageEventRow]:
    stmt = select(UsageEventRow).where(
        UsageEventRow.workspace_id == workspace_id,
        UsageEventRow.occurred_at >= since,
        UsageEventRow.occurred_at < until,
    )
    if workflow:
        stmt = stmt.where(UsageEventRow.workflow == workflow)
    if model:
        stmt = stmt.where(UsageEventRow.model == model)
    if provider:
        stmt = stmt.where(UsageEventRow.provider == provider)
    return list((await session.scalars(stmt)).all())


def _group(rows: list[UsageEventRow], attr: str) -> list[UsageBucket]:
    groups: dict[str, list[UsageEventRow]] = defaultdict(list)
    for r in rows:
        groups[str(getattr(r, attr) or "unattributed")].append(r)
    return [_bucket(k, v) for k, v in sorted(groups.items())]


async def usage_report(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    since: datetime,
    until: datetime,
    group_by: str,
    workflow: str | None,
    model: str | None,
    provider: str | None,
) -> UsageResponse:
    attr = {
        "workflow": "workflow",
        "model": "model",
        "provider": "provider",
        "node": "node_name",
        "user": "user_id",
    }[group_by]
    rows = await _usage_rows(session, workspace_id, since, until, workflow, model, provider)
    latencies = [r.latency_ms for r in rows if r.status == "succeeded"]
    errors = sum(1 for r in rows if r.status != "succeeded")
    fallbacks = sum(1 for r in rows if r.status == "fallback")
    return UsageResponse(
        group_by=group_by,
        since=since,
        until=until,
        total_requests=sum(r.request_count for r in rows),
        total_input_tokens=sum(r.input_tokens for r in rows),
        total_output_tokens=sum(r.output_tokens for r in rows),
        latency_p50_ms=_pct(latencies, 50),
        latency_p95_ms=_pct(latencies, 95),
        fallback_rate=(fallbacks / len(rows)) if rows else None,
        error_rate=(errors / len(rows)) if rows else None,
        buckets=_group(rows, attr),
    )


async def costs_report(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    since: datetime,
    until: datetime,
    workflow: str | None,
    model: str | None,
    provider: str | None,
) -> CostsResponse:
    rows = await _usage_rows(session, workspace_id, since, until, workflow, model, provider)
    by_day: dict[str, list[UsageEventRow]] = defaultdict(list)
    for r in rows:
        by_day[r.occurred_at.astimezone(UTC).strftime("%Y-%m-%d")].append(r)
    daily = [
        DailyCost(
            day=d,
            cost_usd=_bucket(d, v).cost_usd,
            cost_unavailable_requests=_bucket(d, v).cost_unavailable_requests,
        )
        for d, v in sorted(by_day.items())
    ]
    per_inv: dict[UUID, float] = defaultdict(float)
    per_rem: dict[UUID, float] = defaultdict(float)
    for r in rows:
        if r.estimated_cost_usd is None:
            continue
        if r.investigation_id:
            per_inv[r.investigation_id] += float(r.estimated_cost_usd)
        if r.remediation_id:
            per_rem[r.remediation_id] += float(r.estimated_cost_usd)
    reserved = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(amount_usd),0)::float, COALESCE(SUM(actual_usd),0)::float "
                "FROM budget_reservations WHERE workspace_id=:ws AND created_at >= :s "
                "AND created_at < :u AND status='reconciled'"
            ),
            {"ws": workspace_id, "s": since, "u": until},
        )
    ).one()
    events = (
        await session.scalars(
            select(BudgetEventRow)
            .where(
                BudgetEventRow.workspace_id == workspace_id,
                BudgetEventRow.created_at >= since,
                BudgetEventRow.kind.in_(("soft_warning", "hard_block", "override")),
            )
            .order_by(BudgetEventRow.created_at.desc())
            .limit(50)
        )
    ).all()
    total = _bucket("all", rows)
    return CostsResponse(
        since=since,
        until=until,
        daily=daily,
        by_workflow=_group(rows, "workflow"),
        by_model=_group(rows, "model"),
        by_provider=_group(rows, "provider"),
        by_user=_group(rows, "user_id"),
        cost_per_investigation_usd=(sum(per_inv.values()) / len(per_inv)) if per_inv else None,
        cost_per_completed_remediation_usd=(sum(per_rem.values()) / len(per_rem))
        if per_rem
        else None,
        reserved_vs_actual={"reserved_usd": reserved[0], "actual_usd": reserved[1]},
        limit_events=[
            {
                "kind": ev.kind,
                "scope_key": ev.scope_key,
                "detail": ev.detail,
                "at": ev.created_at.isoformat(),
            }
            for ev in events
        ],
        total_cost_usd=total.cost_usd,
        unavailable_cost_requests=total.cost_unavailable_requests,
    )


# ------------------------------------------------------------------ budgets and provider health
async def ensure_default_budgets(session: AsyncSession, workspace_id: UUID) -> None:
    count = await session.scalar(
        select(func.count())
        .select_from(BudgetPolicyRow)
        .where(BudgetPolicyRow.workspace_id == workspace_id)
    )
    if count:
        return
    for scope, limit in DEFAULT_BUDGETS:
        session.add(
            BudgetPolicyRow(
                workspace_id=workspace_id,
                scope=scope,
                workflow="*",
                hard_limit_usd=limit,
                soft_fraction=0.8,
                enabled=True,
            )
        )
    await session.commit()


async def list_budgets(session: AsyncSession, workspace_id: UUID) -> list[BudgetOut]:
    await ensure_default_budgets(session, workspace_id)
    policies = (
        await session.scalars(
            select(BudgetPolicyRow)
            .where(BudgetPolicyRow.workspace_id == workspace_id)
            .order_by(BudgetPolicyRow.scope)
        )
    ).all()
    counters = (
        await session.scalars(
            select(BudgetCounter).where(BudgetCounter.workspace_id == workspace_id)
        )
    ).all()
    now = datetime.now(UTC)
    out = []
    for p in policies:
        prefix = {
            "daily_workspace": f"daily_workspace:{workspace_id}:{now:%Y-%m-%d}",
            "monthly_workspace": f"monthly_workspace:{workspace_id}:{now:%Y-%m}",
        }.get(p.scope)
        if prefix:
            matched = [c for c in counters if c.scope_key == prefix]
        else:  # per-investigation / per-remediation budgets: show the most-used window
            matched = sorted(
                (c for c in counters if c.scope_key.startswith(p.scope + ":")),
                key=lambda c: float(c.spent_usd + c.reserved_usd),
                reverse=True,
            )[:1]
        spent = float(matched[0].spent_usd) if matched else 0.0
        reserved = float(matched[0].reserved_usd) if matched else 0.0
        unmetered = matched[0].unmetered_calls if matched else 0
        hard, soft = float(p.hard_limit_usd), float(p.hard_limit_usd) * p.soft_fraction
        used = spent + reserved
        out.append(
            BudgetOut(
                id=p.id,
                scope=p.scope,
                workflow=p.workflow,
                hard_limit_usd=hard,
                soft_fraction=p.soft_fraction,
                enabled=p.enabled,
                spent_usd=spent,
                reserved_usd=reserved,
                unmetered_calls=unmetered,
                soft_threshold_usd=soft,
                state="hard_limit" if used >= hard else "soft_warning" if used >= soft else "ok",
                updated_at=p.updated_at,
            )
        )
    return out


async def patch_budget(
    session: AsyncSession,
    workspace_id: UUID,
    budget_id: UUID,
    body: BudgetPatch,
    actor: Actor,
    audit: AuditFn,
) -> BudgetOut:
    actor.require(Permission.LLMOPS_BUDGETS_MANAGE)
    p = await session.scalar(
        select(BudgetPolicyRow).where(
            BudgetPolicyRow.id == budget_id, BudgetPolicyRow.workspace_id == workspace_id
        )
    )
    if p is None:
        raise NotFoundError("budget not found")
    raising = body.hard_limit_usd is not None and body.hard_limit_usd > float(p.hard_limit_usd)
    disabling = body.enabled is False
    if (raising or disabling) and not (body.reason and len(body.reason.strip()) >= 10):
        raise ValidationError(
            "raising a limit or disabling a budget needs a reason (10+ characters)"
        )
    before = {
        "hard_limit_usd": float(p.hard_limit_usd),
        "soft_fraction": p.soft_fraction,
        "enabled": p.enabled,
    }
    if body.hard_limit_usd is not None:
        p.hard_limit_usd = body.hard_limit_usd
    if body.soft_fraction is not None:
        p.soft_fraction = body.soft_fraction
    if body.enabled is not None:
        p.enabled = body.enabled
    p.updated_by_user_id = actor.user_id
    if raising or disabling:
        session.add(
            BudgetEventRow(
                workspace_id=workspace_id,
                kind="override",
                scope_key=f"{p.scope}:{workspace_id}",
                detail=f"{actor.user_id}: {(body.reason or '')[:120]}",
            )
        )
    await session.commit()
    await audit(
        "budget.override" if (raising or disabling) else "budget.update",
        "budget_policy",
        str(p.id),
        {
            "before": before,
            "after": {
                "hard_limit_usd": float(p.hard_limit_usd),
                "soft_fraction": p.soft_fraction,
                "enabled": p.enabled,
            },
            "reason": (body.reason or "")[:200],
        },
    )
    return next(b for b in await list_budgets(session, workspace_id) if b.id == p.id)


async def providers_health(
    session: AsyncSession,
    workspace_id: UUID,
    store: BreakerStore | None,
    clock: Callable[[], float],
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=24)
    configs = (
        await session.scalars(
            select(ModelConfiguration).where(ModelConfiguration.status == "published")
        )
    ).all()
    out = []
    for c in configs:
        rows = await _usage_rows(
            session,
            workspace_id,
            since,
            datetime.now(UTC) + timedelta(minutes=1),
            provider=c.provider,
            model=c.model,
        )
        errors = sum(1 for r in rows if r.status != "succeeded")
        state, detail, consecutive, rate = (
            "unknown",
            "circuit-breaker store unavailable",
            None,
            None,
        )
        if store is not None:
            try:
                breaker = CircuitBreaker(c.provider, store, clock=clock)
                snapshot = store.load(c.provider)
                state, consecutive = breaker.current_state(), snapshot.consecutive_failures
                window = snapshot.window
                rate = (
                    (sum(1 for _, o, _ in window if o != "success") / len(window))
                    if window
                    else None
                )
                detail = "shared breaker state"
            except Exception:
                state, detail = "unknown", "circuit-breaker store unreadable"
        out.append(
            {
                "provider": c.provider,
                "model": c.model,
                "breaker_state": state,
                "detail": detail,
                "consecutive_failures": consecutive,
                "recent_error_rate": rate,
                "requests_24h": len(rows),
                "errors_24h": errors,
            }
        )
    return out
