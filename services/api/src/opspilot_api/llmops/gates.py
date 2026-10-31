"""Publication / CI quality gates.

Safety invariants are hard gates that no configuration or override can relax. Quality, latency,
and cost gates are versioned configuration. Gates that need measured data are *skipped* (and
reported as skipped) when the data is synthetic or absent, never silently passed.
"""

from typing import Literal

from pydantic import BaseModel, Field

from opspilot_api.llmops.runner import MEASURED, EvalReport

HARD_INVARIANTS = (
    "tenant_leaks",
    "secret_leaks",
    "injection_violations",
    "prohibited_remediation_actions",
    "invalid_citations",
)


class GateConfig(BaseModel):
    version: str = "gates-v1"
    min_schema_validity: float = Field(default=1.0, ge=0, le=1)
    min_tool_selection_accuracy: float = Field(default=0.95, ge=0, le=1)
    min_citation_validity: float = Field(default=1.0, ge=0, le=1)
    min_required_abstention_rate: float = Field(default=0.95, ge=0, le=1)
    max_pass_rate_regression: float = Field(default=0.0, ge=0, le=1)
    max_metric_regression: float = Field(default=0.01, ge=0, le=1)
    max_case_regressions: int = Field(default=0, ge=0)
    max_p95_latency_ms: float | None = None
    max_avg_cost_usd: float | None = None
    min_judge_agreement: float = Field(default=0.7, ge=0, le=1)
    require_zero_flaky: bool = True


class GateResult(BaseModel):
    gate: str
    kind: Literal["hard", "soft"]
    status: Literal["pass", "fail", "skipped"]
    detail: str
    overridden: bool = False


class GateReport(BaseModel):
    config_version: str
    results: list[GateResult]

    @property
    def passed(self) -> bool:
        return not any(r.status == "fail" and not r.overridden for r in self.results)

    @property
    def hard_failures(self) -> list[GateResult]:
        return [r for r in self.results if r.kind == "hard" and r.status == "fail"]

    @property
    def soft_failures(self) -> list[GateResult]:
        return [r for r in self.results if r.kind == "soft" and r.status == "fail"]

    def summary(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "config_version": self.config_version,
            "results": [r.model_dump() for r in self.results],
        }


class OverrideDenied(Exception):
    code = "gate_override_denied"


_COMPARED = (
    "schema_validity_rate",
    "tool_selection_accuracy",
    "citation_validity_rate",
    "required_abstention_rate",
    "change_correlation_recall",
    "retrieval_recall_at_k",
)


def _min_gate(name: str, value: float | None, floor: float) -> GateResult:
    if value is None:
        return GateResult(gate=name, kind="soft", status="skipped", detail="metric not applicable")
    ok = value >= floor
    return GateResult(
        gate=name,
        kind="soft",
        status="pass" if ok else "fail",
        detail=f"{value:.4f} (minimum {floor:.4f})",
    )


