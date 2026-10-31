"""Dataset lifecycle rules: hashing, immutability, split isolation, duplicate detection, redaction.

These are pure functions so the same rules run in the CLI, the API service, and the tests.
"""

import hashlib
import json
import re
from typing import Any

from opspilot_api.investigations.safety import redact_value
from opspilot_api.llmops.eval_types import EvalCaseSpec
from opspilot_api.llmops.graders import _SECRET_PATTERNS

# What each purpose may read. The test split is never visible to prompt development.
ALLOWED_SPLITS: dict[str, frozenset[str]] = {
    "development": frozenset({"development"}),
    "tuning": frozenset({"development", "validation"}),
    "gate": frozenset({"development", "validation", "test"}),
}


class DatasetError(Exception):
    code = "dataset_error"


class SplitViolation(DatasetError):
    code = "split_violation"


class ImmutableDatasetError(DatasetError):
    code = "dataset_immutable"


class ProductionCaseRejected(DatasetError):
    code = "production_case_rejected"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def case_hash(case: EvalCaseSpec) -> str:
    return hashlib.sha256(canonical_json(case.model_dump(mode="json")).encode()).hexdigest()


def dataset_hash(cases: list[EvalCaseSpec]) -> str:
    """Order-independent: the same set of cases always hashes the same."""
    digest = hashlib.sha256()
    for value in sorted(case_hash(c) for c in cases):
        digest.update(value.encode())
    return digest.hexdigest()


def select_cases(cases: list[EvalCaseSpec], purpose: str) -> list[EvalCaseSpec]:
    """Return only the cases a purpose may see; the test split is excluded from development."""
    try:
        allowed = ALLOWED_SPLITS[purpose]
    except KeyError as exc:
        raise DatasetError(f"unknown dataset purpose {purpose!r}") from exc
    return [c for c in cases if c.split in allowed]


def require_split_access(purpose: str, split: str) -> None:
    if split not in ALLOWED_SPLITS.get(purpose, frozenset()):
        raise SplitViolation(f"the {split!r} split is not available for {purpose!r} runs")


def assert_publishable(status: str) -> None:
    if status == "published":
        raise ImmutableDatasetError("published dataset versions are immutable; create a new one")


# Context shared by many cases (the corpus, action allow-lists) must not dominate similarity.
_SHARED_INPUT_KEYS = frozenset(
    {"corpus", "allowed_actions", "prohibited_actions", "valid_evidence_ids", "workspace_id"}
)
_SHARED_EXPECTED_KEYS = frozenset({"allowed_transitions"})


def _signature(case: EvalCaseSpec) -> set[str]:
    blob = canonical_json(
        {
            "i": {k: v for k, v in case.input_data.items() if k not in _SHARED_INPUT_KEYS},
            "e": {k: v for k, v in case.expected_output.items() if k not in _SHARED_EXPECTED_KEYS},
            "t": [t.model_dump() for t in case.expected_tools],
            "r": case.required_evidence,
        }
    )
    return set(re.findall(r"[a-z0-9_\-\.]{3,}", blob.lower()))


def find_duplicates(
    cases: list[EvalCaseSpec], threshold: float = 0.97
) -> list[tuple[str, str, float]]:
    """Exact duplicates (same hash ignoring the key) and near-duplicates by Jaccard similarity.

    Only cases in the same suite are compared. Returns (key_a, key_b, similarity).
    """
    found: list[tuple[str, str, float]] = []
    sigs = [(c, _signature(c)) for c in cases]
    for i, (a, sa) in enumerate(sigs):
        for b, sb in sigs[i + 1 :]:
            if a.suite != b.suite:
                continue
            union = sa | sb
            similarity = len(sa & sb) / len(union) if union else 1.0
            if similarity >= threshold:
                found.append((a.case_key, b.case_key, round(similarity, 4)))
    return found


def has_secret_material(value: Any) -> bool:
    blob = canonical_json(value)
    return any(p.search(blob) for p in _SECRET_PATTERNS)


def validate_production_case(case: EvalCaseSpec) -> None:
    """Production-derived cases need an explicit redaction attestation and clean content."""
    if case.source != "production_redacted":
        return
    meta = case.input_data.get("_provenance", {})
    if not (meta.get("redacted") is True and meta.get("authorized_by")):
        raise ProductionCaseRejected(
            f"{case.case_key}: production cases require _provenance.redacted and authorized_by"
        )
    if redact_value(case.input_data) != case.input_data or has_secret_material(case.input_data):
        raise ProductionCaseRejected(f"{case.case_key}: content still contains redactable data")


def _scrub_markers(case: EvalCaseSpec) -> dict[str, Any]:
    """Replace planted secrets and foreign-tenant markers everywhere, then apply redaction."""
    needles = {
        str(v)
        for key in ("planted_secrets", "foreign_markers", "injection_canaries")
        for v in case.input_data.get(key, [])
        if str(v) and not str(v).startswith("ev-")
    }
    text = canonical_json(case.model_dump(mode="json"))
    for needle in sorted(needles, key=len, reverse=True):
        text = text.replace(needle, "[REDACTED]")
    return dict(redact_value(json.loads(text)))


def export_dataset(cases: list[EvalCaseSpec], *, name: str, version: int) -> dict[str, Any]:
    """Export for sharing. Everything is redacted, including the fabricated test canaries."""
    return {
        "name": name,
        "version": version,
        "content_hash": dataset_hash(cases),
        "case_count": len(cases),
        "cases": [_scrub_markers(c) for c in cases],
    }
