"""Versioned canonical signal envelopes (raw and normalized).

Every message is validated before publication and again before consumption. A schema version
other than the current one is *not* a valid current message and is never processed as one.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot_api.incidents.models import Environment, Severity

CURRENT_SCHEMA_VERSION = "1"
MAX_DIMENSIONS = 20
MAX_ATTRIBUTES_BYTES = 16 * 1024
MAX_RAW_PAYLOAD_BYTES = 128 * 1024


class SignalType(StrEnum):
    ALERT = "alert"
    LOG_ERROR = "log_error"
    METRIC_THRESHOLD = "metric_threshold"
    AVAILABILITY = "availability"
    DEPLOYMENT = "deployment"
    SECURITY = "security"
    CUSTOM = "custom"


class SignalProvider(StrEnum):
    GENERIC_WEBHOOK = "generic_webhook"
    AWS_CLOUDWATCH = "aws_cloudwatch"
    DEMO_FIXTURE = "demo_fixture"


class UnsupportedSchemaError(ValueError):
    code = "unsupported_schema_version"


class InvalidSignalError(ValueError):
    code = "invalid_signal"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


class RawSignalEnvelope(BaseModel):
    """What the webhook/provider ingress publishes to the raw topic (already redacted)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    signal_id: UUID
    workspace_id: UUID
    service_id: UUID
    source_id: UUID
    provider: SignalProvider
    ingress_key: str = Field(min_length=1, max_length=128)
    provider_event_id: str | None = Field(default=None, max_length=200)
    content_type: str = Field(default="application/json", max_length=100)
    payload: dict[str, Any]
    payload_hash: str = Field(min_length=64, max_length=64)
    environment: Environment
    region: str | None = Field(default=None, max_length=40)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _received(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _bounded(self) -> "RawSignalEnvelope":
        if _json_size(self.payload) > MAX_RAW_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the raw envelope bound")
        return self


class NormalizedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    signal_id: UUID
    workspace_id: UUID
    service_id: UUID
    source_id: UUID
    provider: SignalProvider
    provider_event_id: str | None = Field(default=None, max_length=200)
    signal_type: SignalType
    severity: Severity
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(max_length=2000)
    error_signature: str | None = Field(default=None, max_length=300)
    environment: Environment
    region: str | None = Field(default=None, max_length=40)
    resource_type: str | None = Field(default=None, max_length=100)
    resource_id: str | None = Field(default=None, max_length=300)
    trace_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=128)
    metric_name: str | None = Field(default=None, max_length=200)
    metric_value: float | None = None
    metric_unit: str | None = Field(default=None, max_length=40)
    threshold: float | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime
    payload_hash: str = Field(min_length=64, max_length=64)
    deduplication_key: str = Field(min_length=1, max_length=128)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("dimensions")
    @classmethod
    def _dimensions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_DIMENSIONS:
            raise ValueError("too many dimensions")
        if any(len(k) > 100 or len(v) > 300 for k, v in value.items()):
            raise ValueError("dimension key or value too long")
        return value

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _json_size(value) > MAX_ATTRIBUTES_BYTES:
            raise ValueError("attributes exceed the bound")
        return value


def _check_version(data: Any) -> None:
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(f"unsupported schema_version {str(version)[:20]!r}")


def parse_raw(data: bytes | dict[str, Any]) -> RawSignalEnvelope:
    parsed = json.loads(data) if isinstance(data, bytes | str) else data
    _check_version(parsed)
    try:
        return RawSignalEnvelope.model_validate(parsed)
    except ValueError as exc:
        raise InvalidSignalError("raw signal failed schema validation") from exc


def parse_normalized(data: bytes | dict[str, Any]) -> NormalizedSignal:
    parsed = json.loads(data) if isinstance(data, bytes | str) else data
    _check_version(parsed)
    try:
        return NormalizedSignal.model_validate(parsed)
    except ValueError as exc:
        raise InvalidSignalError("normalized signal failed schema validation") from exc


def partition_key(workspace_id: UUID, service_id: UUID) -> str:
    """Workspace + service identity keeps related signals ordered within one partition."""
    return f"{workspace_id}:{service_id}"
