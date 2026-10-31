"""Local evaluation commands. No network, no credentials, no hosted service.

uv run python -m opspilot_api.llmops.cli ci --baseline tests/fixtures/llmops_baseline.json
uv run python -m opspilot_api.llmops.cli compare --control baseline --candidate candidate
uv run python -m opspilot_api.llmops.cli baseline --write tests/fixtures/llmops_baseline.json
uv run python -m opspilot_api.llmops.cli export --out dataset.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

from opspilot_api.config import get_settings
from opspilot_api.llmops.dataset import export_dataset, find_duplicates, select_cases
from opspilot_api.llmops.gates import GateConfig, evaluate_gates
from opspilot_api.llmops.golden_ops import golden_cases
from opspilot_api.llmops.real_eval import (
    AnthropicEvalSystem,
    RealEvalBudgetExceeded,
    RealEvalError,
    guard_environment,
)
from opspilot_api.llmops.reference_agent import VARIANTS, run_case
from opspilot_api.llmops.runner import MEASURED, EvalReport, run_evaluation, to_markdown
from opspilot_api.llmops.usage import PricingRate

DEFAULT_REPEATS = 3
DATASET_NAME = "opspilot-golden"


def _run(variant: str, repeats: int, purpose: str = "gate") -> EvalReport:
    chosen = VARIANTS[variant]
    return run_evaluation(
        golden_cases(),
        lambda case: run_case(case, chosen),
        variant=variant,
        repeats=repeats,
        run_type="ci",
        purpose=purpose,
    )


def load_baseline(path: Path) -> EvalReport:
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def _write_reports(out: Path, name: str, report: EvalReport, gate: dict[str, object]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    slim = report.model_dump(mode="json")
    (out / f"{name}.json").write_text(
        json.dumps({"report": slim, "gates": gate}, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out / f"{name}.md").write_text(to_markdown(report, gate), encoding="utf-8")


def cmd_ci(args: argparse.Namespace) -> int:
    duplicates = find_duplicates(golden_cases())
    if duplicates:
        print(f"duplicate golden cases detected: {duplicates}", file=sys.stderr)
        return 1
    report = _run(args.variant, args.repeats)
    baseline = load_baseline(Path(args.baseline)) if args.baseline else None
    gates = evaluate_gates(report, baseline, GateConfig())
    _write_reports(Path(args.out), f"ci-{args.variant}", report, gates.summary())
    print(
        f"{report.variant}: {report.passed_cases}/{report.total_cases} cases passed, "
        f"{len(report.flaky_cases)} flaky, gates {'PASS' if gates.passed else 'FAIL'}"
    )
    for item in gates.results:
        if item.status == "fail":
            print(f"  FAIL [{item.kind}] {item.gate}: {item.detail}", file=sys.stderr)
    return 0 if gates.passed else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    report = _run("baseline", args.repeats)
    report.results = []
    path = Path(args.write)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote baseline for {report.total_cases} cases to {path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    control = _run(args.control, args.repeats)
    candidate = _run(args.candidate, args.repeats)
    gates = evaluate_gates(candidate, control, GateConfig())
    out = Path(args.out)
    _write_reports(out, f"compare-{args.candidate}-vs-{args.control}", candidate, gates.summary())
    delta = {
        k: (control.metrics.get(k), candidate.metrics.get(k))
        for k in ("pass_rate", "retrieval_recall_at_k", "citation_precision")
    }
    print(json.dumps({"control_vs_candidate": delta, "gates_passed": gates.passed}, indent=2))
    return 0 if gates.passed else 1


def cmd_real_eval(args: argparse.Namespace) -> int:
    """Manual, credentialed, cost-capped run. Produces a report; never publishes anything."""
    import anthropic

    settings = get_settings()
    key = settings.AI_PRIMARY_API_KEY
    try:
        guard_environment(
            os.environ,
            enabled=settings.LLMOPS_REAL_EVAL_ENABLED,
            has_key=key is not None and settings.AI_PRIMARY_PROVIDER == "anthropic",
        )
    except RealEvalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if key is None:  # unreachable after guard_environment; keeps the type checker honest
        return 2
    cap = min(args.max_cost_usd, settings.LLMOPS_REAL_EVAL_MAX_COST_USD)
    rate = PricingRate(
        args.rate_version,
        "anthropic",
        settings.AI_PRIMARY_MODEL,
        args.input_per_million,
        args.output_per_million,
    )
    system = AnthropicEvalSystem(
        anthropic.Anthropic(api_key=key.get_secret_value()),
        model=settings.AI_PRIMARY_MODEL,
        rate=rate,
        max_cost_usd=cap,
    )
    cases = select_cases(golden_cases(), "tuning")[: args.limit]  # never the held-out test split
    stopped = False
    try:
        report = run_evaluation(
            cases,
            system,
            variant="real-model",
            repeats=1,
            run_type="offline",
            purpose="tuning",
            metrics_source=MEASURED,
        )
    except RealEvalBudgetExceeded:
        stopped = True
        report = run_evaluation([], system, variant="real-model", metrics_source=MEASURED)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": settings.AI_PRIMARY_MODEL,
        "pricing_rate_version": rate.version,
        "hard_cost_cap_usd": cap,
        "spent_usd": round(system.spent_usd, 8),
        "calls": system.calls,
        "stopped_at_cost_cap": stopped,
        "refused_unredactable_cases": system.refused,
        "published_anything": False,
    }
    (out / "real-eval.json").write_text(
        json.dumps({"meta": meta, "report": report.model_dump(mode="json")}, indent=2),
        encoding="utf-8",
    )
    fence = "`" * 3
    lines = ["", "## Run metadata", "", fence + "json", json.dumps(meta, indent=2), fence, ""]
    (out / "real-eval.md").write_text(to_markdown(report) + chr(10).join(lines), encoding="utf-8")
    print(json.dumps(meta))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    payload = export_dataset(golden_cases(), name=DATASET_NAME, version=1)
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"exported {payload['case_count']} redacted cases (hash {payload['content_hash'][:12]})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot-llmops")
    sub = parser.add_subparsers(dest="command", required=True)
    ci = sub.add_parser("ci", help="required CI tier (fake provider, deterministic)")
    ci.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))
    ci.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    ci.add_argument("--baseline")
    ci.add_argument("--out", default="evaluation-reports")
    ci.set_defaults(func=cmd_ci)
    base = sub.add_parser("baseline", help="regenerate the committed golden baseline")
    base.add_argument("--write", required=True)
    base.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    base.set_defaults(func=cmd_baseline)
    cmp_ = sub.add_parser("compare", help="compare a candidate variant against a control")
    cmp_.add_argument("--control", default="baseline", choices=sorted(VARIANTS))
    cmp_.add_argument("--candidate", default="candidate", choices=sorted(VARIANTS))
    cmp_.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    cmp_.add_argument("--out", default="evaluation-reports")
    cmp_.set_defaults(func=cmd_compare)
    exp = sub.add_parser("export", help="export the redacted golden dataset")
    exp.add_argument("--out", required=True)
    exp.set_defaults(func=cmd_export)
    real = sub.add_parser("real-eval", help="manual real-model tier (needs credentials and a cap)")
    real.add_argument("--max-cost-usd", type=float, required=True)
    real.add_argument("--limit", type=int, default=20)
    real.add_argument("--rate-version", required=True)
    real.add_argument("--input-per-million", type=float, required=True)
    real.add_argument("--output-per-million", type=float, required=True)
    real.add_argument("--out", default="evaluation-reports")
    real.set_defaults(func=cmd_real_eval)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
