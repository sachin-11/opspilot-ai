"""Durable LangGraph investigation workflow executed only by RQ workers."""

import logging
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from redis import Redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import ActorType, EventType, Incident, IncidentEvent
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow
from opspilot_api.investigations.models import (
    EvidenceItem,
    Investigation,
    InvestigationStatus,
    InvestigationStep,
    StepStatus,
)
from opspilot_api.investigations.providers import ModelResult, ProviderRouter
from opspilot_api.investigations.safety import sanitize_error
from opspilot_api.investigations.schemas import (
    CritiqueResult,
    HypothesisSet,
    InvestigationPlan,
    InvestigationReport,
    TriageResult,
)
from opspilot_api.investigations.tools import ToolContext, execute_tool

logger = logging.getLogger(__name__)
MODEL_NODES = {
    "triage_incident",
    "plan_investigation",
    "generate_hypotheses",
    "critique_hypotheses",
    "produce_report",
    "revision",
}


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    workspace_id: str
    incident_id: str
    incident_snapshot: dict[str, Any]
    timeline_summary: list[dict[str, Any]]
    triage: dict[str, Any]
    plan: dict[str, Any]
    evidence_ids: list[str]
    code_evidence_ids: list[str]
    tool_observations: list[dict[str, Any]]
    hypotheses: dict[str, Any]
    critique: dict[str, Any]
    report: dict[str, Any]
    human_feedback: dict[str, Any]
    revision_count: int
    error_state: dict[str, str]


CODE_SOURCE_TYPES = (
    "repository_commit",
    "deployment",
    "pull_request",
    "code_chunk",
    "commit_diff",
)


class InvestigationCancelled(Exception):
    pass


class WorkflowLimitReached(Exception):
    pass


class InvalidEvidenceCitation(Exception):
    pass


def _publish(
    settings: Settings,
    investigation: Investigation,
    event_type: str,
    data: dict[str, object] | None = None,
) -> None:
    envelope = RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=investigation.workspace_id,
        incident_id=investigation.incident_id,
        occurred_at=utcnow(),
        data={
            "investigation_id": str(investigation.id),
            "status": investigation.status,
            **(data or {}),
        },
    )
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        connection.publish(
            f"opspilot:workspace:{investigation.workspace_id}:incidents",
            envelope.model_dump_json(),
        )
    except Exception as exc:
        logger.warning(
            "investigation progress publication failed", extra={"error_class": type(exc).__name__}
        )
    finally:
        connection.close()


