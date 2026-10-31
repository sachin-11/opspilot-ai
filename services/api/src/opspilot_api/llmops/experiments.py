"""Experiment assignment, lifecycle, and analysis. Nothing here auto-promotes a variant."""

import hashlib
from dataclasses import dataclass, field
from typing import Literal, Protocol

from opspilot_api.llmops import stats

Variant = Literal["control", "candidate"]
Unit = Literal["workspace", "user", "investigation"]
# Side-effect authorization decisions are never experimented on.
EXCLUDED_NODES = frozenset(
    {"remediation_publish", "remediation_approval", "authorization", "policy_decision"}
)
EXCLUDED_WORKFLOWS = frozenset({"remediation_publication"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"running", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class ExperimentError(Exception):
    code = "experiment_error"


@dataclass(frozen=True)
class ExperimentSpec:
    id: str
    workflow: str
    unit: Unit
    allocation_percentage: float  # 0-100, share routed to the candidate
    primary_metric: str
    guardrail_metrics: tuple[str, ...]
    minimum_sample_size: int
    status: str = "draft"
    shadow_first: bool = True
    allowed_risks: tuple[str, ...] = ("low", "medium")
    salt: str = ""


def assignment_hash(spec: ExperimentSpec, unit_id: str) -> str:
    return hashlib.sha256(f"{spec.id}|{spec.salt}|{spec.unit}|{unit_id}".encode()).hexdigest()


def assign_variant(spec: ExperimentSpec, unit_id: str) -> Variant:
    """Stable: the same (experiment, unit) always lands in the same arm, forever."""
    bucket = int(assignment_hash(spec, unit_id)[:8], 16) % 10_000
    return "candidate" if bucket < spec.allocation_percentage * 100 else "control"


def is_eligible(spec: ExperimentSpec, *, workflow: str, node: str, risk: str) -> bool:
    return (
        spec.status == "running"
        and workflow == spec.workflow
        and workflow not in EXCLUDED_WORKFLOWS
        and node not in EXCLUDED_NODES
        and risk in spec.allowed_risks
    )


@dataclass(frozen=True)
class Assignment:
    experiment_id: str
    unit_id: str
    variant: Variant
    assignment_hash: str


class AssignmentStore(Protocol):
    def get(self, experiment_id: str, unit_id: str) -> Assignment | None: ...
    def put_if_absent(self, assignment: Assignment) -> Assignment:
        """Insert unless present; always return the persisted row (never reassign)."""
        ...


@dataclass
class InMemoryAssignmentStore:
    rows: dict[tuple[str, str], Assignment] = field(default_factory=dict)

    def get(self, experiment_id: str, unit_id: str) -> Assignment | None:
        return self.rows.get((experiment_id, unit_id))

    def put_if_absent(self, assignment: Assignment) -> Assignment:
        return self.rows.setdefault((assignment.experiment_id, assignment.unit_id), assignment)


def get_or_assign(
    spec: ExperimentSpec,
    unit_id: str,
    store: AssignmentStore,
    *,
    workflow: str,
    node: str,
    risk: str,
) -> Assignment | None:
    """Return the persisted assignment, creating it once. None when the call is ineligible."""
    existing = store.get(spec.id, unit_id)
    if existing is not None:
        return existing  # no reassignment on refresh/retry, even if allocation later changes
    if not is_eligible(spec, workflow=workflow, node=node, risk=risk):
        return None
    return store.put_if_absent(
        Assignment(spec.id, unit_id, assign_variant(spec, unit_id), assignment_hash(spec, unit_id))
    )


def validate_for_start(spec: ExperimentSpec) -> None:
    if not spec.primary_metric:
        raise ExperimentError("a primary metric must be defined before starting")
    if not spec.guardrail_metrics:
        raise ExperimentError("at least one guardrail metric must be defined before starting")
    if spec.minimum_sample_size < 30:
        raise ExperimentError("minimum_sample_size must be at least 30 per arm")
    if not 0 < spec.allocation_percentage <= 50:
        raise ExperimentError("allocation_percentage must be in (0, 50]")
    if spec.workflow in EXCLUDED_WORKFLOWS:
        raise ExperimentError("remediation publication cannot be experimented on")


def transition(current: str, target: str) -> str:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise ExperimentError(f"cannot move an experiment from {current} to {target}")
    return target


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    comparison: stats.ProportionComparison
    guardrail: bool
    significant: bool
    regressed: bool


@dataclass(frozen=True)
class Analysis:
    sample: stats.SampleSizeStatus
    metrics: list[MetricComparison]
    safety_stop: bool
    warnings: list[str]
    promotable_by_data: bool  # necessary, never sufficient: a human decides


def analyze(
    spec: ExperimentSpec,
    arms: dict[str, dict[Variant, tuple[int, int]]],
    *,
    safety_violations_candidate: int = 0,
    analyses_so_far: int = 1,
    alpha: float = 0.05,
) -> Analysis:
    """``arms[metric][variant] = (successes, n)`` where success is the *good* outcome."""
    primary = arms[spec.primary_metric]
    sample = stats.sample_size_status(
        primary["control"][1], primary["candidate"][1], spec.minimum_sample_size
    )
    warnings: list[str] = []
    if not sample.reached:
        warnings.append(
            f"minimum sample size {spec.minimum_sample_size} per arm not reached "
            f"({sample.control_n}/{sample.candidate_n}); results are not decision-grade"
        )
    peek = stats.peeking_warning(analyses_so_far)
    if peek:
        warnings.append(peek)
    guardrail_alpha = stats.bonferroni(alpha, len(spec.guardrail_metrics))
    out: list[MetricComparison] = []
    for metric in (spec.primary_metric, *spec.guardrail_metrics):
        guardrail = metric != spec.primary_metric
        c, k = arms[metric]["control"], arms[metric]["candidate"]
        level = 1 - (guardrail_alpha if guardrail else alpha)
        cmp_ = stats.compare_proportions(c[0], c[1], k[0], k[1], confidence=level)
        threshold = guardrail_alpha if guardrail else alpha
        significant = cmp_.p_value is not None and cmp_.p_value < threshold
        out.append(
            MetricComparison(
                metric, cmp_, guardrail, significant, significant and cmp_.difference < 0
            )
        )
    safety_stop = safety_violations_candidate > 0
    guardrail_regressed = any(m.regressed for m in out if m.guardrail)
    primary_cmp = out[0]
    promotable = (
        sample.reached
        and not safety_stop
        and not guardrail_regressed
        and primary_cmp.significant
        and primary_cmp.comparison.difference > 0
    )
    if safety_stop:
        warnings.append("safety regression in candidate arm: stop immediately")
    return Analysis(sample, out, safety_stop, warnings, promotable)
