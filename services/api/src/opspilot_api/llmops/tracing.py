"""Provider-neutral tracing.

Adapters: in-memory (tests), structured log (internal default), and optional LangSmith. A trace
is sanitized before any adapter sees it, so no adapter can leak what the sanitizer removed.
Secondary exporters can never break the workflow: ``CompositeTracer`` isolates their failures.

Never captured: API keys, tokens, authorization headers, unredacted documents/logs, full source
files, or hidden chain-of-thought. Only hashes and bounded, redacted previews are kept.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from opspilot_api.investigations.safety import redact_value

logger = logging.getLogger("opspilot.llmops.trace")
_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "secret",
        "password",
        "cookie",
        "reasoning",
        "chain_of_thought",
        "thinking",
        "scratchpad",
        "private_key",
        "raw_prompt",
        "raw_response",
        "document_text",
        "source_code",
        "file_content",
    }
)
MAX_PREVIEW = 200


@dataclass
class TraceRecord:
    trace_id: str
    workspace_id: str
    workflow: str
    node: str
    final_status: str
    incident_id: str | None = None
    investigation_id: str | None = None
    remediation_id: str | None = None
    prompt_version_id: str | None = None
    prompt_hash: str | None = None
    routing_policy_version: str | None = None
    provider: str | None = None
    model: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_outcome: str | None = None
    structured_output_valid: bool | None = None
    events: list[dict[str, Any]] = field(default_factory=list)  # retry / fallback / breaker
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    error_category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, nested in list(value.items())[:50]:
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _FORBIDDEN_KEYS):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _scrub(nested, depth + 1)
        return out
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return str(redact_value(value))[:MAX_PREVIEW]
    return value


def sanitize(record: TraceRecord) -> TraceRecord:
    """Return a copy that is safe to hand to any adapter."""
    data = {k: v for k, v in record.__dict__.items()}
    for key in ("tool_calls", "events", "metadata"):
        data[key] = _scrub(data[key])
    return TraceRecord(**data)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TraceAdapter(Protocol):
    name: str

    def emit(self, record: TraceRecord) -> None: ...


class InMemoryTraceAdapter:
    name = "memory"

    def __init__(self) -> None:
        self.records: list[TraceRecord] = []

    def emit(self, record: TraceRecord) -> None:
        self.records.append(record)


class StructuredLogTraceAdapter:
    """Internal default: one JSON log line per trace."""

    name = "log"

    def emit(self, record: TraceRecord) -> None:
        logger.info("llm_trace %s", json.dumps(record.__dict__, default=str, sort_keys=True))


class LangSmithTraceAdapter:
    """Optional export. Only constructed when explicitly enabled with a key; always best-effort."""

    name = "langsmith"
    _URL = "https://api.smith.langchain.com/runs"

    def __init__(self, api_key: str, project: str = "opspilot", timeout: float = 3.0) -> None:
        self._headers = {"x-api-key": api_key}
        self._project, self._timeout = project, timeout

    def emit(self, record: TraceRecord) -> None:
        body = {
            "id": record.trace_id,
            "name": f"{record.workflow}.{record.node}",
            "run_type": "llm",
            "session_name": self._project,
            "inputs": {"prompt_hash": record.prompt_hash},
            "outputs": {"status": record.final_status},
            "extra": {"metadata": record.__dict__},
        }
        httpx.post(self._URL, json=body, headers=self._headers, timeout=self._timeout)


class CompositeTracer:
    """Primary adapters must succeed; secondary exporters are isolated from the workflow."""

    def __init__(
        self, primary: list[TraceAdapter], secondary: list[TraceAdapter] | None = None
    ) -> None:
        self.primary, self.secondary = primary, secondary or []
        self.export_failures = 0

    def emit(self, record: TraceRecord) -> None:
        safe = sanitize(record)
        for adapter in self.primary:
            adapter.emit(safe)
        for adapter in self.secondary:
            try:
                adapter.emit(safe)
            except Exception as exc:
                self.export_failures += 1
                logger.warning(
                    "trace export failed adapter=%s error=%s", adapter.name, type(exc).__name__
                )


def build_tracer(*, langsmith_enabled: bool, langsmith_key: str | None) -> CompositeTracer:
    """LangSmith is optional: disabled, or enabled without a key, simply means no export."""
    secondary: list[TraceAdapter] = []
    if langsmith_enabled and langsmith_key:
        secondary.append(LangSmithTraceAdapter(langsmith_key))
    return CompositeTracer([StructuredLogTraceAdapter()], secondary)
