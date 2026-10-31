"""Statistics for experiments. Standard library only; every assumption is explicit.

Assumptions (also shown in the dashboard):
- Units are independent and were assigned by a stable hash (no interference between units).
- Proportion comparisons use a two-sided two-proportion z-test with unpooled-variance Wald
  confidence intervals; valid when each arm has at least ``MIN_EXPECTED_SUCCESSES`` successes and
  failures. Below that, no p-value is produced.
- Guardrail comparisons are Bonferroni-adjusted for the number of guardrails.
- A fixed-horizon test: p-values are only valid at the pre-registered minimum sample size.
  Repeated unplanned looks inflate the false-positive rate; looks are counted and flagged.
"""

import math
from dataclasses import dataclass

MIN_EXPECTED_SUCCESSES = 5
Z_95 = 1.959963984540054


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def z_critical(confidence: float) -> float:
    """Inverse normal CDF by bisection (no scipy)."""
    if not 0.5 < confidence < 1:
        raise ValueError("confidence must be in (0.5, 1)")
    target = 0.5 + confidence / 2
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if normal_cdf(mid) < target else (lo, mid)
    return (lo + hi) / 2


@dataclass(frozen=True)
class Interval:
    low: float
    high: float


@dataclass(frozen=True)
class ProportionComparison:
    control_rate: float
    candidate_rate: float
    difference: float
    interval: Interval
    p_value: float | None
    valid: bool
    note: str


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> Interval:
    if n <= 0:
        return Interval(0.0, 1.0)
    z = z_critical(confidence)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(max(0.0, centre - half), min(1.0, centre + half))


def compare_proportions(
    control_successes: int,
    control_n: int,
    candidate_successes: int,
    candidate_n: int,
    *,
    confidence: float = 0.95,
) -> ProportionComparison:
    if control_n <= 0 or candidate_n <= 0:
        return ProportionComparison(0.0, 0.0, 0.0, Interval(-1.0, 1.0), None, False, "no data")
    p1, p2 = control_successes / control_n, candidate_successes / candidate_n
    diff = p2 - p1
    se = math.sqrt(p1 * (1 - p1) / control_n + p2 * (1 - p2) / candidate_n)
    z = z_critical(confidence)
    interval = Interval(diff - z * se, diff + z * se)
    enough = all(
        min(s, n - s) >= MIN_EXPECTED_SUCCESSES
        for s, n in ((control_successes, control_n), (candidate_successes, candidate_n))
    )
    if not enough:
        note = f"fewer than {MIN_EXPECTED_SUCCESSES} successes or failures in an arm; no p-value"
        return ProportionComparison(p1, p2, diff, interval, None, False, note)
    if se == 0:
        return ProportionComparison(p1, p2, diff, interval, 1.0 if diff == 0 else 0.0, True, "")
    p_value = 2 * (1 - normal_cdf(abs(diff / se)))
    return ProportionComparison(p1, p2, diff, interval, p_value, True, "")


def mean_interval(values: list[float], confidence: float = 0.95) -> Interval:
    n = len(values)
    if n < 2:
        return Interval(float("-inf"), float("inf"))
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z_critical(confidence) * math.sqrt(var / n)
    return Interval(mean - half, mean + half)


def bonferroni(alpha: float, comparisons: int) -> float:
    return alpha / max(1, comparisons)


@dataclass(frozen=True)
class SampleSizeStatus:
    control_n: int
    candidate_n: int
    minimum: int
    reached: bool


def sample_size_status(control_n: int, candidate_n: int, minimum: int) -> SampleSizeStatus:
    return SampleSizeStatus(control_n, candidate_n, minimum, min(control_n, candidate_n) >= minimum)


def peeking_warning(analyses_so_far: int, planned_analyses: int = 1) -> str | None:
    if analyses_so_far > planned_analyses:
        return (
            f"{analyses_so_far} analyses so far against {planned_analyses} planned: repeated "
            "unplanned significance checks inflate false positives; treat p-values as exploratory"
        )
    return None
