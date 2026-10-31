"""Human-label agreement and LLM-judge calibration. Judges gate nothing until calibrated."""

from dataclasses import dataclass


def percent_agreement(a: list[bool], b: list[bool]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("label lists must be non-empty and the same length")
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def cohens_kappa(a: list[bool], b: list[bool]) -> float:
    """Chance-corrected agreement for two raters on binary labels."""
    observed = percent_agreement(a, b)
    n = len(a)
    pa, pb = sum(a) / n, sum(b) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def inter_rater_kappa(labels_by_rater: dict[str, dict[str, bool]]) -> float | None:
    """Mean pairwise kappa over the cases both raters of a pair labelled; None if no overlap."""
    raters = sorted(labels_by_rater)
    kappas: list[float] = []
    for i, r1 in enumerate(raters):
        for r2 in raters[i + 1 :]:
            shared = sorted(set(labels_by_rater[r1]) & set(labels_by_rater[r2]))
            if len(shared) >= 2:
                kappas.append(
                    cohens_kappa(
                        [labels_by_rater[r1][k] for k in shared],
                        [labels_by_rater[r2][k] for k in shared],
                    )
                )
    return sum(kappas) / len(kappas) if kappas else None


@dataclass(frozen=True)
class Calibration:
    labelled_cases: int
    agreement: float | None
    kappa: float | None
    calibrated: bool
    reason: str


def calibrate_judge(
    human: dict[str, bool],
    judge: dict[str, bool],
    *,
    min_labels: int = 30,
    min_agreement: float = 0.8,
    min_kappa: float = 0.6,
) -> Calibration:
    """A judge is calibrated only with enough labels and both agreement and kappa above floors."""
    shared = sorted(set(human) & set(judge))
    if len(shared) < min_labels:
        return Calibration(
            len(shared), None, None, False, f"needs {min_labels} human-labelled cases"
        )
    h = [human[k] for k in shared]
    j = [judge[k] for k in shared]
    agreement, kappa = percent_agreement(h, j), cohens_kappa(h, j)
    ok = agreement >= min_agreement and kappa >= min_kappa
    reason = "calibrated" if ok else f"agreement {agreement:.2f} / kappa {kappa:.2f} below floor"
    return Calibration(len(shared), agreement, kappa, ok, reason)
