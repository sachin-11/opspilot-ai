"""Immutable approval binding: what a human approves is exactly what may be published."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalContext:
    workspace_id: UUID
    repository_id: UUID
    base_commit_sha: str
    revision_id: UUID
    patch_hash: str
    policy_version: str
    verification_hash: str
    expires_at: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "workspace_id": str(self.workspace_id),
            "repository_id": str(self.repository_id),
            "base_commit_sha": self.base_commit_sha,
            "revision_id": str(self.revision_id),
            "patch_hash": self.patch_hash,
            "policy_version": self.policy_version,
            "verification_hash": self.verification_hash,
            "expires_at": self.expires_at.isoformat(),
        }

    @property
    def digest(self) -> str:
        return canonical_hash(self.as_dict())


# Fields that must still match at publication time; expiry is checked against the clock.
BOUND_FIELDS = (
    "workspace_id",
    "repository_id",
    "base_commit_sha",
    "revision_id",
    "patch_hash",
    "policy_version",
    "verification_hash",
)


def binding_mismatches(
    stored: dict[str, Any],
    stored_hash: str,
    expires_at: datetime | None,
    current: dict[str, str],
    now: datetime,
) -> list[str]:
    """Names of bound values that changed (empty means the approval is exact and still valid)."""
    problems: list[str] = []
    if canonical_hash(stored) != stored_hash:
        problems.append("approval_record_tampered")
    problems.extend(field for field in BOUND_FIELDS if stored.get(field) != current.get(field))
    if expires_at is None or now >= expires_at:
        problems.append("expired")
    return problems
