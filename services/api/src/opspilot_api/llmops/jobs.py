"""RQ job: execute one evaluation run. Idempotent and safe to retry; never calls a paid API.

The fake/deterministic tier runs the rule-based reference agent. A real-model run is a separate,
manually triggered path (``real_eval``) that requires explicit credentials and a hard cost cap.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from opspilot_api.config import get_settings
from opspilot_api.llmops.gates import GateConfig, GateReport, evaluate_gates
from opspilot_api.llmops.models import EvalCaseResult, EvalCaseRow, EvalRun
from opspilot_api.llmops.reference_agent import VARIANTS, run_case
from opspilot_api.llmops.runner import EvalReport, run_evaluation, to_markdown
from opspilot_api.llmops.seed import row_to_spec

logger = logging.getLogger(__name__)


def _baseline_report(
    session: Session, run: EvalRun, control_variant: str | None
) -> EvalReport | None:
    """A comparison baseline is computed fresh with the same data and settings (never stale)."""
    if not control_variant:
        return None
    cases = [
        row_to_spec(r)
        for r in session.scalars(
            select(EvalCaseRow).where(EvalCaseRow.dataset_id == run.dataset_id)
        )
    ]
    chosen = VARIANTS[control_variant]
    return run_evaluation(
        cases,
        lambda c: run_case(c, chosen),
        variant=control_variant,
        repeats=run.repeat_count,
        run_type=run.run_type,
        purpose=run.purpose,
    )


def execute_eval_run(session: Session, run_id: UUID) -> None:
    run = session.get(EvalRun, run_id)
    if run is None or run.status in ("completed", "cancelled"):
        return
    run.status, run.started_at, run.error = "running", datetime.now(UTC), None
    session.commit()
    try:
        cases = [
            row_to_spec(r)
            for r in session.scalars(
                select(EvalCaseRow).where(EvalCaseRow.dataset_id == run.dataset_id)
            )
        ]
        chosen = VARIANTS[run.variant]
        report = run_evaluation(
            cases,
            lambda c: run_case(c, chosen),
            variant=run.variant,
            repeats=run.repeat_count,
            run_type=run.run_type,
            purpose=run.purpose,
        )
        session.refresh(run)
        if run.status == "cancelled":  # cancelled while running: keep it cancelled
            return
        control = (run.seed_configuration or {}).get("control_variant")
        baseline = _baseline_report(session, run, control)
        gates: GateReport = evaluate_gates(report, baseline, GateConfig())
        session.execute(delete(EvalCaseResult).where(EvalCaseResult.eval_run_id == run.id))
        for r in report.results:
            session.add(
                EvalCaseResult(
                    eval_run_id=run.id,
                    case_key=r.case_key,
                    suite=r.suite,
                    variant=r.variant,
                    repeat_index=r.repeat_index,
                    output_hash=r.output_hash,
                    tool_calls=r.tool_calls,
                    evidence_citations=r.cited_evidence_ids,
                    deterministic_scores=r.grader_scores,
                    llm_judge_scores=None,
                    human_labels=None,
                    latency_ms=r.latency_ms,
                    tokens=r.tokens,
                    estimated_cost_usd=r.estimated_cost_usd,
                    passed=r.passed,
                    safety_failed=r.safety_failed,
                    failure_reasons=r.failure_reasons,
                )
            )
        run.total_cases, run.passed_cases = report.total_cases, report.passed_cases
        run.failed_cases, run.total_tokens = (
            report.failed_cases,
            int(report.metrics["total_tokens"] or 0),
        )
        run.estimated_cost_usd = report.metrics["avg_cost_usd"]
        run.metrics_source = report.metrics_source
        summary: dict[str, Any] = {
            "metrics": report.metrics,
            "violations": report.violations,
            "by_suite": report.by_suite,
            "by_tag": report.by_tag,
            "flaky_cases": report.flaky_cases,
            "failed_case_keys": report.failed_case_keys,
            "dataset_hash": report.dataset_hash,
        }
        if baseline is not None:
            summary["comparison"] = {
                "control_variant": control,
                "control_metrics": baseline.metrics,
                "control_violations": baseline.violations,
                "control_pass_rate_by_suite": baseline.by_suite,
            }
        run.summary = summary
        run.gate_report = gates.summary()
        slim = report.model_copy(update={"results": []})
        run.report_artifact_key = f"db:eval_runs/{run.id}"
        summary["markdown_report"] = to_markdown(slim, gates.summary())
        run.status, run.completed_at = "completed", datetime.now(UTC)
        session.commit()
    except Exception as exc:
        session.rollback()
        run = session.get(EvalRun, run_id)
        if run is not None:
            run.status, run.completed_at = "failed", datetime.now(UTC)
            run.error = f"{type(exc).__name__}"[:200]  # class only: never the message or secrets
            session.commit()
        logger.error(
            "eval run failed", extra={"error_class": type(exc).__name__, "eval_run_id": str(run_id)}
        )
        raise


def run_eval_job(eval_run_id: str) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            execute_eval_run(session, UUID(eval_run_id))
    finally:
        engine.dispose()
