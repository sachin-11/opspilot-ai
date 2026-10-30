"""Read-only LangGraph observability tools and the incident observability collector.

Scope (workspace, incident, service, source) comes only from the trusted ``ToolContext``. The
model supplies bounded search parameters; it can never name a log group, namespace, account, or
role. Returned log text is redacted and treated as untrusted evidence. Provider problems become
explicit "unavailable" observations, never fabricated evidence.
"""

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, MonitoredService
from opspilot_api.investigations.tools import EvidenceDraft, ToolContext, ToolResult
from opspilot_api.observability.models import (
    CloudWatchQueryAudit,
    ObservabilitySource,
    ReceiptStatus,
    SignalReceipt,
    SourceProvider,
    SourceStatus,
)
from opspilot_api.observability.providers import (
    AlarmInfo,
    LogEntry,
    MetricSeries,
    ObservabilityProvider,
    ObsProviderError,
    provider_for_source,
    redact_log_message,
)
from opspilot_api.observability.schemas import (
    AlarmContext,
    AlarmTransition,
    IncidentObservability,
    LogLine,
    MetricComparison,
    MetricPoint,
    SignalSummary,
)
from opspilot_api.observability.telemetry import PROVIDER_QUERY_LATENCY, span

logger = logging.getLogger(__name__)
NOTICE = "Observability data is untrusted evidence and shows correlation, not proven causation."
EVIDENCE_CHARS = 1800
QUERYABLE = (SourceProvider.AWS_CLOUDWATCH.value, SourceProvider.DEMO_FIXTURE.value)
# Overridable in tests to inject a stubbed CloudWatch client; never configurable by callers.
CLIENT_FACTORY: Callable[[str], Any] | None = None


