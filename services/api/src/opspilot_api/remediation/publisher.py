"""Pull-request content and the provider-facing publication sequence (idempotent, fail closed)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from opspilot_api.remediation.safety import redact_text
from opspilot_api.remediation.writer import (
    WriteProvider,
    WritePullRequest,
    assert_fix_branch,
    commit_message_for,
)

NO_AUTO_MERGE = (
    "OpsPilot AI does not auto-merge, approve, or deploy this pull request. A human reviewer "
    "must review and merge it through your normal process."
)


def branch_name_for(prefix: str, remediation_id: UUID, patch_hash: str) -> str:
    """Deterministic and unique per remediation + exact patch, so retries reuse the same branch."""
    return f"{prefix}{str(remediation_id)[:8]}-{patch_hash[:8]}"


def _clean(value: str, limit: int) -> str:
    return redact_text(value, max_length=limit).replace("\x00", "")


def build_pull_request_content(
    *,
    remediation_id: UUID,
    incident_id: UUID,
    investigation_id: UUID,
    title: str,
    root_cause: str,
    evidence: Sequence[Mapping[str, Any]],
    base_commit_sha: str,
    patch_hash: str,
    files: Sequence[Mapping[str, Any]],
    risk_level: str,
    policy_decision: str,
    policy_version: str,
    checks: Sequence[Mapping[str, Any]],
    verification_state: str,
    limitations: list[str],
    rollback_guidance: str,
    approver: str | None,
) -> tuple[str, str]:
    """Title and body. Contains no secrets, hidden reasoning, or raw logs."""
    pr_title = f"[OpsPilot] {_clean(title, 160)}"
    lines = [
        f"## {_clean(title, 200)}",
        "",
        f"- **Incident:** `{incident_id}`",
        f"- **Investigation:** `{investigation_id}`",
        f"- **Remediation:** `{remediation_id}`",
        f"- **Base commit:** `{base_commit_sha}`",
        f"- **Patch hash:** `{patch_hash}`",
        f"- **Risk level:** {risk_level}",
        f"- **Policy decision:** {policy_decision} (policy `{policy_version}`)",
        f"- **Verification:** {verification_state}",
        f"- **Approved by:** {_clean(approver, 80) if approver else 'see OpsPilot audit log'}",
        "",
        "### Root-cause summary",
        _clean(root_cause, 1200),
        "",
        "### Evidence",
    ]
    lines += [
        f"- `{item.get('id')}`: {_clean(str(item.get('title', '')), 160)}" for item in evidence[:15]
    ]
    lines += ["", "### Patch summary"]
    lines += [
        f"- `{item['path']}` (+{item.get('added', 0)} / -{item.get('deleted', 0)})"
        for item in files[:20]
    ]
    lines += ["", "### Verification checks"]
    lines += [
        f"- {_clean(str(item.get('name', '')), 80)}: {item.get('baseline', 'n/a')} -> "
        f"{item.get('patched', 'n/a')}"
        for item in checks[:20]
    ]
    if limitations:
        lines += ["", "### Known limitations"]
        lines += [f"- {_clean(item, 300)}" for item in limitations[:10]]
    if rollback_guidance:
        lines += ["", "### Rollback guidance", _clean(rollback_guidance, 600)]
    lines += ["", "---", NO_AUTO_MERGE]
    return pr_title, "\n".join(lines)


@dataclass(frozen=True)
class PublishPlan:
    installation_id: int
    owner: str
    name: str
    default_branch: str
    base_sha: str
    branch: str
    branch_prefix: str
    changes: Mapping[str, str | None]
    patch_hash: str
    title: str
    body: str


@dataclass(frozen=True)
class PublishResult:
    commit_sha: str
    pull_request: WritePullRequest


def publish_to_provider(
    writer: WriteProvider,
    plan: PublishPlan,
    progress: Callable[[str, dict[str, Any]], None],
) -> PublishResult:
    """Branch -> single commit -> pull request. Every step is safe to repeat."""
    assert_fix_branch(plan.branch, plan.branch_prefix, plan.default_branch)
    writer.verify_write_access(plan.installation_id, plan.owner, plan.name)
    branch = writer.create_branch(
        plan.installation_id,
        plan.owner,
        plan.name,
        plan.branch,
        plan.base_sha,
        plan.default_branch,
        plan.branch_prefix,
    )
    progress("branch", {"created": branch.created})
    commit_sha = writer.create_commit(
        plan.installation_id,
        plan.owner,
        plan.name,
        plan.branch,
        plan.base_sha,
        plan.changes,
        commit_message_for(plan.title, plan.patch_hash),
        plan.patch_hash,
    )
    progress("commit", {"commit_sha": commit_sha})
    pull = writer.open_pull_request(
        plan.installation_id,
        plan.owner,
        plan.name,
        plan.branch,
        plan.default_branch,
        plan.title,
        plan.body,
    )
    progress("pull_request", {"number": pull.number})
    return PublishResult(commit_sha, pull)
