"""Typed, bounded, read-only investigation tools."""

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, IncidentEvent, IngestedEvent, MonitoredService
from opspilot_api.investigations.safety import redact_text, redact_value

MAX_RESULTS = 100
MAX_WINDOW_DAYS = 30
logger = logging.getLogger(__name__)


class ToolContext(BaseModel):
    workspace_id: UUID
    incident_id: UUID
    investigation_id: UUID
    settings: Settings


class TimeRangeInput(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=MAX_RESULTS)

    @model_validator(mode="after")
    def bounded_range(self) -> "TimeRangeInput":
        if self.start and self.end:
            if self.end <= self.start:
                raise ValueError("end must be after start")
            if self.end - self.start > timedelta(days=MAX_WINDOW_DAYS):
                raise ValueError("time range exceeds 30 days")
        return self


class SearchEventsInput(TimeRangeInput):
    event_type: str | None = Field(default=None, max_length=100)
    severity: Literal["sev1", "sev2", "sev3", "sev4"] | None = None
    error_signature: str | None = Field(default=None, max_length=300)


class CompareWindowsInput(BaseModel):
    split_at: datetime | None = None
    window_minutes: int = Field(default=60, ge=5, le=1440)


class SearchRunbooksInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    service_id: UUID | None = None
    document_types: list[
        Literal["runbook", "postmortem", "architecture", "troubleshooting", "policy", "other"]
    ] = Field(default_factory=list, max_length=6)
    top_k: int = Field(default=3, ge=1, le=10)


class GetKnowledgeChunkInput(BaseModel):
    chunk_id: UUID