class WorkflowRuntime:
    def __init__(self, settings: Settings, provider: ProviderRouter | None = None) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url, pool_pre_ping=True)
        self.provider = provider or ProviderRouter(settings)

    def close(self) -> None:
        self.engine.dispose()

    def _investigation(self, session: Session, state: InvestigationState) -> Investigation:
        row = session.scalar(
            select(Investigation).where(
                Investigation.id == UUID(state["investigation_id"]),
                Investigation.workspace_id == UUID(state["workspace_id"]),
            )
        )
        if row is None:
            raise LookupError("investigation not found in workspace")
        if row.status == InvestigationStatus.CANCELLED.value:
            raise InvestigationCancelled
        return row

    def _check_limits(
        self, session: Session, investigation: Investigation, additional_tools: int = 0
    ) -> None:
        if (
            investigation.started_at
            and (utcnow() - investigation.started_at).total_seconds()
            > self.settings.AI_MAX_RUNTIME_SECONDS
        ):
            raise WorkflowLimitReached("maximum graph runtime reached")
        model_calls = len(
            list(
                session.scalars(
                    select(InvestigationStep.id).where(
                        InvestigationStep.investigation_id == investigation.id,
                        InvestigationStep.workspace_id == investigation.workspace_id,
                        InvestigationStep.node_name.in_(MODEL_NODES),
                        InvestigationStep.status == StepStatus.COMPLETED.value,
                    )
                )
            )
        )
        if model_calls >= self.settings.AI_MAX_LLM_CALLS:
            raise WorkflowLimitReached("maximum LLM call limit reached")
        tool_step = session.scalar(
            select(InvestigationStep).where(
                InvestigationStep.investigation_id == investigation.id,
                InvestigationStep.node_name == "gather_evidence",
            )
        )
        prior_tools = 0
        if tool_step and isinstance(tool_step.structured_output, dict):
            prior_tools = int(tool_step.structured_output.get("tool_calls", 0))
        if prior_tools + additional_tools > self.settings.AI_MAX_TOOL_CALLS:
            raise WorkflowLimitReached("maximum tool call limit reached")
        if investigation.total_input_tokens >= self.settings.AI_MAX_INPUT_TOKENS:
            raise WorkflowLimitReached("maximum input token limit reached")
        if investigation.total_output_tokens >= self.settings.AI_MAX_OUTPUT_TOKENS:
            raise WorkflowLimitReached("maximum output token limit reached")
        if (
            investigation.estimated_cost_usd is not None
            and float(investigation.estimated_cost_usd) >= self.settings.AI_MAX_COST_USD
        ):
            raise WorkflowLimitReached("maximum investigation cost reached")

    def _begin_step(
        self, session: Session, state: InvestigationState, node: str, display: str, attempt: int = 1
    ) -> tuple[Investigation, InvestigationStep]:
        investigation = self._investigation(session, state)
        self._check_limits(session, investigation)
        step = session.scalar(
            select(InvestigationStep).where(
                InvestigationStep.investigation_id == investigation.id,
                InvestigationStep.node_name == node,
                InvestigationStep.attempt == attempt,
            )
        )
        now = utcnow()
        if step is None:
            step = InvestigationStep(
                id=uuid4(),
                workspace_id=investigation.workspace_id,
                investigation_id=investigation.id,
                node_name=node,
                display_name=display,
                status=StepStatus.RUNNING.value,
                attempt=attempt,
                started_at=now,
            )
            session.add(step)
        else:
            step.status = StepStatus.RUNNING.value
            step.started_at = now
            step.completed_at = None
            step.error_code = None
            step.error_message = None
        investigation.updated_at = now
        session.commit()
        _publish(
            self.settings,
            investigation,
            "investigation.step_started",
            {"node_name": node, "display_name": display},
        )
        return investigation, step

    def _complete_step(
        self,
        session: Session,
        investigation: Investigation,
        step: InvestigationStep,
        summary: str,
        output: dict[str, Any] | list[Any] | None,
        usage: ModelResult | None = None,
    ) -> None:
        step.status = StepStatus.COMPLETED.value
        step.summary = summary[:1000]
        step.structured_output = output
        step.completed_at = utcnow()
        if usage:
            step.input_tokens = usage.usage.input_tokens
            step.output_tokens = usage.usage.output_tokens
            cost = self._cost(usage.usage.input_tokens, usage.usage.output_tokens)
            step.estimated_cost_usd = cost
            investigation.total_input_tokens += usage.usage.input_tokens
            investigation.total_output_tokens += usage.usage.output_tokens
            if cost is not None:
                investigation.estimated_cost_usd = (
                    float(investigation.estimated_cost_usd or 0) + cost
                )
        investigation.updated_at = utcnow()
        session.commit()
        _publish(
            self.settings,
            investigation,
            "investigation.step_completed",
            {"node_name": step.node_name, "summary": step.summary},
        )
        if investigation.total_input_tokens > self.settings.AI_MAX_INPUT_TOKENS:
            raise WorkflowLimitReached("maximum input token limit reached")
        if investigation.total_output_tokens > self.settings.AI_MAX_OUTPUT_TOKENS:
            raise WorkflowLimitReached("maximum output token limit reached")
        if (
            investigation.estimated_cost_usd is not None
            and float(investigation.estimated_cost_usd) > self.settings.AI_MAX_COST_USD
        ):
            raise WorkflowLimitReached("maximum investigation cost reached")

    def _fail_step(
        self,
        session: Session,
        investigation: Investigation,
        step: InvestigationStep,
        exc: BaseException,
    ) -> None:
        step.status = StepStatus.FAILED.value
        step.error_code = getattr(exc, "code", type(exc).__name__.lower())
        step.error_message = sanitize_error(exc)
        step.completed_at = utcnow()
        session.commit()
        _publish(
            self.settings,
            investigation,
            "investigation.step_failed",
            {"node_name": step.node_name, "error_code": step.error_code},
        )

    def _cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if (
            self.settings.AI_INPUT_PRICE_PER_MILLION is None
            or self.settings.AI_OUTPUT_PRICE_PER_MILLION is None
        ):
            return None
        return round(
            input_tokens * self.settings.AI_INPUT_PRICE_PER_MILLION / 1_000_000
            + output_tokens * self.settings.AI_OUTPUT_PRICE_PER_MILLION / 1_000_000,
            6,
        )

    def _model(
        self,
        session: Session,
        investigation: Investigation,
        prompt: str,
        schema: type[Any],
        context: dict[str, Any],
    ) -> ModelResult:
        self._check_limits(session, investigation)
        return self.provider.generate(prompt, schema, context)

    def load_context(self, state: InvestigationState) -> InvestigationState:
        with Session(self.engine) as session:
            investigation, step = self._begin_step(
                session, state, "load_context", "Load incident context"
            )
            try:
                incident = session.scalar(
                    select(Incident).where(
                        Incident.id == investigation.incident_id,
                        Incident.workspace_id == investigation.workspace_id,
                    )
                )
                if incident is None:
                    raise LookupError("incident not found in workspace")
                timeline = list(
                    session.scalars(
                        select(IncidentEvent)
                        .where(
                            IncidentEvent.workspace_id == investigation.workspace_id,
                            IncidentEvent.incident_id == investigation.incident_id,
                        )
                        .order_by(IncidentEvent.occurred_at)
                        .limit(100)
                    )
                )
                snapshot = {
                    "title": incident.title,
                    "description": incident.description,
                    "severity": incident.severity,
                    "status": incident.status,
                    "service_id": str(incident.service_id) if incident.service_id else None,
                    "opened_at": incident.opened_at.isoformat(),
                }
                timeline_summary = [
                    {
                        "event_type": item.event_type,
                        "summary": item.summary,
                        "occurred_at": item.occurred_at.isoformat(),
                    }
                    for item in timeline
                ]
                self._complete_step(
                    session,
                    investigation,
                    step,
                    "Loaded incident and timeline snapshot.",
                    {"timeline_count": len(timeline_summary)},
                )
                return {"incident_snapshot": snapshot, "timeline_summary": timeline_summary}
            except Exception as exc:
                self._fail_step(session, investigation, step, exc)
                raise

    def triage(self, state: InvestigationState) -> InvestigationState:
        return self._model_node(
            state,
            "triage_incident",
            "Triage incident",
            "triage",
            TriageResult,
            {"incident": state["incident_snapshot"]},
            "triage",
        )

    def plan(self, state: InvestigationState) -> InvestigationState:
        return self._model_node(
            state,
            "plan_investigation",
            "Plan investigation",
            "plan",
            InvestigationPlan,
            {"incident": state["incident_snapshot"], "triage": state["triage"]},
            "plan",
        )

    def _model_node(
        self,
        state: InvestigationState,
        node: str,
        display: str,
        prompt: str,
        schema: type[Any],
        context: dict[str, Any],
        state_key: str,
        attempt: int = 1,
    ) -> InvestigationState:
        with Session(self.engine) as session:
            investigation, step = self._begin_step(session, state, node, display, attempt)
            try:
                result = self._model(session, investigation, prompt, schema, context)
                output = result.output.model_dump(mode="json")
                self._complete_step(
                    session, investigation, step, f"{display} completed.", output, result
                )
                return cast(InvestigationState, {state_key: output})
            except Exception as exc:
                self._fail_step(session, investigation, step, exc)
                raise

    def gather(self, state: InvestigationState) -> InvestigationState:
        with Session(self.engine) as session:
            investigation, step = self._begin_step(
                session, state, "gather_evidence", "Gather evidence"
            )
            try:
                plan = InvestigationPlan.model_validate(state["plan"])
                self._check_limits(session, investigation, len(plan.tool_requests))
                context = ToolContext(
                    workspace_id=investigation.workspace_id,
                    incident_id=investigation.incident_id,
                    investigation_id=investigation.id,
                    settings=self.settings,
                )
                evidence_ids: list[str] = []
                observations: list[dict[str, Any]] = []
                for request in plan.tool_requests:
                    result = execute_tool(
                        request.tool_name,
                        session,
                        context,
                        request.arguments,
                        self.settings.AI_MAX_TOOL_OUTPUT_BYTES,
                    )
                    observations.append(
                        {
                            "tool_name": result.tool_name,
                            "summary": result.summary,
                            "data": result.data,
                        }
                    )
                    for draft in result.evidence:
                        existing = session.scalar(
                            select(EvidenceItem).where(
                                EvidenceItem.investigation_id == investigation.id,
                                EvidenceItem.content_hash == draft.content_hash,
                            )
                        )
                        if existing is None:
                            current_count = len(
                                list(
                                    session.scalars(
                                        select(EvidenceItem.id).where(
                                            EvidenceItem.investigation_id == investigation.id
                                        )
                                    )
                                )
                            )
                            if current_count >= self.settings.AI_MAX_EVIDENCE_ITEMS:
                                raise WorkflowLimitReached("maximum evidence item limit reached")
                            existing = EvidenceItem(
                                id=uuid4(),
                                workspace_id=investigation.workspace_id,
                                investigation_id=investigation.id,
                                evidence_type=draft.evidence_type,
                                source_type=draft.source_type,
                                source_id=draft.source_id,
                                title=draft.title[:240],
                                excerpt=draft.excerpt[:2000],
                                evidence_metadata=draft.metadata,
                                content_hash=draft.content_hash,
                                collected_at=draft.collected_at,
                            )
                            session.add(existing)
                            session.flush()
                        evidence_ids.append(str(existing.id))
                evidence_ids = list(dict.fromkeys(evidence_ids))
                code_evidence_ids = [
                    str(item)
                    for item in session.scalars(
                        select(EvidenceItem.id).where(
                            EvidenceItem.investigation_id == investigation.id,
                            EvidenceItem.workspace_id == investigation.workspace_id,
                            EvidenceItem.id.in_([UUID(value) for value in evidence_ids]),
                            EvidenceItem.source_type.in_(CODE_SOURCE_TYPES),
                        )
                    )
                ]
                output = {
                    "tool_calls": len(plan.tool_requests),
                    "evidence_count": len(evidence_ids),
                }
                self._complete_step(
                    session,
                    investigation,
                    step,
                    f"Collected {len(evidence_ids)} deduplicated evidence items.",
                    output,
                )
                return {
                    "evidence_ids": evidence_ids,
                    "code_evidence_ids": code_evidence_ids,
                    "tool_observations": observations,
                }
            except Exception as exc:
                session.rollback()
                investigation, step = self._begin_step(
                    session, state, "gather_evidence", "Gather evidence"
                )
                self._fail_step(session, investigation, step, exc)
                raise

    def hypotheses(self, state: InvestigationState) -> InvestigationState:
        result = self._model_node(
            state,
            "generate_hypotheses",
            "Generate hypotheses",
            "hypotheses",
            HypothesisSet,
            {
                "incident": state["incident_snapshot"],
                "observations": state["tool_observations"],
                "evidence_ids": state["evidence_ids"],
                "code_evidence_ids": state.get("code_evidence_ids", []),
            },
            "hypotheses",
        )
        hypotheses = HypothesisSet.model_validate(result["hypotheses"])
        self._validate_hypothesis_citations(hypotheses, state["evidence_ids"])
        return result

    def critique(self, state: InvestigationState) -> InvestigationState:
        return self._model_node(
            state,
            "critique_hypotheses",
            "Critique hypotheses",
            "critique",
            CritiqueResult,
            {
                "hypotheses": state["hypotheses"],
                "evidence_ids": state["evidence_ids"],
                "insufficient_evidence": len(state["evidence_ids"]) < 2,
            },
            "critique",
        )

    def report(self, state: InvestigationState) -> InvestigationState:
        result = self._model_node(
            state,
            "produce_report",
            "Produce investigation report",
            "report",
            InvestigationReport,
            {
                "incident": state["incident_snapshot"],
                "hypotheses": state["hypotheses"],
                "critique": state["critique"],
                "evidence_ids": state["evidence_ids"],
                "code_evidence_ids": state.get("code_evidence_ids", []),
                "fallback_evidence_id": state["evidence_ids"][0],
                "insufficient_evidence": state["critique"].get("insufficient_evidence", False),
            },
            "report",
        )
        self._validate_report_and_persist(state, result["report"])
        return result

    def human_review(self, state: InvestigationState) -> InvestigationState:
        with Session(self.engine) as session:
            investigation = self._investigation(session, state)
            if investigation.status != InvestigationStatus.AWAITING_REVIEW.value:
                investigation.status = InvestigationStatus.AWAITING_REVIEW.value
                investigation.review_requested_at = utcnow()
                investigation.version += 1
                session.commit()
                _publish(self.settings, investigation, "investigation.awaiting_review")
        decision = interrupt(
            {
                "investigation_id": state["investigation_id"],
                "message": "Explicit human review is required.",
            }
        )
        return {"human_feedback": cast(dict[str, Any], decision)}

    def revision(self, state: InvestigationState) -> InvestigationState:
        revision_count = int(
            state["human_feedback"].get("revision_count", state.get("revision_count", 0) + 1)
        )
        if revision_count > self.settings.AI_MAX_REVISIONS:
            raise WorkflowLimitReached("maximum revision count reached")
        result = self._model_node(
            state,
            "revision",
            "Revise investigation report",
            "revision",
            InvestigationReport,
            {
                "report": state["report"],
                "human_feedback": state["human_feedback"].get("comment"),
                "evidence_ids": state["evidence_ids"],
                "code_evidence_ids": state.get("code_evidence_ids", []),
                "fallback_evidence_id": state["evidence_ids"][0],
            },
            "report",
            attempt=revision_count,
        )
        self._validate_report_and_persist(state, result["report"])
        return {**result, "revision_count": revision_count}

    def complete(self, state: InvestigationState) -> InvestigationState:
        self._terminal(
            state,
            InvestigationStatus.COMPLETED,
            EventType.INVESTIGATION_COMPLETED,
            "Investigation approved and completed",
            "investigation.completed",
        )
        return {}

    def reject(self, state: InvestigationState) -> InvestigationState:
        self._terminal(
            state,
            InvestigationStatus.REJECTED,
            EventType.INVESTIGATION_REJECTED,
            "Investigation rejected during human review",
            "investigation.rejected",
        )
        return {}

    def _terminal(
        self,
        state: InvestigationState,
        status: InvestigationStatus,
        timeline_type: EventType,
        summary: str,
        realtime_type: str,
    ) -> None:
        with Session(self.engine) as session:
            investigation = self._investigation(session, state)
            investigation.status = status.value
            investigation.completed_at = (
                utcnow() if status is InvestigationStatus.COMPLETED else None
            )
            investigation.version += 1
            exists = session.scalar(
                select(IncidentEvent.id).where(
                    IncidentEvent.workspace_id == investigation.workspace_id,
                    IncidentEvent.incident_id == investigation.incident_id,
                    IncidentEvent.event_type == timeline_type.value,
                    IncidentEvent.event_metadata["investigation_id"].astext
                    == str(investigation.id),
                )
            )
            if exists is None:
                session.add(
                    IncidentEvent(
                        id=uuid4(),
                        workspace_id=investigation.workspace_id,
                        incident_id=investigation.incident_id,
                        event_type=timeline_type.value,
                        actor_type=ActorType.SYSTEM.value,
                        summary=summary,
                        event_metadata={"investigation_id": str(investigation.id)},
                        occurred_at=utcnow(),
                    )
                )
            session.commit()
            _publish(self.settings, investigation, realtime_type)

    def _validate_hypothesis_citations(
        self, hypotheses: HypothesisSet, valid_ids: list[str]
    ) -> None:
        valid = {UUID(value) for value in valid_ids}
        cited = {
            item
            for hypothesis in hypotheses.hypotheses
            for item in hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids
        }
        if not cited.issubset(valid):
            raise InvalidEvidenceCitation("hypothesis cites evidence outside this investigation")

    def _validate_report_and_persist(
        self, state: InvestigationState, raw_report: dict[str, Any]
    ) -> None:
        report = InvestigationReport.model_validate(raw_report)
        valid = {UUID(value) for value in state["evidence_ids"]}
        cited = set(report.evidence_citations)
        for step in (
            report.immediate_containment_steps + report.remediation_plan + report.verification_plan
        ):
            cited.update(step.evidence_ids)
        if not cited or not cited.issubset(valid):
            raise InvalidEvidenceCitation("report contains unsupported evidence citations")
        with Session(self.engine) as session:
            investigation = self._investigation(session, state)
            investigation.title = report.title
            investigation.executive_summary = report.executive_summary
            investigation.root_cause = report.root_cause
            investigation.confidence = report.confidence
            investigation.remediation_plan = [
                item.model_dump(mode="json") for item in report.remediation_plan
            ]
            investigation.verification_plan = [
                item.model_dump(mode="json") for item in report.verification_plan
            ]
            session.commit()


