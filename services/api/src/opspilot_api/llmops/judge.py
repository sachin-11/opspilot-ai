"""Provider-neutral LLM-as-judge abstraction.

Rules enforced here, not by convention:
- Deterministic graders run first; a safety failure means the judge is never consulted and can
  never rescue the case (``combine_verdict``).
- Judge output is a strict schema: integer scores and a short rationale. Any extra field (for
  example ``reasoning`` or ``chain_of_thought``) fails validation, so hidden reasoning cannot be
  stored or displayed.
- The judge prompt never contains variant names; pairwise order is randomized and unblinded after.
- A judge is only a release gate once calibrated against human labels (see ``human.py``).
- CI uses ``FakeJudge`` only.
"""

import hashlib
import json
import statistics
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

RUBRIC_VERSION = "judge-rubric-v1"
SCALE = (1, 5)

CRITERIA: dict[str, dict[str, str]] = {
    "factual_consistency": {
        "poor": "Makes claims the supplied evidence does not support or contradicts it.",
        "acceptable": "Mostly supported; minor unsupported detail.",
        "excellent": "Every claim is traceable to the supplied evidence.",
    },
    "root_cause_quality": {
        "poor": "Names no cause or states a cause as certain without support.",
        "acceptable": "Names a plausible cause and cites some evidence.",
        "excellent": "Names the best-supported cause, separates correlation from causation.",
    },
    "relevance": {
        "poor": "Mostly off-topic or padded with unrelated material.",
        "acceptable": "On topic with some filler.",
        "excellent": "Every sentence addresses the incident.",
    },
    "completeness": {
        "poor": "Omits key findings or required sections.",
        "acceptable": "Covers main findings; misses secondary ones.",
        "excellent": "Covers all material findings and unknowns.",
    },
    "actionability": {
        "poor": "No concrete next step, or an unsafe one.",
        "acceptable": "Generic but safe next steps.",
        "excellent": "Specific, safe, verifiable next steps that need human approval.",
    },
    "appropriate_uncertainty": {
        "poor": "Overconfident or refuses to state any finding.",
        "acceptable": "Some hedging, roughly calibrated.",
        "excellent": "Confidence matches the evidence; abstains when evidence is insufficient.",
    },
}


class JudgeOutputError(Exception):
    code = "invalid_judge_output"


