"""Explicit remediation lifecycle: the only legal status transitions."""

from opspilot_api.incidents.service import ConflictError
from opspilot_api.remediation.models import RemediationStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DRAFTING: frozenset({S.POLICY_CHECK, S.POLICY_BLOCKED, S.STALE, S.FAILED, S.CANCELLED}),
    S.POLICY_CHECK: frozenset({S.SANDBOX_QUEUED, S.POLICY_BLOCKED, S.STALE, S.FAILED, S.CANCELLED}),
    S.SANDBOX_QUEUED: frozenset({S.SANDBOX_RUNNING, S.STALE, S.FAILED, S.CANCELLED}),
    S.SANDBOX_RUNNING: frozenset(
        {S.AWAITING_APPROVAL, S.VERIFICATION_FAILED, S.STALE, S.FAILED, S.CANCELLED}
    ),
    S.AWAITING_APPROVAL: frozenset(
        {S.APPROVED, S.REJECTED, S.DRAFTING, S.POLICY_CHECK, S.STALE, S.FAILED, S.CANCELLED}
    ),
    # Approved work can be re-verified (approval invalidated), published, or marked stale.
    S.APPROVED: frozenset({S.PUBLISHING, S.POLICY_CHECK, S.STALE, S.FAILED, S.CANCELLED}),
    # A transient provider failure returns to approved so publication can be retried.
    S.PUBLISHING: frozenset(
        {S.PULL_REQUEST_CREATED, S.APPROVED, S.POLICY_CHECK, S.STALE, S.FAILED}
    ),
    S.POLICY_BLOCKED: frozenset(),
    S.VERIFICATION_FAILED: frozenset(),
    S.PULL_REQUEST_CREATED: frozenset(),
    S.REJECTED: frozenset(),
    S.STALE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL = frozenset(status for status, targets in TRANSITIONS.items() if not targets)
ACTIVE = frozenset(S) - TERMINAL
RETRYABLE = frozenset({S.FAILED, S.STALE, S.POLICY_BLOCKED, S.VERIFICATION_FAILED, S.CANCELLED})


def can_transition(current: str, target: S) -> bool:
    return target in TRANSITIONS[S(current)]


def require_transition(current: str, target: S) -> None:
    if not can_transition(current, target):
        raise ConflictError(f"invalid remediation transition: {current} -> {target.value}")
