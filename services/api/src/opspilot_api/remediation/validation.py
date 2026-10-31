"""Structural validation of model output before the deterministic policy engine runs."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from opspilot_api.remediation.commands import is_registered
from opspilot_api.remediation.diffparse import (
    DiffError,
    FileDiff,
    apply_patch,
    normalize_patch,
    parse_unified_diff,
    patch_hash,
)
from opspilot_api.remediation.schemas import RemediationOutput

# Parse failures that are policy violations (blocked and recorded), not repairable syntax errors.
POLICY_TYPE_DIFF_CODES = frozenset({"binary_patch", "unsupported_operation", "mode_change"})


class OutputRejectedError(Exception):
    """Output is structurally unusable; the generator may repair it a bounded number of times."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StructuredPatch:
    patch_text: str
    patch_hash: str
    diffs: list[FileDiff] | None
    added: int
    deleted: int

    @property
    def files_changed(self) -> int:
        return len(self.diffs) if self.diffs is not None else 0


def validate_structure(
    output: RemediationOutput,
    allowed_evidence_ids: set[UUID],
    repository_full_name: str,
    base_commit_sha: str,
) -> StructuredPatch:
    """Citations, repository, base commit, command IDs, and diff syntax."""
    unknown = [item for item in output.evidence_ids if item not in allowed_evidence_ids]
    if unknown:
        raise OutputRejectedError(
            "invalid_evidence_citation", f"{len(unknown)} cited evidence ID(s) do not exist"
        )
    if output.target_repository.lower() != repository_full_name.lower():
        raise OutputRejectedError("unapproved_repository", "target repository is not approved")
    if output.base_commit_sha != base_commit_sha:
        raise OutputRejectedError("base_commit_mismatch", "patch targets a different base commit")
    bad = [item for item in output.verification_command_ids if not is_registered(item)]
    if bad:
        raise OutputRejectedError("arbitrary_command", "only registered command IDs are allowed")
    text = normalize_patch(output.unified_diff)
    try:
        diffs = parse_unified_diff(text)
    except DiffError as exc:
        if exc.code in POLICY_TYPE_DIFF_CODES:
            return StructuredPatch(text, patch_hash(text), None, 0, 0)
        raise OutputRejectedError(exc.code, str(exc)) from exc
    listed = {item.path for item in output.files}
    touched = {item.path for item in diffs}
    if listed != touched:
        raise OutputRejectedError(
            "file_list_mismatch", "files[] must list exactly the files changed by the diff"
        )
    return StructuredPatch(
        text,
        patch_hash(text),
        diffs,
        sum(item.added for item in diffs),
        sum(item.deleted for item in diffs),
    )


def check_applicable(diffs: list[FileDiff], read_base: Callable[[str], str | None]) -> None:
    """Dry-run application against the base revision; raises :class:`OutputRejectedError`."""
    try:
        apply_patch(diffs, read_base)
    except DiffError as exc:
        raise OutputRejectedError(exc.code, str(exc)) from exc
