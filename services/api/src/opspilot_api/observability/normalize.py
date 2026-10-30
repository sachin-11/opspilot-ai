"""Deterministic provider-specific normalization into the canonical signal schema.

No LLM and no guessing: required fields are validated explicitly, strings and JSON are bounded and
redacted, unknown fields survive only inside bounded sanitized attributes, and every failure is a
``NormalizationError`` (non-retryable) so it is dead-lettered rather than silently dropped.
"""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from opspilot_api.incidents.models import Severity
from opspilot_api.investigations.safety import redact_text, redact_value
from opspilot_api.observability.signal_schema import (
    CURRENT_SCHEMA_VERSION,
    MAX_ATTRIBUTES_BYTES,
    NormalizedSignal,
    RawSignalEnvelope,
    SignalType,
)

MAX_PAST = timedelta(days=30)
MAX_FUTURE = timedelta(minutes=5)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_QUOTED = re.compile(r"(['\"])(?:(?!\1).){0,80}\1")
_EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Timeout|Failure))\b")
_SEVERITY_WORDS = {
    "critical": Severity.SEV1, "fatal": Severity.SEV1, "emergency": Severity.SEV1,
    "p1": Severity.SEV1, "sev1": Severity.SEV1, "sev0": Severity.SEV1,
    "high": Severity.SEV2, "error": Severity.SEV2, "major": Severity.SEV2, "p2": Severity.SEV2,
    "sev2": Severity.SEV2, "alarm": Severity.SEV2,
    "medium": Severity.SEV3, "warning": Severity.SEV3, "warn": Severity.SEV3,
    "moderate": Severity.SEV3,
    "p3": Severity.SEV3, "sev3": Severity.SEV3,
    "low": Severity.SEV4, "info": Severity.SEV4, "minor": Severity.SEV4, "ok": Severity.SEV4,
    "p4": Severity.SEV4, "sev4": Severity.SEV4, "resolved": Severity.SEV4,
}  # fmt: skip
_LOG_LEVEL_SEVERITY = {
    "fatal": Severity.SEV1, "critical": Severity.SEV1, "emergency": Severity.SEV1,
    "error": Severity.SEV3, "err": Severity.SEV3,
}  # fmt: skip
_ALERT_KINDS = {
    "alert": SignalType.ALERT,
    "availability": SignalType.AVAILABILITY,
    "security": SignalType.SECURITY,
    "custom": SignalType.CUSTOM,
}
_KNOWN_ALERT_FIELDS = {
    "type", "title", "message", "severity", "occurred_at", "event_id", "error_signature",
    "resource_type", "resource_id", "trace_id", "correlation_id", "dimensions", "attributes",
}  # fmt: skip


class NormalizationError(ValueError):
    code = "invalid_payload"

    def __init__(self, message: str, code: str = "invalid_payload") -> None:
        super().__init__(message)
        self.code = code


def clean_text(value: Any, limit: int, *, required: str | None = None) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise NormalizationError(f"{required} is required", "missing_required_field")
        return ""
    if not isinstance(value, str | int | float):
        raise NormalizationError(f"{required or 'field'} must be text")
    text = _CONTROL.sub("", str(value)).strip()
    return redact_text(text, max_length=limit)


def parse_timestamp(value: Any, received_at: datetime, field: str = "occurred_at") -> datetime:
    if value is None or value == "":
        raise NormalizationError(f"{field} is required", "missing_required_field")
    if isinstance(value, bool):
        raise NormalizationError(f"{field} is not a timestamp", "invalid_timestamp")
    try:
        if isinstance(value, int | float):
            seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            parsed = datetime.fromtimestamp(seconds, UTC)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise NormalizationError(f"{field} must include a timezone", "invalid_timestamp")
    except (ValueError, OverflowError, OSError) as exc:
        raise NormalizationError(f"{field} is not a valid timestamp", "invalid_timestamp") from exc
    parsed = parsed.astimezone(UTC)
    if parsed > received_at + MAX_FUTURE or parsed < received_at - MAX_PAST:
        raise NormalizationError(f"{field} is outside the accepted range", "invalid_timestamp")
    return parsed