def build_graph(runtime: WorkflowRuntime, checkpointer: PostgresSaver) -> Any:
    builder = StateGraph(InvestigationState)
    builder.add_node("load_context", runtime.load_context)
    builder.add_node("triage_incident", runtime.triage)
    builder.add_node("plan_investigation", runtime.plan)
    builder.add_node("gather_evidence", runtime.gather)
    builder.add_node("generate_hypotheses", runtime.hypotheses)
    builder.add_node("critique_hypotheses", runtime.critique)
    builder.add_node("produce_report", runtime.report)
    builder.add_node("human_review", runtime.human_review)
    builder.add_node("revision", runtime.revision)
    builder.add_node("complete", runtime.complete)
    builder.add_node("reject", runtime.reject)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "triage_incident")
    builder.add_edge("triage_incident", "plan_investigation")
    builder.add_edge("plan_investigation", "gather_evidence")
    builder.add_edge("gather_evidence", "generate_hypotheses")
    builder.add_edge("generate_hypotheses", "critique_hypotheses")
    builder.add_edge("critique_hypotheses", "produce_report")
    builder.add_edge("produce_report", "human_review")
    builder.add_conditional_edges(
        "human_review",
        lambda state: state["human_feedback"]["action"],
        {"approved": "complete", "revision_requested": "revision", "rejected": "reject"},
    )
    builder.add_edge("revision", "human_review")
    builder.add_edge("complete", END)
    builder.add_edge("reject", END)
    return builder.compile(checkpointer=checkpointer)