class JudgeScores(BaseModel):
    model_config = ConfigDict(extra="forbid")  # hidden reasoning fields are rejected
    scores: dict[str, int]
    rationale: str = Field(max_length=300)
    preference: Literal["A", "B", "tie"] | None = None

    @field_validator("scores")
    @classmethod
    def _valid_scores(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - set(CRITERIA)
        if unknown:
            raise ValueError(f"unknown criteria: {sorted(unknown)}")
        for criterion, score in value.items():
            if not SCALE[0] <= score <= SCALE[1]:
                raise ValueError(f"{criterion} score {score} outside {SCALE}")
        return value


class JudgeProvider(Protocol):
    name: str
    model: str
    version: str

    def complete(self, prompt: str) -> str:
        """Return the judge's raw JSON response."""
        ...


def _rubric_text() -> str:
    lines = [f"Rubric {RUBRIC_VERSION}. Score each criterion {SCALE[0]}-{SCALE[1]}."]
    for name, anchors in CRITERIA.items():
        lines.append(
            f"- {name}: poor(1)={anchors['poor']} acceptable(3)={anchors['acceptable']} "
            f"excellent(5)={anchors['excellent']}"
        )
    lines.append('Reply with JSON only: {"scores": {...}, "rationale": "<=300 chars"}.')
    lines.append("Do not include reasoning steps; give scores and a brief rationale only.")
    return "\n".join(lines)


def build_prompt(
    reference: str,
    answers: dict[str, str],
    *,
    forbidden_names: tuple[str, ...] = (),
) -> str:
    """``answers`` is keyed by neutral labels (A/B). Variant identity must not leak in."""
    body = "\n\n".join(f"Answer {label}:\n{text}" for label, text in sorted(answers.items()))
    prompt = f"{_rubric_text()}\n\nReference evidence:\n{reference}\n\n{body}"
    lowered = prompt.lower()
    for name in forbidden_names:
        if name and name.lower() in lowered:
            raise JudgeOutputError(f"variant name {name!r} must not appear in a judge prompt")
    return prompt


def validate_output(raw: str) -> JudgeScores:
    try:
        return JudgeScores.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise JudgeOutputError(f"judge output rejected: {type(exc).__name__}") from exc


@dataclass(frozen=True)
class PairwiseItem:
    case_key: str
    answer_a: str
    answer_b: str
    a_is_x: bool  # True when X (e.g. control) was shown as A


def randomize_pair(case_key: str, answer_x: str, answer_y: str, seed: str) -> PairwiseItem:
    """Seeded, per-case coin flip. Reproducible, and independent of which variant is which."""
    flip = hashlib.sha256(f"{seed}|{case_key}".encode()).digest()[0] % 2 == 0
    return PairwiseItem(
        case_key, answer_x if flip else answer_y, answer_y if flip else answer_x, flip
    )


def unblind(preference: Literal["A", "B", "tie"], item: PairwiseItem) -> Literal["x", "y", "tie"]:
    if preference == "tie":
        return "tie"
    chose_a = preference == "A"
    return "x" if chose_a == item.a_is_x else "y"


@dataclass(frozen=True)
class JudgedResult:
    scores: dict[str, float]  # median per criterion over repeats
    rationale: str
    repeats: int
    max_spread: int  # largest (max-min) across repeats for any criterion
    provider: str
    model: str
    rubric_version: str


def run_judge(
    provider: JudgeProvider, reference: str, answer: str, *, repeats: int = 1
) -> JudgedResult:
    prompt = build_prompt(reference, {"A": answer})
    runs = [validate_output(provider.complete(prompt)) for _ in range(max(1, repeats))]
    criteria = sorted(set().union(*(r.scores for r in runs)))
    medians = {
        c: float(statistics.median(r.scores[c] for r in runs if c in r.scores)) for c in criteria
    }
    spread = max(
        (
            max(r.scores[c] for r in runs if c in r.scores)
            - min(r.scores[c] for r in runs if c in r.scores)
            for c in criteria
        ),
        default=0,
    )
    return JudgedResult(
        medians,
        runs[0].rationale,
        len(runs),
        spread,
        provider.name,
        f"{provider.model}@{provider.version}",
        RUBRIC_VERSION,
    )


def combine_verdict(
    *, deterministic_passed: bool, safety_failed: bool, judge_passed: bool | None, calibrated: bool
) -> bool:
    """Deterministic safety failures are final. An uncalibrated judge never changes a verdict."""
    if safety_failed or not deterministic_passed:
        return False
    if judge_passed is None or not calibrated:
        return True
    return judge_passed


def length_bias_rate(choices: list[tuple[int, int, str]]) -> float | None:
    """Fraction of decisive picks that chose the longer answer on pairs humans judged equal.

    ``choices`` holds (len_a, len_b, picked) with picked in {"A", "B", "tie"}.
    """
    decisive = [(la, lb, p) for la, lb, p in choices if p != "tie" and la != lb]
    if not decisive:
        return None
    longer = sum(1 for la, lb, p in decisive if (p == "A") == (la > lb))
    return longer / len(decisive)


def is_verbosity_biased(rate: float | None, n: int, *, threshold: float = 0.65) -> bool:
    return rate is not None and n >= 10 and rate > threshold


class FakeJudge:
    """Deterministic, length-insensitive judge for CI: scores by evidence-token coverage."""

    name = "fake-judge"
    model = "deterministic"
    version = "1"

    def complete(self, prompt: str) -> str:
        reference = prompt.split("Reference evidence:\n", 1)[1].split("\n\nAnswer ", 1)[0]
        answers = prompt.split("\n\nAnswer ")[1:]
        ref_tokens = set(reference.lower().split())
        scored: dict[str, int] = {}
        for entry in answers:
            _, text = entry.split(":\n", 1)
            tokens = set(text.lower().split())
            coverage = len(tokens & ref_tokens) / max(1, len(ref_tokens))
            scored[entry[0]] = 1 + min(4, round(coverage * 8))
        first = next(iter(scored.values()), 1)
        payload: dict[str, object] = {
            "scores": {c: first for c in CRITERIA},
            "rationale": "Scored by evidence-token coverage.",
        }
        if len(scored) == 2:
            a, b = scored.get("A", 1), scored.get("B", 1)
            payload["preference"] = "tie" if a == b else ("A" if a > b else "B")
        return json.dumps(payload)
