"""Online (production) evaluation sampling, review queue rules, and dataset promotion.

Disabled by default and opt-in per workspace. Selection is a deterministic hash (the same unit is
always in or out), the rate is bounded, high-risk and sensitive runs are excluded unless the
workspace explicitly allows them, everything is redacted before it can reach an evaluator, and
sampling never runs in the user-facing request path (callers enqueue; they never wait).
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from opspilot_api.investigations.safety import redact_value
from opspilot_api.llmops.dataset import (
    DatasetError,
    ProductionCaseRejected,
    find_duplicates,
    has_secret_material,
    validate_production_case,
)
from opspilot_api.llmops.eval_types import EvalCaseSpec

MAX_SAMPLE_RATE = 0.10
LOW_CONFIDENCE = 0.6


@dataclass(frozen=True)
class OnlineEvalConfig:
    enabled: bool = False  # global switch, off by default
    sample_rate: float = 0.0  # 0..MAX_SAMPLE_RATE
    allow_high_risk: bool = False
    salt: str = "online-eval-v1"


@dataclass(frozen=True)
class WorkspaceEvalPolicy:
    opted_in: bool = False
    allow_high_risk: bool = False


def effective_rate(config: OnlineEvalConfig) -> float:
    return min(max(config.sample_rate, 0.0), MAX_SAMPLE_RATE)


def should_sample(
    config: OnlineEvalConfig,
    policy: WorkspaceEvalPolicy,
    *,
    unit_id: str,
    risk: str,
    sensitive: bool,
) -> bool:
    """Deterministic and bounded; never true unless every opt-in and exclusion rule allows it."""
    if not (config.enabled and policy.opted_in):
        return False
    if sensitive and not policy.allow_high_risk:
        return False
    if risk in ("high", "critical") and not (config.allow_high_risk and policy.allow_high_risk):
        return False
    bucket = int(hashlib.sha256(f"{config.salt}|{unit_id}".encode()).hexdigest()[:8], 16) % 10_000
    return bucket < effective_rate(config) * 10_000


def redact_for_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact, then verify nothing secret-like remains; refuse to hand over anything unsafe."""
    redacted = dict(redact_value(payload))
    if has_secret_material(redacted):
        raise ProductionCaseRejected("payload still contains secret material after redaction")
    return redacted


def needs_review(*, passed: bool, safety_failed: bool, confidence: float | None) -> bool:
    """Failures and low-confidence outputs go to the human review queue."""
    return (not passed) or safety_failed or (confidence is not None and confidence < LOW_CONFIDENCE)


def promote_reviewed_cases(
    existing: list[EvalCaseSpec],
    reviewed: list[EvalCaseSpec],
    *,
    authorized_by: str,
) -> list[EvalCaseSpec]:
    """Build the case list of a NEW dataset version from the old cases plus reviewed ones.

    Each promoted case must carry a redaction attestation and clean content, and must not
    duplicate an existing case. The old version is never touched.
    """
    if not authorized_by:
        raise DatasetError("promotion requires an authorizing user")
    taken = {c.case_key for c in existing}
    promoted: list[EvalCaseSpec] = []
    for case in reviewed:
        if case.case_key in taken:
            raise DatasetError(f"case key {case.case_key!r} already exists")
        attested = case.model_copy(
            update={
                "source": "production_redacted",
                "split": "development",  # promoted cases never enter the held-out test split
                "input_data": {
                    **case.input_data,
                    "_provenance": {"redacted": True, "authorized_by": authorized_by},
                },
            }
        )
        validate_production_case(attested)
        promoted.append(attested)
    combined = [*existing, *promoted]
    duplicates = find_duplicates(combined)
    if duplicates:
        raise DatasetError(f"promotion would add duplicate cases: {duplicates[:3]}")
    return combined
