"""Prompt version lifecycle rules (draft -> candidate -> published -> retired).

Pure rules shared by the API service and the tests. Content is immutable once a version leaves
``draft``; rollback moves the default pointer to an older immutable version and never edits
history. Publication needs a passing gate report (or a privileged, audited override of *soft*
gates only).
"""

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any

from opspilot_api.llmops.dataset import canonical_json
from opspilot_api.llmops.gates import GateReport, OverrideDenied, apply_override
from opspilot_api.llmops.graders import _SECRET_PATTERNS

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_LIFECYCLE = {
    "draft": {"candidate", "retired"},
    "candidate": {"published", "draft", "retired"},
    "published": {"retired"},
    "retired": set(),
}


class PromptError(Exception):
    code = "prompt_error"


class ImmutablePromptError(PromptError):
    code = "prompt_immutable"


class PublicationBlocked(PromptError):
    code = "prompt_publication_blocked"


@dataclass(frozen=True)
class PromptVersion:
    id: str
    prompt_definition_id: str
    semantic_version: str
    system_template: str
    developer_template: str | None
    input_schema: dict[str, Any]  # {"fields": {name: {"untrusted": bool}}}
    output_schema: dict[str, Any]
    change_notes: str
    created_by_user_id: str
    status: str = "draft"
    is_default: bool = False
    published_by_user_id: str | None = None
    override_reason: str | None = None
    content_hash: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def compute_hash(
    system: str, developer: str | None, input_schema: dict[str, Any], output_schema: dict[str, Any]
) -> str:
    payload = canonical_json([system, developer, input_schema, output_schema])
    return hashlib.sha256(payload.encode()).hexdigest()


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def validate_content(system: str, developer: str | None, input_schema: dict[str, Any]) -> None:
    """Reject credentials and any template that interpolates untrusted data into instructions."""
    for label, text in (("system_template", system), ("developer_template", developer or "")):
        if any(p.search(text) for p in _SECRET_PATTERNS):
            raise PromptError(f"{label} appears to contain a credential")
    fields: dict[str, dict[str, Any]] = input_schema.get("fields", {})
    untrusted = {name for name, meta in fields.items() if meta.get("untrusted")}
    for label, text in (("system_template", system), ("developer_template", developer or "")):
        leaked = placeholders(text) & untrusted
        if leaked:
            raise PromptError(
                f"{label} must not interpolate untrusted fields {sorted(leaked)}; pass untrusted "
                "evidence only as separate, delimited data"
            )
    unknown = (placeholders(system) | placeholders(developer or "")) - set(fields)
    if unknown:
        raise PromptError(f"templates reference undeclared inputs {sorted(unknown)}")


def next_version(existing: list[str], bump: str = "patch") -> str:
    if not existing:
        return "1.0.0"
    major, minor, patch = max(tuple(int(x) for x in v.split(".")) for v in existing)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def create_version(
    *,
    version_id: str,
    definition_id: str,
    existing_versions: list[str],
    system_template: str,
    developer_template: str | None,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    change_notes: str,
    user_id: str,
    bump: str = "patch",
) -> PromptVersion:
    """Editing a published prompt always lands here: a new draft version, never an in-place edit."""
    validate_content(system_template, developer_template, input_schema)
    if not change_notes.strip():
        raise PromptError("change_notes are required")
    return PromptVersion(
        id=version_id,
        prompt_definition_id=definition_id,
        semantic_version=next_version(existing_versions, bump),
        system_template=system_template,
        developer_template=developer_template,
        input_schema=input_schema,
        output_schema=output_schema,
        change_notes=change_notes,
        created_by_user_id=user_id,
        content_hash=compute_hash(system_template, developer_template, input_schema, output_schema),
    )


def edit_content(version: PromptVersion, **_: Any) -> PromptVersion:
    if version.status != "draft":
        raise ImmutablePromptError(
            f"version {version.semantic_version} is {version.status}; create a new version"
        )
    return version  # drafts are edited by creating a replacement draft; kept explicit for tests


def verify_integrity(version: PromptVersion) -> bool:
    return version.content_hash == compute_hash(
        version.system_template,
        version.developer_template,
        version.input_schema,
        version.output_schema,
    )


def move(version: PromptVersion, target: str) -> PromptVersion:
    if target not in _LIFECYCLE[version.status]:
        raise PromptError(f"cannot move a prompt from {version.status} to {target}")
    return replace(version, status=target)


def publish(
    version: PromptVersion,
    siblings: list[PromptVersion],
    gate_report: GateReport | None,
    *,
    publisher_id: str,
    override_privileged: bool = False,
    override_reason: str | None = None,
) -> tuple[PromptVersion, list[PromptVersion]]:
    """Publish a candidate as the single default. Returns (published, updated siblings)."""
    if version.status != "candidate":
        raise PublicationBlocked("only a candidate version can be published")
    if not verify_integrity(version):
        raise PublicationBlocked("content hash mismatch: version content was altered")
    if gate_report is None:
        raise PublicationBlocked("publication requires an evaluation gate report")
    report = gate_report
    if not report.passed:
        if override_reason is None:
            names = ", ".join(r.gate for r in report.results if r.status == "fail")
            raise PublicationBlocked(f"quality gates failed: {names}")
        try:
            report = apply_override(report, privileged=override_privileged, reason=override_reason)
        except OverrideDenied as exc:
            raise PublicationBlocked(str(exc)) from exc
    published = replace(
        version,
        status="published",
        is_default=True,
        published_by_user_id=publisher_id,
        override_reason=override_reason if override_reason and not gate_report.passed else None,
    )
    cleared = [replace(s, is_default=False) if s.is_default else s for s in siblings]
    return published, cleared


def rollback(
    target: PromptVersion, siblings: list[PromptVersion]
) -> tuple[PromptVersion, list[PromptVersion]]:
    """Make an older immutable version the default. History (content, status) is untouched."""
    if target.status != "published":
        raise PromptError("rollback targets a previously published version")
    if not verify_integrity(target):
        raise PromptError("rollback target failed its integrity check")
    cleared = [
        replace(s, is_default=False) if s.is_default and s.id != target.id else s for s in siblings
    ]
    return replace(target, is_default=True), cleared


def default_version(versions: list[PromptVersion]) -> PromptVersion | None:
    defaults = [v for v in versions if v.is_default and v.status == "published"]
    if len(defaults) > 1:
        raise PromptError("invariant violated: more than one default published version")
    return defaults[0] if defaults else None