def map_severity(value: Any, *, required: bool = True) -> Severity:
    if value is None or value == "":
        if required:
            raise NormalizationError("severity is required", "missing_required_field")
        return Severity.SEV3
    word = str(value).strip().lower()
    if word in _SEVERITY_WORDS:
        return _SEVERITY_WORDS[word]
    raise NormalizationError(f"unrecognized severity {word[:20]!r}", "invalid_severity")


def error_signature_for(message: str) -> str:
    """Stable signature: volatile ids, numbers, and quoted values collapse to placeholders."""
    text = _QUOTED.sub("<s>", message)
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text.lower())
    text = " ".join(text.split())[:200]
    found = _EXCEPTION.search(message)
    prefix = found.group(1) if found else "error"
    return f"{prefix}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def bounded_dimensions(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NormalizationError("dimensions must be an object")
    result: dict[str, str] = {}
    for key, item in list(value.items())[:20]:
        if isinstance(item, dict | list):
            continue
        result[clean_text(key, 100)] = clean_text(item, 300)
    return {str(k): str(v) for k, v in redact_value(result).items()}  # key-aware redaction


def bounded_attributes(extra: dict[str, Any]) -> dict[str, Any]:
    cleaned = redact_value({str(k)[:100]: _trim(v) for k, v in list(extra.items())[:30]})
    while len(json.dumps(cleaned, default=str).encode()) > MAX_ATTRIBUTES_BYTES and cleaned:
        cleaned.popitem()
    return dict(cleaned)


def _trim(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict) and depth < 2:
        return {str(k)[:100]: _trim(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list) and depth < 2:
        return [_trim(v, depth + 1) for v in value[:20]]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)[:500]


def fingerprint_key(
    raw: RawSignalEnvelope, signal_type: SignalType, signature: str | None, title: str
) -> str:
    material = "|".join(
        [str(raw.workspace_id), str(raw.service_id), str(raw.source_id), signal_type.value,
         signature or "", title.lower()]
    )  # fmt: skip
    return "fp:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def deduplication_key_for(
    raw: RawSignalEnvelope, signal_type: SignalType, signature: str | None, title: str
) -> str:
    """Priority: provider event ID, then source idempotency key, then a normalized fingerprint."""
    if raw.provider_event_id:
        digest = hashlib.sha256(f"{raw.source_id}:{raw.provider_event_id}".encode()).hexdigest()
        return "evt:" + digest[:32]
    if raw.ingress_key.startswith("idem:"):
        return raw.ingress_key[:128]
    return fingerprint_key(raw, signal_type, signature, title)


def _base(
    raw: RawSignalEnvelope,
    *,
    signal_type: SignalType,
    severity: Severity,
    title: str,
    message: str,
    signature: str | None,
    occurred_at: datetime,
    **fields: Any,
) -> NormalizedSignal:
    return NormalizedSignal(
        schema_version=CURRENT_SCHEMA_VERSION,
        signal_id=raw.signal_id,
        workspace_id=raw.workspace_id,
        service_id=raw.service_id,
        source_id=raw.source_id,
        provider=raw.provider,
        provider_event_id=raw.provider_event_id,
        signal_type=signal_type,
        severity=severity,
        title=title,
        message=message,
        error_signature=signature,
        environment=raw.environment,
        region=raw.region,
        occurred_at=occurred_at,
        received_at=raw.received_at,
        payload_hash=raw.payload_hash,
        deduplication_key=deduplication_key_for(raw, signal_type, signature, title),
        **fields,
    )


def _optional(payload: dict[str, Any], key: str, limit: int) -> str | None:
    value = clean_text(payload.get(key), limit)
    return value or None


