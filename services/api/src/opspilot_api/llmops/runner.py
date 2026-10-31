"""Evaluation runner: executes a system over a dataset, grades deterministically, aggregates.

Deterministic graders always run first; a safety failure marks the case failed regardless of any
later judge score. The runner is provider-agnostic: ``system`` is any callable from a case to a
``CaseOutput`` (the fake reference agent in CI, a real provider adapter for manual runs).
"""

import hashlib
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from opspilot_api.llmops.dataset import canonical_json, dataset_hash, select_cases
from opspilot_api.llmops.eval_types import CaseOutput, EvalCaseSpec
from opspilot_api.llmops.graders import case_passed, grade_case, safety_failed

System = Callable[[EvalCaseSpec], CaseOutput]

# Metrics for the fake tier are derived from payload size, not measured; reports say so.
SYNTHETIC = "synthetic"
MEASURED = "measured"


class CaseResult(BaseModel):
    case_key: str
    suite: str
    split: str
    tags: list[str]
    variant: str
    repeat_index: int
    output_hash: str
    passed: bool
    safety_failed: bool
    failure_reasons: list[str]
    grader_scores: dict[str, float]
    tool_calls: list[str]
    cited_evidence_ids: list[str]
    latency_ms: float
    tokens: int
    estimated_cost_usd: float | None


class EvalReport(BaseModel):
    variant: str
    dataset_hash: str
    run_type: str
    purpose: str
    repeat_count: int
    metrics_source: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    metrics: dict[str, float | None]
    violations: dict[str, int]
    by_suite: dict[str, float]
    by_tag: dict[str, float]
    flaky_cases: list[str]
    failed_case_keys: list[str]
    case_pass: dict[str, bool]
    results: list[CaseResult] = Field(default_factory=list)


def output_hash(output: CaseOutput) -> str:
    stable = output.model_dump(
        mode="json", exclude={"latency_ms", "input_tokens", "output_tokens", "estimated_cost_usd"}
    )
    return hashlib.sha256(canonical_json(stable).encode()).hexdigest()


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def run_evaluation(
    cases: list[EvalCaseSpec],
    system: System,
    *,
    variant: str,
    repeats: int = 1,
    run_type: str = "ci",
    purpose: str = "gate",
    metrics_source: str = SYNTHETIC,
) -> EvalReport:
    selected = select_cases(cases, purpose)
    results: list[CaseResult] = []
    hashes: dict[str, set[str]] = defaultdict(set)
    for case in selected:
        for repeat in range(repeats):
            output = system(case)
            graded = grade_case(case, output)
            ok, reasons = case_passed(graded)
            out_hash = output_hash(output)
            hashes[case.case_key].add(out_hash)
            results.append(
                CaseResult(
                    case_key=case.case_key,
                    suite=case.suite,
                    split=case.split,
                    tags=case.tags,
                    variant=variant,
                    repeat_index=repeat,
                    output_hash=out_hash,
                    passed=ok,
                    safety_failed=safety_failed(graded),
                    failure_reasons=reasons,
                    grader_scores={g.grader: g.score for g in graded},
                    tool_calls=[t.name for t in output.tool_calls],
                    cited_evidence_ids=output.cited_evidence_ids,
                    latency_ms=output.latency_ms,
                    tokens=output.input_tokens + output.output_tokens,
                    estimated_cost_usd=output.estimated_cost_usd,
                )
            )
    return _summarize(
        results,
        selected,
        variant,
        repeats,
        run_type,
        purpose,
        metrics_source,
        flaky=sorted(k for k, v in hashes.items() if len(v) > 1),
    )


def _grader_values(results: list[CaseResult], grader: str) -> list[float]:
    return [r.grader_scores[grader] for r in results if grader in r.grader_scores]


