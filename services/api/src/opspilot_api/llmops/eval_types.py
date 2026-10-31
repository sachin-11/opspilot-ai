"""Shared, provider-neutral value types for evaluation and tracing."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Split = Literal["development", "validation", "test"]
Source = Literal["synthetic", "human_curated", "production_redacted"]
Severity = Literal["low", "medium", "high", "critical"]
SUITES = (
    "triage",
    "tool_selection",
    "grounding",
    "report_quality",
    "retrieval_citations",
    "change_correlation",
    "remediation_safety",
    "isolation_injection_abstention",
)


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvalCaseSpec(BaseModel):
    """One golden case. ``input_data`` is what the system under test may see."""

    case_key: str = Field(min_length=3, max_length=80)
    suite: str
    split: Split
    input_data: dict[str, Any]
    expected_output: dict[str, Any] = Field(default_factory=dict)
    expected_tools: list[ToolCall] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    severity: Severity = "medium"
    source: Source = "synthetic"


class CaseOutput(BaseModel):
    """What the system under test produced. No hidden reasoning is representable here."""

    output: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    retrieved_ids: list[str] = Field(default_factory=list)
    suspect_change_ids: list[str] = Field(default_factory=list)
    abstained: bool = False
    final_status: str | None = None
    status_path: list[str] = Field(default_factory=list)
    error_class: str | None = None
    remediation_actions: list[str] = Field(default_factory=list)
    schema_valid: bool = True
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


class GraderResult(BaseModel):
    grader: str
    score: float = Field(ge=0, le=1)
    passed: bool
    safety: bool = False  # a failed safety grader is a hard failure no judge can override
    detail: str = ""