def normalize_generic_alert(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    signal_type = _ALERT_KINDS[str(payload.get("type"))]
    title = clean_text(payload.get("title"), 300, required="title")
    message = clean_text(payload.get("message"), 2000)
    severity = map_severity(payload.get("severity"))
    occurred = parse_timestamp(payload.get("occurred_at"), raw.received_at)
    signature = (
        _optional(payload, "error_signature", 300)
        or f"{signal_type.value}:{hashlib.sha256(title.lower().encode()).hexdigest()[:12]}"
    )
    extra = {k: v for k, v in payload.items() if k not in _KNOWN_ALERT_FIELDS}
    declared = payload.get("attributes")
    if isinstance(declared, dict):
        extra = {**declared, **extra}
    return _base(
        raw,
        signal_type=signal_type,
        severity=severity,
        title=title,
        message=message,
        signature=signature,
        occurred_at=occurred,
        resource_type=_optional(payload, "resource_type", 100),
        resource_id=_optional(payload, "resource_id", 300),
        trace_id=_optional(payload, "trace_id", 64),
        correlation_id=_optional(payload, "correlation_id", 128),
        dimensions=bounded_dimensions(payload.get("dimensions")),
        attributes=bounded_attributes(extra),
    )


def normalize_generic_log(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    level = clean_text(payload.get("level"), 20, required="level").lower()
    if level not in _LOG_LEVEL_SEVERITY:
        raise NormalizationError(
            f"log level {level[:20]!r} is below the error threshold", "unsupported_log_level"
        )
    message = clean_text(payload.get("message"), 2000, required="message")
    occurred = parse_timestamp(
        payload.get("timestamp", payload.get("occurred_at")), raw.received_at
    )
    signature = _optional(payload, "error_signature", 300) or error_signature_for(message)
    title = message.splitlines()[0][:300]
    known = {"type", "level", "message", "timestamp", "occurred_at", "event_id", "error_signature",
             "resource_type", "resource_id", "trace_id", "correlation_id",
             "dimensions"}  # fmt: skip
    return _base(
        raw,
        signal_type=SignalType.LOG_ERROR,
        severity=_LOG_LEVEL_SEVERITY[level],
        title=title,
        message=message,
        signature=signature,
        occurred_at=occurred,
        resource_type=_optional(payload, "resource_type", 100),
        resource_id=_optional(payload, "resource_id", 300),
        trace_id=_optional(payload, "trace_id", 64),
        correlation_id=_optional(payload, "correlation_id", 128),
        dimensions=bounded_dimensions(payload.get("dimensions")),
        attributes=bounded_attributes({k: v for k, v in payload.items() if k not in known}),
    )


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NormalizationError(f"{key} must be a number", "missing_required_field")
    return float(value)


def normalize_generic_metric(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    metric = clean_text(payload.get("metric_name"), 200, required="metric_name")
    value, threshold = _number(payload, "value"), _number(payload, "threshold")
    occurred = parse_timestamp(payload.get("occurred_at"), raw.received_at)
    title = clean_text(payload.get("title"), 300) or f"{metric} crossed threshold {threshold:g}"
    known = {"type", "metric_name", "value", "threshold", "unit", "severity", "occurred_at",
             "event_id", "title", "message", "dimensions", "error_signature"}  # fmt: skip
    return _base(
        raw,
        signal_type=SignalType.METRIC_THRESHOLD,
        severity=map_severity(payload.get("severity")),
        title=title,
        message=clean_text(payload.get("message"), 2000)
        or f"{metric}={value:g} (threshold {threshold:g})",
        signature=_optional(payload, "error_signature", 300) or f"metric:{metric}",
        occurred_at=occurred,
        metric_name=metric,
        metric_value=value,
        metric_unit=_optional(payload, "unit", 40),
        threshold=threshold,
        dimensions=bounded_dimensions(payload.get("dimensions")),
        attributes=bounded_attributes({k: v for k, v in payload.items() if k not in known}),
    )


def normalize_deployment(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    sha = clean_text(payload.get("commit_sha", payload.get("sha")), 64, required="commit_sha")
    status = clean_text(payload.get("status"), 30) or "success"
    environment = clean_text(payload.get("environment"), 100, required="environment")
    occurred = parse_timestamp(payload.get("occurred_at"), raw.received_at)
    repository = _optional(payload, "repository", 200)
    return _base(
        raw,
        signal_type=SignalType.DEPLOYMENT,
        severity=Severity.SEV4,
        title=f"Deployment {status} {sha[:7]} to {environment}",
        message=(
            f"{repository + ' ' if repository else ''}{sha[:12]} deployed to {environment}: "
            f"{status}"
        ),
        signature=f"deployment:{environment}",
        occurred_at=occurred,
        resource_type="deployment",
        resource_id=sha[:40],
        # Module 4 compatibility: same commit SHA/repository keys as the repository integration.
        attributes=bounded_attributes(
            {
                "commit_sha": sha,
                "repository": repository,
                "environment": environment,
                "status": status,
            }
        ),
    )


_ALARM_STATE_SEVERITY = {
    "ALARM": Severity.SEV2,
    "INSUFFICIENT_DATA": Severity.SEV3,
    "OK": Severity.SEV4,
}


def normalize_cloudwatch_alarm(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    name = clean_text(payload.get("AlarmName"), 255, required="AlarmName")
    state = clean_text(payload.get("NewStateValue"), 30, required="NewStateValue").upper()
    if state not in _ALARM_STATE_SEVERITY:
        raise NormalizationError(f"unknown alarm state {state[:20]!r}", "invalid_severity")
    raw_trigger = payload.get("Trigger")
    trigger: dict[str, Any] = raw_trigger if isinstance(raw_trigger, dict) else {}
    occurred = parse_timestamp(payload.get("StateChangeTime"), raw.received_at, "StateChangeTime")
    reason = clean_text(payload.get("NewStateReason"), 2000)
    threshold = trigger.get("Threshold")
    dimensions = {
        clean_text(item.get("name"), 100): clean_text(item.get("value"), 300)
        for item in (trigger.get("Dimensions") or [])[:20]
        if isinstance(item, dict)
    }
    return _base(
        raw,
        signal_type=SignalType.ALERT,
        severity=_ALARM_STATE_SEVERITY[state],
        title=f"{name} is {state}",
        message=reason or f"{name} changed to {state}",
        signature=f"alarm:{name}",
        occurred_at=occurred,
        resource_type="cloudwatch_alarm",
        resource_id=name,
        metric_name=clean_text(trigger.get("MetricName"), 200) or None,
        threshold=float(threshold) if isinstance(threshold, int | float) else None,
        dimensions=dimensions,
        attributes=bounded_attributes(
            {
                "state": state,
                "namespace": trigger.get("Namespace"),
                "period": trigger.get("Period"),
                "account": payload.get("AWSAccountId"),
                "region": payload.get("Region"),
            }
        ),
    )


def normalize_cloudwatch_log(raw: RawSignalEnvelope) -> NormalizedSignal:
    payload = raw.payload
    message = clean_text(payload.get("message"), 2000, required="message")
    group = clean_text(payload.get("logGroup"), 512, required="logGroup")
    occurred = parse_timestamp(payload.get("timestamp"), raw.received_at, "timestamp")
    level = clean_text(payload.get("level"), 20).lower() or "error"
    if level not in _LOG_LEVEL_SEVERITY:
        raise NormalizationError(
            f"log level {level[:20]!r} is below the error threshold", "unsupported_log_level"
        )
    return _base(
        raw,
        signal_type=SignalType.LOG_ERROR,
        severity=_LOG_LEVEL_SEVERITY[level],
        title=message.splitlines()[0][:300],
        message=message,
        signature=error_signature_for(message),
        occurred_at=occurred,
        resource_type="log_group",
        resource_id=group,
        attributes=bounded_attributes({"logStream": payload.get("logStream")}),
    )


Normalizer = Callable[[RawSignalEnvelope], NormalizedSignal]


def select_normalizer(payload: dict[str, Any]) -> Normalizer:
    """Explicit shape/type dispatch; anything unrecognized is an error, never a guess."""
    if "AlarmName" in payload and "NewStateValue" in payload:
        return normalize_cloudwatch_alarm
    if "logGroup" in payload and "message" in payload:
        return normalize_cloudwatch_log
    kind = payload.get("type")
    if kind in _ALERT_KINDS:
        return normalize_generic_alert
    if kind == "log":
        return normalize_generic_log
    if kind == "metric":
        return normalize_generic_metric
    if kind == "deployment":
        return normalize_deployment
    raise NormalizationError(
        f"unrecognized signal type {str(kind)[:30]!r}", "unsupported_signal_type"
    )


def normalize(raw: RawSignalEnvelope) -> NormalizedSignal:
    try:
        return select_normalizer(raw.payload)(raw)
    except NormalizationError:
        raise
    except ValueError as exc:  # pydantic bounds on the produced signal
        raise NormalizationError("normalized signal failed bounds validation") from exc