def _summarize(
    results: list[CaseResult],
    selected: list[EvalCaseSpec],
    variant: str,
    repeats: int,
    run_type: str,
    purpose: str,
    metrics_source: str,
    flaky: list[str],
) -> EvalReport:
    # A case passes only if every repeat passes.
    case_pass: dict[str, bool] = {}
    for r in results:
        case_pass[r.case_key] = case_pass.get(r.case_key, True) and r.passed
    first = [r for r in results if r.repeat_index == 0]

    def mean(grader: str) -> float | None:
        values = _grader_values(first, grader)
        return round(sum(values) / len(values), 6) if values else None

    def count_fail(grader: str) -> int:
        return sum(1 for r in results if r.grader_scores.get(grader, 1.0) < 1.0)

    cost_values = [r.estimated_cost_usd for r in first if r.estimated_cost_usd is not None]
    latencies = [r.latency_ms for r in first]
    metrics: dict[str, float | None] = {
        "pass_rate": _rate(list(case_pass.values())),
        "schema_validity_rate": mean("schema_validity"),
        "tool_selection_accuracy": mean("tool_selection"),
        "tool_argument_accuracy": mean("tool_arguments"),
        "citation_validity_rate": mean("evidence_id_validity"),
        "citation_precision": mean("citation_precision"),
        "citation_recall": mean("citation_recall"),
        "required_abstention_rate": mean("required_abstention"),
        "change_correlation_recall": mean("change_correlation_recall"),
        "retrieval_recall_at_k": mean("retrieval_metrics"),
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "avg_cost_usd": round(statistics.fmean(cost_values), 8) if cost_values else None,
        "total_tokens": float(sum(r.tokens for r in first)),
    }
    violations = {
        "tenant_leaks": count_fail("tenant_leak"),
        "secret_leaks": count_fail("secret_leak"),
        "injection_violations": count_fail("injection_compliance"),
        "prohibited_remediation_actions": count_fail("remediation_policy"),
        "invalid_citations": sum(
            1 for r in results if r.grader_scores.get("evidence_id_validity", 1.0) < 1.0
        ),
    }
    by_suite = _group_rate(case_pass, {c.case_key: [c.suite] for c in selected})
    by_tag = _group_rate(case_pass, {c.case_key: c.tags for c in selected})
    failed = sorted(k for k, ok in case_pass.items() if not ok)
    return EvalReport(
        variant=variant,
        dataset_hash=dataset_hash(selected),
        run_type=run_type,
        purpose=purpose,
        repeat_count=repeats,
        metrics_source=metrics_source,
        total_cases=len(case_pass),
        passed_cases=len(case_pass) - len(failed),
        failed_cases=len(failed),
        metrics=metrics,
        violations=violations,
        by_suite=by_suite,
        by_tag=by_tag,
        flaky_cases=flaky,
        failed_case_keys=failed,
        case_pass=case_pass,
        results=results,
    )


def _group_rate(case_pass: dict[str, bool], groups: dict[str, list[str]]) -> dict[str, float]:
    buckets: dict[str, list[bool]] = defaultdict(list)
    for key, ok in case_pass.items():
        for group in groups.get(key, []):
            buckets[group].append(ok)
    return {g: round(sum(v) / len(v), 6) for g, v in sorted(buckets.items())}


def to_markdown(report: EvalReport, gate_summary: dict[str, Any] | None = None) -> str:
    lines = [
        f"# Evaluation report: `{report.variant}`",
        "",
        f"- Run type: `{report.run_type}` · purpose: `{report.purpose}` · repeats: "
        f"{report.repeat_count}",
        f"- Dataset hash: `{report.dataset_hash[:16]}`",
        f"- Cases: {report.passed_cases}/{report.total_cases} passed",
        f"- Latency/token source: **{report.metrics_source}**"
        + (
            " (derived from payload size, not measured)"
            if report.metrics_source == SYNTHETIC
            else ""
        ),
        f"- Flaky cases: {len(report.flaky_cases)}",
        "",
        "## Safety violations",
        "",
        "| Invariant | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in report.violations.items()],
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[
            f"| {k} | {'n/a' if v is None else v} |"
            for k, v in report.metrics.items()
            if not (report.metrics_source == SYNTHETIC and k.startswith("latency"))
        ],
        "",
        "## Pass rate by suite",
        "",
        "| Suite | Pass rate |",
        "|---|---|",
        *[f"| {k} | {v:.3f} |" for k, v in report.by_suite.items()],
    ]
    if report.failed_case_keys:
        lines += ["", "## Failed cases", "", *[f"- `{k}`" for k in report.failed_case_keys]]
    if gate_summary:
        lines += ["", "## Gates", "", f"Result: **{'PASS' if gate_summary['passed'] else 'FAIL'}**"]
        for item in gate_summary["results"]:
            mark = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}[item["status"]]
            lines.append(f"- {mark} `{item['gate']}` ({item['kind']}): {item['detail']}")
    return "\n".join(lines) + "\n"
