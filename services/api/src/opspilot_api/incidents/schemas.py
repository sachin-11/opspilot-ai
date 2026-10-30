"""Validated API contracts for incident management and real-time events."""

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opspilot_api.incidents.models import Environment, IncidentStatus, Severity

MAX_ATTRIBUTES_BYTES = 64 * 1024
SENSITIVE_KEYS = {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}


def reject_sensitive(value: Any, path: str = "attributes") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEYS):
                raise ValueError(f"sensitive key is not allowed in {path}")
            reject_sensitive(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for item in value:
            reject_sensitive(item, path)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=80)


class WorkspaceResponse(ORMModel):
    id: UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    environment: Environment


class ServiceResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    description: str | None
    environment: Environment
    created_at: datetime
    updated_at: datetime


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=5000)
    service_id: UUID | None = None
    severity: Severity


class IncidentResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    service_id: UUID | None
    service_name: str | None = None
    service_environment: Environment | None = None
    title: str
    description: str | None
    severity: Severity
    status: IncidentStatus
    source: Literal["manual", "event_ingestion"]
    fingerprint: str | None
    version: int
    opened_at: datetime
    acknowledged_at: datetime | None
    mitigated_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IncidentListResponse(BaseModel):
    items: list[IncidentResponse]
    next_cursor: str | None


class StatusUpdate(BaseModel):
    status: IncidentStatus
    version: int = Field(ge=1)


class SeverityUpdate(BaseModel):
    severity: Severity
    version: int = Field(ge=1)


class NoteCreate(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)


class TimelineResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    incident_id: UUID
    event_type: str
    actor_type: str
    actor_id: str | None
    summary: str
    metadata: dict[str, Any] = Field(validation_alias="event_metadata")
    occurred_at: datetime
    created_at: datetime


class EventIngestRequest(BaseModel):
    service_id: UUID
    external_id: str | None = Field(default=None, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=100)
    severity: Severity
    summary: str = Field(min_length=1, max_length=500)
    error_signature: str | None = Field(default=None, max_length=300)
    attributes: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        reject_sensitive(value)
        if (
            len(json.dumps(value, separators=(",", ":"), default=str).encode())
            > MAX_ATTRIBUTES_BYTES
        ):
            raise ValueError("attributes exceed 64 KiB")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class EventIngestResponse(BaseModel):
    event_id: UUID
    incident_id: UUID
    result: Literal["created", "correlated"]


class RealtimeEnvelope(BaseModel):
    event_id: UUID
    type: str
    workspace_id: UUID
    incident_id: UUID | None
    occurred_at: datetime
    data: dict[str, Any]