class EvidenceDraft(BaseModel):
    evidence_type: str
    source_type: str
    source_id: str | None
    title: str
    excerpt: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {
                "evidence_type": self.evidence_type,
                "source_type": self.source_type,
                "source_id": self.source_id,
                "title": self.title,
                "excerpt": self.excerpt,
                "metadata": self.metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class ToolResult(BaseModel):
    tool_name: str
    summary: str
    evidence: list[EvidenceDraft]
    data: dict[str, Any]


def _incident(session: Session, context: ToolContext) -> Incident:
    incident = session.scalar(
        select(Incident).where(
            Incident.id == context.incident_id,
            Incident.workspace_id == context.workspace_id,
        )
    )
    if incident is None:
        raise LookupError("incident not found in workspace")
    return incident


def get_incident_context(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    incident = _incident(session, context)
    service = None
    if incident.service_id:
        service = session.scalar(
            select(MonitoredService).where(
                MonitoredService.id == incident.service_id,
                MonitoredService.workspace_id == context.workspace_id,
            )
        )
    data = {
        "incident_id": str(incident.id),
        "title": redact_text(incident.title),
        "description": redact_text(incident.description or ""),
        "severity": incident.severity,
        "status": incident.status,
        "opened_at": incident.opened_at.isoformat(),
        "service": {
            "id": str(service.id),
            "name": redact_text(service.name),
            "environment": service.environment,
            "description": redact_text(service.description or ""),
        }
        if service
        else None,
    }
    evidence = EvidenceDraft(
        evidence_type="incident",
        source_type="incident",
        source_id=str(incident.id),
        title="Incident and service context",
        excerpt=redact_text(json.dumps(data, default=str)),
        metadata={"severity": incident.severity, "status": incident.status},
        collected_at=datetime.now(UTC),
    )
    return ToolResult(
        tool_name="get_incident_context",
        summary="Loaded incident and service metadata.",
        evidence=[evidence],
        data=data,
    )


def get_incident_timeline(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    _incident(session, context)
    params = TimeRangeInput.model_validate(raw)
    stmt = select(IncidentEvent).where(
        IncidentEvent.workspace_id == context.workspace_id,
        IncidentEvent.incident_id == context.incident_id,
    )
    if params.start:
        stmt = stmt.where(IncidentEvent.occurred_at >= params.start)
    if params.end:
        stmt = stmt.where(IncidentEvent.occurred_at <= params.end)
    rows = list(session.scalars(stmt.order_by(IncidentEvent.occurred_at).limit(params.limit)))
    events = [
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "summary": redact_text(row.summary),
            "occurred_at": row.occurred_at.isoformat(),
        }
        for row in rows
    ]
    evidence = [
        EvidenceDraft(
            evidence_type="timeline",
            source_type="incident_event",
            source_id=item["id"],
            title=f"Timeline: {item['event_type']}",
            excerpt=item["summary"],
            metadata={"event_type": item["event_type"]},
            collected_at=datetime.fromisoformat(item["occurred_at"]),
        )
        for item in events
    ]
    return ToolResult(
        tool_name="get_incident_timeline",
        summary=f"Loaded {len(events)} timeline events.",
        evidence=evidence,
        data={"events": events},
    )


def search_ingested_events(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    _incident(session, context)
    params = SearchEventsInput.model_validate(raw)
    stmt = select(IngestedEvent).where(
        IngestedEvent.workspace_id == context.workspace_id,
        IngestedEvent.incident_id == context.incident_id,
    )
    if params.start:
        stmt = stmt.where(IngestedEvent.occurred_at >= params.start)
    if params.end:
        stmt = stmt.where(IngestedEvent.occurred_at <= params.end)
    if params.event_type:
        stmt = stmt.where(IngestedEvent.event_type == params.event_type)
    if params.severity:
        stmt = stmt.where(IngestedEvent.severity == params.severity)
    if params.error_signature:
        stmt = stmt.where(IngestedEvent.error_signature == params.error_signature)
    rows = list(session.scalars(stmt.order_by(IngestedEvent.occurred_at).limit(params.limit)))
    items = [
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "severity": row.severity,
            "summary": redact_text(row.summary),
            "error_signature": redact_text(row.error_signature or ""),
            "attributes": redact_value(row.attributes),
            "occurred_at": row.occurred_at.isoformat(),
        }
        for row in rows
    ]
    evidence = [
        EvidenceDraft(
            evidence_type="ingested_event",
            source_type="ingested_event",
            source_id=item["id"],
            title=f"{item['event_type']} ({item['severity']})",
            excerpt=redact_text(f"{item['summary']} signature={item['error_signature']}"),
            metadata={"event_type": item["event_type"], "severity": item["severity"]},
            collected_at=datetime.fromisoformat(item["occurred_at"]),
        )
        for item in items
    ]
    return ToolResult(
        tool_name="search_ingested_events",
        summary=f"Found {len(items)} correlated events.",
        evidence=evidence,
        data={"events": items},
    )


def summarize_event_frequency(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    _incident(session, context)
    params = TimeRangeInput.model_validate(raw)
    stmt = select(IngestedEvent).where(
        IngestedEvent.workspace_id == context.workspace_id,
        IngestedEvent.incident_id == context.incident_id,
    )
    if params.start:
        stmt = stmt.where(IngestedEvent.occurred_at >= params.start)
    if params.end:
        stmt = stmt.where(IngestedEvent.occurred_at <= params.end)
    rows = list(session.scalars(stmt.order_by(IngestedEvent.occurred_at).limit(params.limit)))
    buckets = Counter(
        row.occurred_at.replace(minute=0, second=0, microsecond=0).isoformat() for row in rows
    )
    data = {
        "total": len(rows),
        "by_event_type": dict(Counter(row.event_type for row in rows)),
        "by_severity": dict(Counter(row.severity for row in rows)),
        "first_occurrence": rows[0].occurred_at.isoformat() if rows else None,
        "last_occurrence": rows[-1].occurred_at.isoformat() if rows else None,
        "hourly_buckets": dict(sorted(buckets.items())),
    }
    evidence = EvidenceDraft(
        evidence_type="metric_summary",
        source_type="derived",
        source_id=None,
        title="Event frequency summary",
        excerpt=redact_text(json.dumps(data)),
        metadata=data,
        collected_at=datetime.now(UTC),
    )
    return ToolResult(
        tool_name="summarize_event_frequency",
        summary=f"Summarized {len(rows)} events.",
        evidence=[evidence],
        data=data,
    )


def compare_incident_windows(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    incident = _incident(session, context)
    params = CompareWindowsInput.model_validate(raw)
    split = params.split_at or incident.opened_at
    delta = timedelta(minutes=params.window_minutes)
    rows = list(
        session.scalars(
            select(IngestedEvent).where(
                IngestedEvent.workspace_id == context.workspace_id,
                IngestedEvent.incident_id == context.incident_id,
                and_(
                    IngestedEvent.occurred_at >= split - delta,
                    IngestedEvent.occurred_at <= split + delta,
                ),
            )
        )
    )
    before = [row for row in rows if row.occurred_at < split]
    after = [row for row in rows if row.occurred_at >= split]
    data = {
        "split_at": split.isoformat(),
        "window_minutes": params.window_minutes,
        "before_count": len(before),
        "after_count": len(after),
        "delta": len(after) - len(before),
    }
    evidence = EvidenceDraft(
        evidence_type="metric_summary",
        source_type="derived",
        source_id=None,
        title="Before/after incident event comparison",
        excerpt=json.dumps(data),
        metadata=data,
        collected_at=datetime.now(UTC),
    )
    return ToolResult(
        tool_name="compare_incident_windows",
        summary="Compared bounded event windows.",
        evidence=[evidence],
        data=data,
    )


def search_runbooks(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    incident = _incident(session, context)
    params = SearchRunbooksInput.model_validate(raw)
    if params.service_id is not None and params.service_id != incident.service_id:
        raise ValueError("runbook service filter must match the incident service")
    from opspilot_api.knowledge.embeddings import EmbeddingError
    from opspilot_api.knowledge.models import DocumentType
    from opspilot_api.knowledge.retrieval import RetrievalService
    from opspilot_api.knowledge.schemas import RetrievalVariant, SearchRequest

    try:
        response = RetrievalService(context.settings).search(
            session,
            context.workspace_id,
            SearchRequest(
                query=params.query,
                service_id=params.service_id or incident.service_id,
                document_types=[DocumentType(value) for value in params.document_types],
                variant=RetrievalVariant.HYBRID_RRF,
                candidate_count=max(5, params.top_k * 4),
                top_k=params.top_k,
            ),
            context.investigation_id,
        )
    except (EmbeddingError, SQLAlchemyError) as exc:
        session.rollback()
        logger.warning("runbook retrieval unavailable", extra={"error_class": type(exc).__name__})
        return ToolResult(
            tool_name="search_runbooks",
            summary="Runbook retrieval was unavailable; the investigation can continue.",
            evidence=[],
            data={"results": [], "low_relevance": True, "temporarily_unavailable": True},
        )
    evidence = [
        EvidenceDraft(
            evidence_type="runbook",
            source_type="knowledge_chunk",
            source_id=str(item.chunk_id),
            title=item.citation_label,
            excerpt=redact_text(item.excerpt),
            metadata={
                "citation_label": item.citation_label,
                "document_id": str(item.document_id),
                "document_version_id": str(item.document_version_id),
                "rrf_score": item.rrf_score,
            },
            collected_at=datetime.now(UTC),
        )
        for item in response.results
    ]
    return ToolResult(
        tool_name="search_runbooks",
        summary=(
            f"Found {len(evidence)} relevant runbook chunks."
            if evidence
            else "No sufficiently relevant runbook was found."
        ),
        evidence=evidence,
        data={
            "results": [
                {
                    "chunk_id": str(item.chunk_id),
                    "citation_label": item.citation_label,
                    "rrf_score": item.rrf_score,
                }
                for item in response.results
            ],
            "low_relevance": response.diagnostics.low_relevance,
        },
    )


def get_knowledge_chunk(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    params = GetKnowledgeChunkInput.model_validate(raw)
    from opspilot_api.knowledge.retrieval import get_chunk

    chunk, document, citation = get_chunk(session, context.workspace_id, params.chunk_id)
    evidence = EvidenceDraft(
        evidence_type="runbook",
        source_type="knowledge_chunk",
        source_id=str(chunk.id),
        title=citation,
        excerpt=redact_text(chunk.content),
        metadata={
            "citation_label": citation,
            "document_id": str(document.id),
            "document_version_id": str(chunk.document_version_id),
        },
        collected_at=chunk.created_at,
    )
    return ToolResult(
        tool_name="get_knowledge_chunk",
        summary="Loaded one workspace-scoped knowledge chunk.",
        evidence=[evidence],
        data={"chunk_id": str(chunk.id), "citation_label": citation},
    )


ToolFunction = Callable[[Session, ToolContext, dict[str, Any]], ToolResult]


def _code_tool(name: str) -> ToolFunction:
    """Late-bound adapter so repository tools can import this module without a cycle."""

    def run(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
        from opspilot_api.repositories.tools import CODE_TOOLS

        return CODE_TOOLS[name](session, context, raw)

    return run


TOOL_REGISTRY: dict[str, ToolFunction] = {
    "get_incident_context": get_incident_context,
    "get_incident_timeline": get_incident_timeline,
    "search_ingested_events": search_ingested_events,
    "summarize_event_frequency": summarize_event_frequency,
    "compare_incident_windows": compare_incident_windows,
    "search_runbooks": search_runbooks,
    "get_knowledge_chunk": get_knowledge_chunk,
    "list_recent_changes": _code_tool("list_recent_changes"),
    "rank_suspect_changes": _code_tool("rank_suspect_changes"),
    "get_commit_summary": _code_tool("get_commit_summary"),
    "get_commit_diff": _code_tool("get_commit_diff"),
    "search_repository_code": _code_tool("search_repository_code"),
    "get_code_excerpt": _code_tool("get_code_excerpt"),
    "get_pull_request_context": _code_tool("get_pull_request_context"),
}


def execute_tool(
    name: str,
    session: Session,
    context: ToolContext,
    arguments: dict[str, Any],
    max_output_bytes: int,
) -> ToolResult:
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"unregistered tool: {name}")
    result = tool(session, context, arguments)
    if len(result.model_dump_json().encode()) > max_output_bytes:
        raise ValueError(f"tool output exceeds configured limit: {name}")
    return result