def _mark_failed(
    settings: Settings, workspace_id: UUID, investigation_id: UUID, exc: BaseException
) -> None:
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            investigation = session.scalar(
                select(Investigation).where(
                    Investigation.id == investigation_id,
                    Investigation.workspace_id == workspace_id,
                )
            )
            if investigation is None or investigation.status == InvestigationStatus.CANCELLED.value:
                return
            investigation.status = InvestigationStatus.FAILED.value
            investigation.failed_at = utcnow()
            investigation.failure_code = (
                "limit_reached"
                if isinstance(exc, WorkflowLimitReached)
                else getattr(exc, "code", type(exc).__name__.lower())
            )
            investigation.failure_message = sanitize_error(exc)
            investigation.version += 1
            session.commit()
            event_type = (
                "investigation.limit_reached"
                if isinstance(exc, WorkflowLimitReached)
                else "investigation.failed"
            )
            _publish(
                settings, investigation, event_type, {"failure_code": investigation.failure_code}
            )
    finally:
        engine.dispose()


def run_workflow(
    settings: Settings,
    workspace_id: UUID,
    investigation_id: UUID,
    resume: dict[str, Any] | None = None,
) -> None:
    runtime = WorkflowRuntime(settings)
    config: RunnableConfig = {"configurable": {"thread_id": str(investigation_id)}}
    try:
        with PostgresSaver.from_conn_string(settings.checkpoint_url) as checkpointer:
            checkpointer.setup()
            graph = build_graph(runtime, checkpointer)
            with Session(runtime.engine) as session:
                investigation = runtime._investigation(
                    session,
                    {"investigation_id": str(investigation_id), "workspace_id": str(workspace_id)},
                )
                if (
                    resume is None
                    and investigation.status == InvestigationStatus.AWAITING_REVIEW.value
                ):
                    return
                checkpoint_exists = checkpointer.get(config) is not None
                if resume is None:
                    investigation.status = InvestigationStatus.RUNNING.value
                    investigation.started_at = investigation.started_at or utcnow()
                    investigation.updated_at = utcnow()
                    session.commit()
                    _publish(settings, investigation, "investigation.started")
                    initial: InvestigationState | None
                    if checkpoint_exists:
                        initial = None
                    else:
                        initial = {
                            "investigation_id": str(investigation.id),
                            "workspace_id": str(investigation.workspace_id),
                            "incident_id": str(investigation.incident_id),
                            "revision_count": investigation.revision_count,
                        }
                else:
                    initial = None
            graph.invoke(Command(resume=resume) if resume is not None else initial, config)
    except InvestigationCancelled:
        return
    except Exception as exc:
        _mark_failed(settings, workspace_id, investigation_id, exc)
        raise
    finally:
        runtime.close()


def setup_checkpoints(settings: Settings) -> None:
    with PostgresSaver.from_conn_string(settings.checkpoint_url) as checkpointer:
        checkpointer.setup()