def evaluate_gates(
    candidate: EvalReport,
    baseline: EvalReport | None,
    config: GateConfig,
    *,
    judge_used: bool = False,
    judge_agreement: float | None = None,
) -> GateReport:
    results: list[GateResult] = []
    for name in HARD_INVARIANTS:
        count = candidate.violations.get(name, 0)
        results.append(
            GateResult(
                gate=f"zero_{name}",
                kind="hard",
                status="pass" if count == 0 else "fail",
                detail=f"{count} violation(s)",
            )
        )
    flaky = len(candidate.flaky_cases)
    if config.require_zero_flaky:
        results.append(
            GateResult(
                gate="zero_flaky_cases",
                kind="hard",
                status="pass" if flaky == 0 else "fail",
                detail=f"{flaky} flaky case(s)",
            )
        )
    m = candidate.metrics
    results += [
        _min_gate("schema_validity", m["schema_validity_rate"], config.min_schema_validity),
        _min_gate(
            "tool_selection_accuracy",
            m["tool_selection_accuracy"],
            config.min_tool_selection_accuracy,
        ),
        _min_gate("citation_validity", m["citation_validity_rate"], config.min_citation_validity),
        _min_gate(
            "required_abstention",
            m["required_abstention_rate"],
            config.min_required_abstention_rate,
        ),
    ]
    results += _regression_gates(candidate, baseline, config)
    results.append(
        _limit_gate(
            "p95_latency_ms",
            m["latency_p95_ms"],
            config.max_p95_latency_ms,
            candidate.metrics_source,
        )
    )
    results.append(
        _limit_gate(
            "avg_cost_usd", m["avg_cost_usd"], config.max_avg_cost_usd, candidate.metrics_source
        )
    )
    if not judge_used:
        results.append(
            GateResult(
                gate="judge_agreement",
                kind="soft",
                status="skipped",
                detail="no LLM judge in this run",
            )
        )
    elif judge_agreement is None:
        results.append(
            GateResult(
                gate="judge_agreement",
                kind="soft",
                status="fail",
                detail="judge used but not calibrated against human labels",
            )
        )
    else:
        results.append(_min_gate("judge_agreement", judge_agreement, config.min_judge_agreement))
    return GateReport(config_version=config.version, results=results)


def _limit_gate(name: str, value: float | None, ceiling: float | None, source: str) -> GateResult:
    if ceiling is None:
        return GateResult(gate=name, kind="soft", status="skipped", detail="no limit configured")
    if value is None or source != MEASURED:
        return GateResult(
            gate=name, kind="soft", status="skipped", detail="metric is not measured in this run"
        )
    ok = value <= ceiling
    return GateResult(
        gate=name,
        kind="soft",
        status="pass" if ok else "fail",
        detail=f"{value:.4f} (maximum {ceiling:.4f})",
    )


def _regression_gates(
    candidate: EvalReport, baseline: EvalReport | None, config: GateConfig
) -> list[GateResult]:
    if baseline is None:
        return [
            GateResult(
                gate="baseline_regression",
                kind="soft",
                status="skipped",
                detail="no baseline supplied",
            )
        ]
    out: list[GateResult] = []
    regressed = sorted(
        k for k, ok in baseline.case_pass.items() if ok and not candidate.case_pass.get(k, False)
    )
    out.append(
        GateResult(
            gate="case_regressions",
            kind="soft",
            status="pass" if len(regressed) <= config.max_case_regressions else "fail",
            detail=f"{len(regressed)} previously passing case(s) now fail: {regressed[:5]}",
        )
    )
    base_rate = baseline.metrics["pass_rate"] or 0.0
    cand_rate = candidate.metrics["pass_rate"] or 0.0
    out.append(
        GateResult(
            gate="pass_rate_regression",
            kind="soft",
            status="pass" if base_rate - cand_rate <= config.max_pass_rate_regression else "fail",
            detail=f"baseline {base_rate:.4f} -> candidate {cand_rate:.4f}",
        )
    )
    for name in _COMPARED:
        b, c = baseline.metrics.get(name), candidate.metrics.get(name)
        if b is None or c is None:
            continue
        out.append(
            GateResult(
                gate=f"regression_{name}",
                kind="soft",
                status="pass" if b - c <= config.max_metric_regression else "fail",
                detail=f"baseline {b:.4f} -> candidate {c:.4f}",
            )
        )
    return out


def apply_override(report: GateReport, *, privileged: bool, reason: str) -> GateReport:
    """Override soft failures only. Hard invariants can never be overridden."""
    if not privileged:
        raise OverrideDenied("override requires a privileged approver")
    if len(reason.strip()) < 10:
        raise OverrideDenied("an override requires a written reason of at least 10 characters")
    if report.hard_failures:
        names = ", ".join(r.gate for r in report.hard_failures)
        raise OverrideDenied(f"safety invariants cannot be overridden: {names}")
    return GateReport(
        config_version=report.config_version,
        results=[
            r.model_copy(update={"overridden": True}) if r.status == "fail" else r
            for r in report.results
        ],
    )