class QueryLogsInput(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    search_terms: list[str] = Field(default_factory=list, max_length=5)
    severity: Literal["FATAL", "CRITICAL", "ERROR", "WARN", "INFO"] | None = None
    limit: int = Field(default=20, ge=1, le=50)


class GetMetricsInput(BaseModel):
    metric_name: str | None = Field(default=None, max_length=255)
    start: datetime | None = None
    end: datetime | None = None
    period: int = Field(default=300, ge=60, le=3600)
    statistic: Literal["Average", "Sum", "Minimum", "Maximum", "SampleCount"] = "Average"


class CompareWindowsInput(BaseModel):
    metric_name: str | None = Field(default=None, max_length=255)
    baseline_minutes: int = Field(default=30, ge=5, le=240)
    incident_minutes: int = Field(default=30, ge=5, le=240)
    period: int = Field(default=300, ge=60, le=3600)
    statistic: Literal["Average", "Sum", "Minimum", "Maximum", "SampleCount"] = "Average"


class AlarmInput(BaseModel):
    lookback_hours: int = Field(default=6, ge=1, le=24)


class _Scope:
    def __init__(self, incident: Incident, service: MonitoredService, source: ObservabilitySource):
        self.incident = incident
        self.service = service
        self.source = source


def _unavailable(tool: str, summary: str, **data: Any) -> ToolResult:
    return ToolResult(
        tool_name=tool,
        summary=summary,
        evidence=[],
        data={"evidence_count": 0, "unavailable": True, **data},
    )


def _find_scope(session: Session, workspace_id: UUID, incident_id: UUID) -> _Scope | None:
    incident = session.scalar(
        select(Incident).where(Incident.id == incident_id, Incident.workspace_id == workspace_id)
    )
    if incident is None:
        raise LookupError("incident not found in workspace")
    if incident.service_id is None:
        return None
    service = session.scalar(
        select(MonitoredService).where(
            MonitoredService.id == incident.service_id,
            MonitoredService.workspace_id == workspace_id,
        )
    )
    if service is None:
        return None
    source = session.scalar(
        select(ObservabilitySource)
        .where(
            ObservabilitySource.workspace_id == workspace_id,
            ObservabilitySource.service_id == service.id,
            ObservabilitySource.provider.in_(QUERYABLE),
            ObservabilitySource.status != SourceStatus.DISABLED.value,
        )
        .order_by(ObservabilitySource.provider, ObservabilitySource.created_at)
        .limit(1)
    )
    return _Scope(incident, service, source) if source is not None else None


def _audited(
    session: Session,
    scope: _Scope,
    investigation_id: UUID | None,
    query_type: str,
    start: datetime,
    end: datetime,
    log_group: str | None,
    call: Callable[[], Any],
    count: Callable[[Any], int],
) -> Any:
    """Run a provider call with a latency metric, a span, and a result-free audit record."""
    began = time.perf_counter()
    status, failure_code, total, result = "ok", None, 0, None
    try:
        with span(
            "provider.query",
            source__id=scope.source.id,
            query__type=query_type,
            workspace__id=scope.source.workspace_id,
            provider=scope.source.provider,
        ):
            result = call()
        total = count(result)
        return result
    except ObsProviderError as exc:
        status, failure_code = "failed", exc.code
        raise
    finally:
        latency = time.perf_counter() - began
        PROVIDER_QUERY_LATENCY.labels(
            provider=scope.source.provider, query_type=query_type
        ).observe(latency)
        session.add(
            CloudWatchQueryAudit(
                workspace_id=scope.source.workspace_id,
                source_id=scope.source.id,
                investigation_id=investigation_id,
                query_type=query_type,
                log_group_hash=hashlib.sha256(log_group.encode()).hexdigest()
                if log_group
                else None,
                start_time=start,
                end_time=end,
                result_count=total,
                latency_ms=int(latency * 1000),
                status=status,
                failure_code=failure_code,
            )
        )
        session.commit()


def _provider(settings: Settings, scope: _Scope) -> ObservabilityProvider:
    return provider_for_source(settings, scope.source, CLIENT_FACTORY)


def _label(source: ObservabilitySource, kind: str, start: datetime, end: datetime) -> str:
    return f"[observability: {source.name} {kind} {start:%Y-%m-%d %H:%M}-{end:%H:%M}Z]"


def _log_window(scope: _Scope, params: QueryLogsInput) -> tuple[datetime, datetime]:
    opened = scope.incident.opened_at
    return params.start or opened - timedelta(minutes=30), params.end or opened + timedelta(
        minutes=10
    )


def _log_lines(entries: list[LogEntry], limit: int = 50) -> list[LogLine]:
    return [
        LogLine(
            timestamp=e.timestamp,
            message=redact_log_message(e.message),
            severity=e.severity,
            log_group=e.log_group,
        )
        for e in entries[:limit]
    ]


def _fmt_lines(lines: list[LogLine], count: int = 8) -> str:
    return "\n".join(
        f"{line.timestamp:%H:%M:%S} {line.severity or '-'} {line.message}" for line in lines[:count]
    )[:EVIDENCE_CHARS]


def _guard(tool: str, session: Session, run: Callable[[], ToolResult]) -> ToolResult:
    try:
        return run()
    except ObsProviderError as exc:
        return _unavailable(
            tool,
            f"Observability provider unavailable ({exc.code}); the investigation continues.",
            reason=exc.code,
            retryable=exc.retryable,
        )
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning("observability tool unavailable", extra={"error_class": type(exc).__name__})
        return _unavailable(
            tool, "Observability data was unavailable; the investigation continues."
        )
    except (LookupError, ValueError) as exc:
        return _unavailable(tool, f"Tool request rejected: {type(exc).__name__}")


def _no_source(tool: str) -> ToolResult:
    return ToolResult(
        tool_name=tool,
        summary="No observability source is configured for the affected service.",
        evidence=[],
        data={"evidence_count": 0, "has_observability_source": False},
    )


def query_service_logs(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = QueryLogsInput.model_validate(raw)
        scope = _find_scope(session, context.workspace_id, context.incident_id)
        if scope is None:
            return _no_source("query_service_logs")
        provider = _provider(context.settings, scope)
        start, end = _log_window(scope, params)
        start, end, clamped = provider.clamp_window(start, end)
        groups = provider.config.allowed_log_groups
        group = groups[0] if groups else None
        entries = _audited(
            session, scope, context.investigation_id, "logs", start, end, group,
            lambda: provider.query_logs(
                start, end, params.search_terms, params.severity, params.limit
            ),
            len,
        )  # fmt: skip
        lines = _log_lines(entries, params.limit)
        label = _label(scope.source, "logs", start, end)
        evidence = (
            [
                EvidenceDraft(
                    evidence_type="observability",
                    source_type="observability_log",
                    source_id=str(scope.source.id),
                    title=label,
                    excerpt=_fmt_lines(lines),
                    metadata={
                        "citation_label": label,
                        "source": scope.source.name,
                        "count": len(lines),
                        "window_clamped": clamped,
                        "untrusted": True,
                    },
                    collected_at=end,
                )
            ]
            if lines
            else []
        )
        return ToolResult(
            tool_name="query_service_logs",
            summary=f"Found {len(lines)} log entries." if lines else "No matching log entries.",
            evidence=evidence,
            data={"evidence_count": len(evidence), "count": len(lines), "window_clamped": clamped},
        )

    return _guard("query_service_logs", session, run)


def _series_average(series: MetricSeries) -> float | None:
    values = [value for _, value in series.points]
    return sum(values) / len(values) if values else None


def _metric_evidence(
    scope: _Scope, label: str, excerpt: str, end: datetime, data: dict[str, Any]
) -> EvidenceDraft:
    return EvidenceDraft(
        evidence_type="observability",
        source_type="observability_metric",
        source_id=str(scope.source.id),
        title=label,
        excerpt=excerpt[:EVIDENCE_CHARS],
        metadata={"citation_label": label, "source": scope.source.name, "untrusted": True, **data},
        collected_at=end,
    )


def get_service_metrics(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = GetMetricsInput.model_validate(raw)
        scope = _find_scope(session, context.workspace_id, context.incident_id)
        if scope is None:
            return _no_source("get_service_metrics")
        provider = _provider(context.settings, scope)
        opened = scope.incident.opened_at
        start, end, clamped = provider.clamp_window(
            params.start or opened - timedelta(minutes=30),
            params.end or opened + timedelta(minutes=10),
        )
        series: MetricSeries = _audited(
            session, scope, context.investigation_id, "metrics", start, end, None,
            lambda: provider.get_metric_statistics(
                params.metric_name, start, end, params.period, params.statistic
            ),
            lambda result: len(result.points),
        )  # fmt: skip
        average = _series_average(series)
        label = _label(scope.source, f"metric {series.metric_name}", start, end)
        excerpt = (
            f"{series.metric_name} {series.statistic}: {len(series.points)} points, "
            f"average {average:.2f}"
            if average is not None
            else f"{series.metric_name}: no datapoints"
        )
        evidence = [
            _metric_evidence(
                scope,
                label,
                excerpt,
                end,
                {"points": len(series.points), "window_clamped": clamped},
            )
        ]
        return ToolResult(
            tool_name="get_service_metrics",
            summary=excerpt,
            evidence=evidence if series.points else [],
            data={"evidence_count": len(evidence) if series.points else 0, "average": average},
        )

    return _guard("get_service_metrics", session, run)


def compare_windows(
    provider: ObservabilityProvider, params: CompareWindowsInput, opened: datetime
) -> tuple[MetricSeries, MetricSeries, datetime, datetime]:
    incident_end = opened + timedelta(minutes=5)
    incident_start = incident_end - timedelta(minutes=params.incident_minutes)
    baseline_start = incident_start - timedelta(minutes=params.baseline_minutes)
    baseline = provider.get_metric_statistics(
        params.metric_name, baseline_start, incident_start, params.period, params.statistic
    )
    current = provider.get_metric_statistics(
        params.metric_name, incident_start, incident_end, params.period, params.statistic
    )
    return baseline, current, incident_start, incident_end


def _comparison(baseline: MetricSeries, current: MetricSeries) -> MetricComparison:
    base_avg, cur_avg = _series_average(baseline), _series_average(current)
    change = (
        round((cur_avg - base_avg) / base_avg * 100, 2)
        if base_avg not in (None, 0) and cur_avg is not None
        else None
    )
    return MetricComparison(
        metric_name=current.metric_name,
        statistic=current.statistic,
        unit=current.unit,
        baseline_average=base_avg,
        incident_average=cur_avg,
        change_percent=change,
        baseline_points=[MetricPoint(timestamp=t, value=v) for t, v in baseline.points[:100]],
        incident_points=[MetricPoint(timestamp=t, value=v) for t, v in current.points[:100]],
    )


def compare_metric_windows(
    session: Session, context: ToolContext, raw: dict[str, Any]
) -> ToolResult:
    def run() -> ToolResult:
        params = CompareWindowsInput.model_validate(raw)
        scope = _find_scope(session, context.workspace_id, context.incident_id)
        if scope is None:
            return _no_source("compare_metric_windows")
        provider = _provider(context.settings, scope)
        pair = _audited(
            session, scope, context.investigation_id, "metric_compare",
            scope.incident.opened_at
            - timedelta(minutes=params.baseline_minutes + params.incident_minutes),
            scope.incident.opened_at + timedelta(minutes=5), None,
            lambda: compare_windows(provider, params, scope.incident.opened_at),
            lambda result: len(result[0].points) + len(result[1].points),
        )  # fmt: skip
        baseline, current, start, end = pair
        comparison = _comparison(baseline, current)
        change = comparison.change_percent if comparison.change_percent is not None else "n/a"
        label = _label(scope.source, f"metric-compare {current.metric_name}", start, end)
        if comparison.incident_average is None or comparison.baseline_average is None:
            return ToolResult(
                tool_name="compare_metric_windows",
                summary="Not enough datapoints to compare windows.",
                evidence=[],
                data={"evidence_count": 0},
            )
        excerpt = (
            f"{current.metric_name} {current.statistic}: baseline "
            f"{comparison.baseline_average:.2f} -> incident {comparison.incident_average:.2f} "
            f"({change}% change). Correlation only."
        )
        return ToolResult(
            tool_name="compare_metric_windows",
            summary=excerpt,
            evidence=[
                _metric_evidence(
                    scope, label, excerpt, end, {"change_percent": comparison.change_percent}
                )
            ],
            data={"evidence_count": 1, "change_percent": comparison.change_percent},
        )

    return _guard("compare_metric_windows", session, run)


def _alarm_context(info: AlarmInfo) -> AlarmContext:
    return AlarmContext(
        alarm_name=info.name,
        state=info.state,
        metric_name=info.metric_name,
        threshold=info.threshold,
        comparison=info.comparison,
        period_seconds=info.period_seconds,
        transitions=[
            AlarmTransition(
                at=at, from_state=old, to_state=new, reason=(reason or "")[:300] or None
            )
            for at, old, new, reason in info.transitions[:10]
        ],
    )


def _related_signals(
    session: Session, scope: _Scope, limit: int = 3
) -> list[tuple[SignalReceipt, str | None]]:
    rows = session.scalars(
        select(SignalReceipt)
        .where(
            SignalReceipt.workspace_id == scope.source.workspace_id,
            SignalReceipt.service_id == scope.service.id,
            SignalReceipt.status == ReceiptStatus.PROCESSED.value,
            SignalReceipt.signal_type.in_(["alert", "metric_threshold"]),
            SignalReceipt.received_at >= scope.incident.opened_at - timedelta(hours=24),
        )
        .order_by(SignalReceipt.received_at.desc())
        .limit(limit)
    )
    return [(row, scope.source.name) for row in rows]


def get_alarm_context(session: Session, context: ToolContext, raw: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        params = AlarmInput.model_validate(raw)
        scope = _find_scope(session, context.workspace_id, context.incident_id)
        if scope is None:
            return _no_source("get_alarm_context")
        provider = _provider(context.settings, scope)
        opened = scope.incident.opened_at
        start, end = opened - timedelta(hours=params.lookback_hours), opened + timedelta(minutes=10)
        info = _audited(
            session, scope, context.investigation_id, "alarm", start, end, None,
            lambda: provider.get_alarm(start, end),
            lambda result: 0 if result is None else 1 + len(result.transitions),
        )  # fmt: skip
        evidence: list[EvidenceDraft] = []
        if info is not None:
            label = _label(scope.source, f"alarm {info.name}", start, end)
            moves = "; ".join(
                f"{at:%H:%M} {old or '?'}->{new}" for at, old, new, _ in info.transitions[:5]
            )
            evidence.append(
                EvidenceDraft(
                    evidence_type="observability",
                    source_type="observability_alarm",
                    source_id=str(scope.source.id),
                    title=label,
                    excerpt=(
                        f"Alarm {info.name} is {info.state}; threshold {info.threshold} "
                        f"({info.comparison}); transitions: {moves or 'none'}"
                    )[:EVIDENCE_CHARS],
                    metadata={"citation_label": label, "state": info.state, "untrusted": True},
                    collected_at=end,
                )
            )
        for receipt, name in _related_signals(session, scope):
            label = (
                f"[signal: {receipt.provider} {str(receipt.signal_id)[:8]} {receipt.title or ''}]"[
                    :240
                ]
            )
            evidence.append(
                EvidenceDraft(
                    evidence_type="observability",
                    source_type="normalized_signal",
                    source_id=str(receipt.signal_id),
                    title=label,
                    excerpt=redact_log_message(
                        f"{receipt.signal_type} {receipt.severity}: {receipt.title} (source {name})"
                    ),
                    metadata={"citation_label": label, "untrusted": True},
                    collected_at=receipt.occurred_at or receipt.received_at,
                )
            )
        return ToolResult(
            tool_name="get_alarm_context",
            summary=(
                f"Alarm {info.name} is {info.state}."
                if info is not None
                else "No alarm configured."
            ),
            evidence=evidence,
            data={"evidence_count": len(evidence)},
        )

    return _guard("get_alarm_context", session, run)


OBSERVABILITY_TOOLS: dict[str, Callable[[Session, ToolContext, dict[str, Any]], ToolResult]] = {
    "query_service_logs": query_service_logs,
    "get_service_metrics": get_service_metrics,
    "compare_metric_windows": compare_metric_windows,
    "get_alarm_context": get_alarm_context,
}


def signal_summary(
    receipt: SignalReceipt, source_name: str | None, service_name: str | None
) -> SignalSummary:
    return SignalSummary(
        signal_id=receipt.signal_id,
        source_id=receipt.source_id,
        source_name=source_name,
        provider=receipt.provider,
        service_id=receipt.service_id,
        service_name=service_name,
        signal_type=receipt.signal_type,
        severity=receipt.severity,
        title=receipt.title,
        status=receipt.status,
        received_at=receipt.received_at,
        occurred_at=receipt.occurred_at,
        processed_at=receipt.processed_at,
        incident_id=receipt.incident_id,
        ingest_result=receipt.ingest_result,
        deduplication_key=receipt.deduplication_key,
        duplicate_of=receipt.duplicate_of,
        attempt_count=receipt.attempt_count,
        failure_code=receipt.failure_code,
    )


def collect_incident_observability(
    session: Session, settings: Settings, workspace_id: UUID, incident_id: UUID
) -> IncidentObservability:
    """Bounded, redacted view for the incident page; provider failures are reported, not raised."""
    incident = session.scalar(
        select(Incident).where(Incident.id == incident_id, Incident.workspace_id == workspace_id)
    )
    if incident is None:
        raise LookupError("incident not found in workspace")
    signals = [
        signal_summary(row, name, None)
        for row, name in (
            (r, None)
            for r in session.scalars(
                select(SignalReceipt)
                .where(
                    SignalReceipt.workspace_id == workspace_id,
                    SignalReceipt.incident_id == incident_id,
                )
                .order_by(SignalReceipt.received_at.desc())
                .limit(10)
            )
        )
    ]
    scope = _find_scope(session, workspace_id, incident_id)
    base = {"incident_id": incident_id, "signals": signals, "notice": NOTICE}
    if scope is None:
        return IncidentObservability(
            has_source=False, source_id=None, source_name=None, provider=None,
            logs=[], metric=None, alarm=None, unavailable=None, **base,
        )  # fmt: skip
    result: dict[str, Any] = {"logs": [], "metric": None, "alarm": None, "unavailable": None}
    try:
        provider = _provider(settings, scope)
        opened = incident.opened_at
        start, end, _ = provider.clamp_window(
            opened - timedelta(minutes=30), opened + timedelta(minutes=10)
        )
        entries = _audited(
            session, scope, None, "logs", start, end, None,
            lambda: provider.query_logs(start, end, [], None, 20), len,
        )  # fmt: skip
        result["logs"] = _log_lines(entries)
        compare = CompareWindowsInput()
        baseline, current, *_ = _audited(
            session, scope, None, "metric_compare", start, end, None,
            lambda: compare_windows(provider, compare, opened),
            lambda pair: len(pair[0].points) + len(pair[1].points),
        )  # fmt: skip
        result["metric"] = _comparison(baseline, current)
        info = _audited(
            session, scope, None, "alarm", start, end, None,
            lambda: provider.get_alarm(opened - timedelta(hours=6), end),
            lambda found: 0 if found is None else 1,
        )  # fmt: skip
        result["alarm"] = _alarm_context(info) if info is not None else None
    except ObsProviderError as exc:
        result["unavailable"] = exc.code
    except SQLAlchemyError:
        session.rollback()
        result["unavailable"] = "storage_unavailable"
    return IncidentObservability(
        has_source=True,
        source_id=scope.source.id,
        source_name=scope.source.name,
        provider=scope.source.provider,
        **result,
        **base,
    )
