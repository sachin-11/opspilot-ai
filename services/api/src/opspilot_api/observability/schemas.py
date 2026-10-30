"""Pydantic API shapes for observability sources, signals, and pipeline status."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opspilot_api.incidents.models import Environment
from opspilot_api.observability.models import SourceProvider, SourceStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: SourceProvider
    service_id: UUID
    environment: Environment
    region: str | None = Field(default=None, max_length=40)
    configuration: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return value.strip()


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["active", "disabled"] | None = None
    environment: Environment | None = None
    region: str | None = Field(default=None, max_length=40)
    configuration: dict[str, Any] | None = None


class SourceResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    service_id: UUID
    service_name: str | None = None
    public_id: str
    name: str
    provider: SourceProvider
    source_type: str
    status: SourceStatus
    environment: Environment
    region: str | None
    configuration: dict[str, Any]
    has_secret: bool
    webhook_path: str | None
    last_event_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceCreated(SourceResponse):
    """Returned once on creation; ``webhook_secret`` is never retrievable again."""

    webhook_secret: str | None = None


class SecretRotated(BaseModel):
    source_id: UUID
    webhook_secret: str
    rotated_at: datetime


class SourceTestResult(BaseModel):
    ok: bool
    status: Literal["ok", "throttled", "auth_failed", "unavailable", "misconfigured"]
    message: str
    latency_ms: int


class SignalSummary(BaseModel):
    signal_id: UUID
    source_id: UUID
    source_name: str | None
    provider: str
    service_id: UUID
    service_name: str | None
    signal_type: str | None
    severity: str | None
    title: str | None
    status: str
    received_at: datetime
    occurred_at: datetime | None
    processed_at: datetime | None
    incident_id: UUID | None
    ingest_result: str | None
    deduplication_key: str
    duplicate_of: UUID | None
    attempt_count: int
    failure_code: str | None


class SignalDetail(SignalSummary):
    provider_event_id: str | None
    payload_hash: str
    schema_version: str
    failure_message: str | None
    normalized: dict[str, Any] | None


class SignalPage(BaseModel):
    items: list[SignalSummary]
    next_cursor: str | None


class SourceHealth(BaseModel):
    id: UUID
    name: str
    provider: str
    status: str
    last_event_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


class BackpressureStatus(BaseModel):
    active: bool
    reasons: list[str]


class PipelineStatus(BaseModel):
    window_minutes: int
    generated_at: datetime
    accepted: int
    processed: int
    duplicates: int
    failures: int
    dead_letters_open: int
    events_per_second: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    incidents_created: int
    events_correlated: int
    consumer_lag: int | None
    lag_available: bool
    broker_healthy: bool
    broker_backend: str
    backpressure: BackpressureStatus
    sources: list[SourceHealth]


class DeadLetterResponse(BaseModel):
    id: UUID
    workspace_id: UUID | None
    source_id: UUID | None
    source_name: str | None
    signal_id: UUID | None
    original_topic: str
    partition: int | None
    offset: int | None
    failure_code: str
    failure_class: Literal["retryable_exhausted", "non_retryable"]
    failure_message: str
    attempt_count: int
    first_failed_at: datetime
    last_failed_at: datetime
    replayed_at: datetime | None
    resolved_at: datetime | None
    replay_count: int
    replayable: bool
    payload_preview: dict[str, Any] | None


class DeadLetterPage(BaseModel):
    items: list[DeadLetterResponse]
    next_cursor: str | None


class ReplayResponse(BaseModel):
    status: Literal["replayed", "already_replayed"]
    dead_letter_id: UUID
    replayed_at: datetime | None
    replay_count: int


class DemoGenerateRequest(BaseModel):
    source_id: UUID | None = None
    count: int = Field(default=20, ge=1, le=200)
    duplicate_percent: int = Field(default=10, ge=0, le=50)
    invalid_percent: int = Field(default=0, ge=0, le=20)


class DemoGenerateResponse(BaseModel):
    source_id: UUID
    accepted: int
    duplicates: int
    rejected: int


class WebhookAccepted(BaseModel):
    status: Literal["accepted", "duplicate"]
    signal_id: UUID


class LogLine(BaseModel):
    timestamp: datetime
    message: str
    severity: str | None = None
    log_group: str | None = None


class MetricPoint(BaseModel):
    timestamp: datetime
    value: float


class MetricComparison(BaseModel):
    metric_name: str
    statistic: str
    unit: str | None
    baseline_average: float | None
    incident_average: float | None
    change_percent: float | None
    baseline_points: list[MetricPoint]
    incident_points: list[MetricPoint]


class AlarmTransition(BaseModel):
    at: datetime
    from_state: str | None
    to_state: str
    reason: str | None


class AlarmContext(BaseModel):
    alarm_name: str
    state: str
    metric_name: str | None
    threshold: float | None
    comparison: str | None
    period_seconds: int | None
    transitions: list[AlarmTransition]


class IncidentObservability(BaseModel):
    incident_id: UUID
    has_source: bool
    source_id: UUID | None
    source_name: str | None
    provider: str | None
    signals: list[SignalSummary]
    logs: list[LogLine]
    metric: MetricComparison | None
    alarm: AlarmContext | None
    unavailable: str | None
    notice: str
