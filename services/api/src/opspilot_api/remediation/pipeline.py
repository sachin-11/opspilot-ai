"""Database-backed remediation steps executed by RQ workers (called from the LangGraph nodes).

Each step is idempotent: it checks the expected lifecycle status first and does nothing when the
remediation has already moved past it, so RQ retries and checkpoint resumes never duplicate work.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from opspilot_api.auth.permissions import Permission
from opspilot_api.auth.service import has_permission_sync
from opspilot_api.config import Settings
from opspilot_api.incidents.service import utcnow
from opspilot_api.investigations.models import EvidenceItem, Investigation
from opspilot_api.remediation.approval import ApprovalContext
from opspilot_api.remediation.audit import emit_event, record_audit
from opspilot_api.remediation.binding import (
    check_binding as binding_check,
)
from opspilot_api.remediation.binding import (
    current_revision as binding_revision,
)
from opspilot_api.remediation.binding import (
    latest_approval,
    latest_run,
    outcome_from_rows,
    recompute_verification_hash,
)
from opspilot_api.remediation.checkout import Checkout, CheckoutError, fetch_checkout
from opspilot_api.remediation.commands import COMMANDS, default_command_ids, is_registered
from opspilot_api.remediation.diffparse import apply_patch, parse_unified_diff
from opspilot_api.remediation.generator import (
    PROMPT_VERSION,
    GeneratorError,
    RemediationGenerator,
    create_generator,
)
from opspilot_api.remediation.lifecycle import RemediationCancelledError, transition
from opspilot_api.remediation.models import (
    CheckStatus,
    FixPullRequest,
    PolicyEvaluation,
    PullRequestStatus,
    RemediationApproval,
    RemediationProposal,
    RemediationRevision,
    RemediationStatus,
    RemediationStep,
    RiskLevel,
    SandboxRun,
    VerificationCheck,
)
from opspilot_api.remediation.policy import (
    PolicyContext,
    check_path,
    evaluate_patch,
)
from opspilot_api.remediation.publisher import (
    PublishPlan,
    branch_name_for,
    build_pull_request_content,
    publish_to_provider,
)
from opspilot_api.remediation.safety import excerpt, redact_code, redact_text, sanitize_error
from opspilot_api.remediation.sandbox import (
    DockerSandboxProvider,
    SandboxLimits,
    SandboxProvider,
    SandboxUnavailableError,
    create_sandbox_provider,
)
from opspilot_api.remediation.schemas import (
    EvidenceContext,
    GenerationContext,
    RemediationOutput,
    VerificationComparison,
)
from opspilot_api.remediation.state import TERMINAL
from opspilot_api.remediation.validation import (
    OutputRejectedError,
    check_applicable,
    validate_structure,
)
from opspilot_api.remediation.verification import (
    RunOutcome,
    compare_runs,
    run_baseline,
    run_patched,
    verification_hash,
)
from opspilot_api.remediation.writer import WriteProvider, get_write_provider, write_enabled_for
from opspilot_api.repositories.models import GitHubInstallation, Repository
from opspilot_api.repositories.provider import ProviderError, RepositoryProvider
from opspilot_api.repositories.queue import get_provider

logger = logging.getLogger(__name__)
S = RemediationStatus
INFRASTRUCTURE_FAILURES = {"sandbox_unavailable", "sandbox_error"}
_PATH_TOKEN = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|json|ya?ml|toml)\b")
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")
_RISK_ORDER = {RiskLevel.LOW.value: 0, RiskLevel.MEDIUM.value: 1, RiskLevel.HIGH.value: 2}


class RemediationFailureError(Exception):
    """Terminal, user-visible failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalInvalidError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("approval is no longer valid: " + ", ".join(reasons))
        self.reasons = reasons


@dataclass
class RepositoryScope:
    repository: Repository
    installation_provider_id: int
    path_prefix: str | None
    default_branch: str


def _risk_max(first: str, second: str) -> str:
    return first if _RISK_ORDER.get(first, 3) >= _RISK_ORDER.get(second, 3) else second


class RemediationPipeline:
    def __init__(
        self,
        settings: Settings,
        generator: RemediationGenerator | None = None,
        sandbox_provider: SandboxProvider | None = None,
        repo_provider: RepositoryProvider | None = None,
        write_provider: WriteProvider | None = None,
    ) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url, pool_pre_ping=True)
        self.generator = generator or create_generator(settings)
        self.sandbox_provider = sandbox_provider or create_sandbox_provider(settings)
        self._repo_provider = repo_provider
        self._write_provider = write_provider
        self._checkouts: dict[tuple[UUID, str], Checkout] = {}

    def close(self) -> None:
        self.engine.dispose()

    @property
    def repo_provider(self) -> RepositoryProvider:
        return self._repo_provider or get_provider(self.settings)

    @property
    def write_provider(self) -> WriteProvider:
        return self._write_provider or get_write_provider(self.settings, self.repo_provider)

    @staticmethod
    def _skip(proposal: RemediationProposal) -> dict[str, Any]:
        """Step already done (retry/resume): halt only when the remediation is terminal."""
        return {"halt": S(proposal.status) in TERMINAL}

    # -- persistence helpers ----------------------------------------------------------------
    def load(
        self, session: Session, workspace_id: UUID, remediation_id: UUID
    ) -> RemediationProposal:
        proposal = session.scalar(
            select(RemediationProposal).where(
                RemediationProposal.id == remediation_id,
                RemediationProposal.workspace_id == workspace_id,
            )
        )
        if proposal is None:
            raise LookupError("remediation not found in workspace")
        if proposal.status == S.CANCELLED.value:
            raise RemediationCancelledError
        return proposal

    def transition(
        self, session: Session, proposal: RemediationProposal, target: S, **fields: Any
    ) -> None:
        transition(session, proposal, target, **fields)

    def emit(
        self, proposal: RemediationProposal, event_type: str, data: dict[str, Any] | None = None
    ) -> None:
        emit_event(
            self.settings,
            proposal.workspace_id,
            proposal.incident_id,
            proposal.id,
            event_type,
            {"status": proposal.status, **(data or {})},
        )

    def audit(
        self,
        session: Session,
        proposal: RemediationProposal,
        action: str,
        target_type: str,
        target_id: str | UUID | None,
        result: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        record_audit(
            session,
            proposal.workspace_id,
            proposal.id,
            action,
            target_type,
            target_id,
            result,
            details=details,
        )

    def begin_step(
        self, session: Session, proposal: RemediationProposal, node: str, display: str
    ) -> RemediationStep:
        step = session.scalar(
            select(RemediationStep).where(
                RemediationStep.remediation_id == proposal.id,
                RemediationStep.node_name == node,
                RemediationStep.status == "running",
            )
        )
        if step is None:
            attempt = (
                session.scalar(
                    select(func.coalesce(func.max(RemediationStep.attempt), 0)).where(
                        RemediationStep.remediation_id == proposal.id,
                        RemediationStep.node_name == node,
                    )
                )
                or 0
            ) + 1
            step = RemediationStep(
                id=uuid4(),
                workspace_id=proposal.workspace_id,
                remediation_id=proposal.id,
                node_name=node,
                display_name=display,
                status="running",
                attempt=attempt,
                started_at=utcnow(),
            )
            session.add(step)
        proposal.updated_at = utcnow()
        session.commit()
        return step

    def complete_step(
        self,
        session: Session,
        step: RemediationStep,
        summary: str,
        output: dict[str, Any] | None = None,
        status: str = "completed",
        error_code: str | None = None,
    ) -> None:
        step.status = status
        step.summary = summary[:1000]
        step.structured_output = output
        step.error_code = error_code
        step.completed_at = utcnow()
        session.commit()

    # -- scope ------------------------------------------------------------------------------
    def scope(self, session: Session, proposal: RemediationProposal) -> RepositoryScope:
        repository = session.scalar(
            select(Repository).where(
                Repository.id == proposal.repository_id,
                Repository.workspace_id == proposal.workspace_id,
            )
        )
        if repository is None:
            raise RemediationFailureError("repository_missing", "repository no longer exists")
        installation = session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.id == repository.github_installation_id,
                GitHubInstallation.workspace_id == proposal.workspace_id,
            )
        )
        if installation is None or installation.status != "active":
            raise RemediationFailureError("installation_inactive", "GitHub installation inactive")
        scope = proposal.structured_output.get("scope", {})
        return RepositoryScope(
            repository,
            installation.provider_installation_id,
            scope.get("path_prefix"),
            repository.default_branch,
        )

    def checkout(self, proposal: RemediationProposal, scope: RepositoryScope) -> Checkout:
        key = (proposal.id, proposal.base_commit_sha)
        cached = self._checkouts.get(key)
        if cached is not None:
            return cached
        try:
            fetched = fetch_checkout(
                self.settings,
                self.repo_provider,
                scope.installation_provider_id,
                scope.repository.owner,
                scope.repository.name,
                proposal.base_commit_sha,
            )
        except CheckoutError as exc:
            raise RemediationFailureError(exc.code, str(exc)) from exc
        self._checkouts[key] = fetched
        return fetched

    def policy_context(
        self, proposal: RemediationProposal, scope: RepositoryScope
    ) -> PolicyContext:
        return PolicyContext.from_settings(
            self.settings,
            path_prefix=scope.path_prefix,
            default_branch=scope.default_branch,
            target_branch=branch_name_for(
                self.settings.REMEDIATION_BRANCH_PREFIX, proposal.id, proposal.patch_hash or "0" * 8
            ),
        )

    def fail(
        self, session: Session, proposal: RemediationProposal, code: str, message: str
    ) -> None:
        if proposal.status in {S.CANCELLED.value, S.FAILED.value}:
            return
        self.transition(
            session,
            proposal,
            S.FAILED,
            failed_at=utcnow(),
            failure_code=code,
            failure_message=redact_text(message, max_length=500),
        )
        self.audit(
            session,
            proposal,
            "remediation_failed",
            "remediation",
            proposal.id,
            "failure",
            {"failure_code": code},
        )
        session.commit()
        self.emit(proposal, "remediation.failed", {"failure_code": code})

    # -- step 1-3: context ------------------------------------------------------------------
    def load_context(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.DRAFTING.value:
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "load_context", "Load approved investigation")
            investigation = session.scalar(
                select(Investigation).where(
                    Investigation.id == proposal.investigation_id,
                    Investigation.workspace_id == workspace_id,
                )
            )
            count = session.scalar(
                select(func.count())
                .select_from(EvidenceItem)
                .where(
                    EvidenceItem.investigation_id == proposal.investigation_id,
                    EvidenceItem.workspace_id == workspace_id,
                )
            )
            if investigation is None or not investigation.root_cause or not count:
                self.complete_step(
                    session, step, "Investigation no longer eligible", None, "failed"
                )
                self.fail(
                    session, proposal, "investigation_ineligible", "investigation not eligible"
                )
                return {"halt": True}
            self.complete_step(
                session,
                step,
                f"Loaded approved investigation with {count} evidence items.",
                {"evidence_count": count},
            )
            self.emit(proposal, "remediation.started")
            return {"halt": False}

    def select_repository(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.DRAFTING.value:
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "select_repository", "Select repository")
            try:
                scope = self.scope(session, proposal)
                if scope.repository.status != "active" or scope.repository.is_archived:
                    raise RemediationFailureError("repository_inactive", "repository not active")
                self.repo_provider.get_commit(
                    scope.installation_provider_id,
                    scope.repository.owner,
                    scope.repository.name,
                    proposal.base_commit_sha,
                    1,
                )
            except (RemediationFailureError, ProviderError) as exc:
                code = getattr(exc, "code", "repository_unavailable")
                self.complete_step(
                    session, step, "Repository could not be selected", None, "failed", code
                )
                self.fail(session, proposal, code, str(exc))
                return {"halt": True}
            self.complete_step(
                session,
                step,
                f"Pinned {scope.repository.full_name} at {proposal.base_commit_sha[:12]}.",
                {
                    "repository": scope.repository.full_name,
                    "base_commit_sha": proposal.base_commit_sha,
                },
            )
            return {"halt": False}

    def select_context_files(
        self,
        checkout: Checkout,
        evidence: list[EvidenceItem],
        root_cause: str,
        path_prefix: str | None,
    ) -> dict[str, str]:
        """Bounded, redacted, in-scope source files most relevant to the evidence."""
        ctx = PolicyContext.from_settings(self.settings, path_prefix=path_prefix)
        candidates = {
            path: data
            for path, data in checkout.files.items()
            if not check_path(path, ctx) and b"\x00" not in data
        }
        scores: dict[str, int] = dict.fromkeys(candidates, 0)
        words = set(_WORD.findall(root_cause.lower()))
        for item in evidence:
            blob = " ".join(
                [item.title, item.excerpt, *[str(v) for v in item.evidence_metadata.values()]]
            )
            for token in _PATH_TOKEN.findall(blob):
                for path in candidates:
                    if path == token or path.endswith("/" + token.lstrip("./")):
                        scores[path] += 10
            words |= set(_WORD.findall(blob.lower()))
        for path in candidates:
            stem = set(_WORD.findall(path.lower().replace("/", " ")))
            scores[path] += len(stem & words)
        chosen: dict[str, str] = {}
        budget = self.settings.REMEDIATION_MAX_CONTEXT_BYTES
        for path, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
            if score <= 0 or len(chosen) >= self.settings.REMEDIATION_MAX_CONTEXT_FILES:
                continue
            text = candidates[path].decode("utf-8", errors="replace")
            if len(text) > budget:
                continue
            chosen[path] = redact_code(text)
            budget -= len(text)
        # Tests next to selected files help the model write a targeted regression test.
        return chosen

    def select_code_context(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.DRAFTING.value:
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "select_code_context", "Select code context")
            try:
                scope = self.scope(session, proposal)
                checkout = self.checkout(proposal, scope)
            except RemediationFailureError as exc:
                self.complete_step(session, step, "Checkout failed", None, "failed", exc.code)
                self.fail(session, proposal, exc.code, str(exc))
                return {"halt": True}
            evidence = self._evidence(session, proposal)
            files = self.select_context_files(
                checkout, evidence, self._root_cause(session, proposal), scope.path_prefix
            )
            self.complete_step(
                session,
                step,
                f"Selected {len(files)} bounded source files from {len(checkout.files)}.",
                {
                    "files": sorted(files),
                    "tree_hash": checkout.tree_hash,
                    "skipped_symlinks": len(checkout.skipped_symlinks),
                    "skipped_submodules": len(checkout.skipped_submodules),
                },
            )
            return {"halt": False}

    def _evidence(self, session: Session, proposal: RemediationProposal) -> list[EvidenceItem]:
        return list(
            session.scalars(
                select(EvidenceItem)
                .where(
                    EvidenceItem.investigation_id == proposal.investigation_id,
                    EvidenceItem.workspace_id == proposal.workspace_id,
                )
                .order_by(EvidenceItem.collected_at, EvidenceItem.id)
            )
        )

    def _root_cause(self, session: Session, proposal: RemediationProposal) -> str:
        investigation = session.scalar(
            select(Investigation).where(
                Investigation.id == proposal.investigation_id,
                Investigation.workspace_id == proposal.workspace_id,
            )
        )
        return (investigation.root_cause if investigation else "") or ""

    # -- step 4: generate -------------------------------------------------------------------
    def generate_patch(
        self, workspace_id: UUID, remediation_id: UUID, feedback: str | None = None
    ) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.DRAFTING.value:
                return self._skip(proposal)
            step = self.begin_step(
                session, proposal, "generate_patch", "Generate constrained patch"
            )
            if proposal.revision_count >= self.settings.REMEDIATION_MAX_REVISIONS:
                self.complete_step(session, step, "Revision limit reached", None, "failed")
                self.fail(session, proposal, "max_revisions_reached", "maximum revisions reached")
                return {"halt": True}
            try:
                return self._generate(session, proposal, step, feedback)
            except RemediationFailureError as exc:
                self.complete_step(
                    session, step, f"Generation failed: {exc.code}", None, "failed", exc.code
                )
                self.fail(session, proposal, exc.code, str(exc))
                return {"halt": True}

    def _generate(
        self,
        session: Session,
        proposal: RemediationProposal,
        step: RemediationStep,
        feedback: str | None,
    ) -> dict[str, Any]:
        scope = self.scope(session, proposal)
        checkout = self.checkout(proposal, scope)
        evidence = self._evidence(session, proposal)
        root_cause = self._root_cause(session, proposal)
        investigation = session.get(Investigation, proposal.investigation_id)
        files = self.select_context_files(checkout, evidence, root_cause, scope.path_prefix)
        context = GenerationContext(
            investigation_title=(investigation.title if investigation else None) or "",
            root_cause=root_cause,
            evidence=[
                EvidenceContext(
                    id=e.id, title=e.title, excerpt=e.excerpt, source_type=e.source_type
                )
                for e in evidence
            ],
            repository_full_name=scope.repository.full_name,
            base_commit_sha=proposal.base_commit_sha,
            path_prefix=scope.path_prefix,
            code_files=files,
            available_command_ids=sorted(COMMANDS),
            human_feedback=feedback,
        )
        allowed_ids = {e.id for e in evidence}
        policy_ctx = self.policy_context(proposal, scope)
        attempts = 0
        input_tokens = output_tokens = 0
        while True:
            try:
                result = self.generator.generate(context)
            except GeneratorError as exc:
                raise RemediationFailureError(exc.code, str(exc)) from exc
            input_tokens += result.usage.input_tokens
            output_tokens += result.usage.output_tokens
            output = result.output
            if output.abstain:
                self._record_usage(proposal, input_tokens, output_tokens)
                reason = output.abstain_reason or "no_safe_patch"
                self.complete_step(
                    session, step, f"Abstained: {reason}", {"abstain_reason": reason}, "completed"
                )
                self.audit(
                    session,
                    proposal,
                    "patch_abstained",
                    "remediation",
                    proposal.id,
                    "abstained",
                    {"reason": reason},
                )
                self.fail(
                    session,
                    proposal,
                    f"abstained_{reason}",
                    output.abstain_explanation or "No safe automated patch is available.",
                )
                return {"halt": True}
            try:
                structured = validate_structure(
                    output, allowed_ids, scope.repository.full_name, proposal.base_commit_sha
                )
                if (
                    structured.diffs is not None
                    and not evaluate_patch(structured.patch_text, policy_ctx).blocked
                ):

                    def read_base(path: str) -> str | None:
                        data = checkout.files.get(path)
                        return None if data is None else data.decode("utf-8")

                    check_applicable(structured.diffs, read_base)
                break
            except (OutputRejectedError, UnicodeDecodeError) as exc:
                attempts += 1
                code = getattr(exc, "code", "invalid_patch")
                if attempts > self.settings.REMEDIATION_MAX_REPAIR_ATTEMPTS:
                    self._record_usage(proposal, input_tokens, output_tokens)
                    raise RemediationFailureError(
                        "invalid_patch", f"model output rejected after repair attempts: {code}"
                    ) from exc
                context = context.model_copy(update={"repair_errors": [f"{code}: {exc}"][:3]})
        revision = self._store_revision(session, proposal, output, structured)
        self._record_usage(proposal, input_tokens, output_tokens)
        self.complete_step(
            session,
            step,
            f"Generated patch touching {structured.files_changed} file(s) "
            f"(+{structured.added}/-{structured.deleted}); repair attempts: {attempts}.",
            {
                "revision_id": str(revision.id),
                "patch_hash": revision.patch_hash,
                "repairs": attempts,
            },
        )
        self.audit(
            session,
            proposal,
            "patch_generated",
            "revision",
            revision.id,
            "success",
            {"patch_hash": revision.patch_hash, "files": structured.files_changed},
        )
        session.commit()
        self.emit(proposal, "remediation.patch_generated", {"revision_id": str(revision.id)})
        return {"halt": False}

    def _record_usage(
        self, proposal: RemediationProposal, input_tokens: int, output_tokens: int
    ) -> None:
        proposal.total_input_tokens += input_tokens
        proposal.total_output_tokens += output_tokens
        if (
            self.settings.AI_INPUT_PRICE_PER_MILLION is not None
            and self.settings.AI_OUTPUT_PRICE_PER_MILLION is not None
        ):
            cost = (
                input_tokens * self.settings.AI_INPUT_PRICE_PER_MILLION
                + output_tokens * self.settings.AI_OUTPUT_PRICE_PER_MILLION
            ) / 1_000_000
            proposal.estimated_cost_usd = round(float(proposal.estimated_cost_usd or 0) + cost, 6)

    def _store_revision(
        self,
        session: Session,
        proposal: RemediationProposal,
        output: RemediationOutput,
        structured: Any,
    ) -> RemediationRevision:
        number = proposal.revision_count + 1
        revision = RemediationRevision(
            id=uuid4(),
            workspace_id=proposal.workspace_id,
            remediation_id=proposal.id,
            revision_number=number,
            base_commit_sha=proposal.base_commit_sha,
            patch_text=structured.patch_text,
            patch_hash=structured.patch_hash,
            change_summary=(output.expected_behavior_change or output.title)[:1000],
            files_changed=structured.files_changed,
            lines_added=structured.added,
            lines_deleted=structured.deleted,
        )
        session.add(revision)
        session.flush()
        previous_scope = proposal.structured_output.get("scope", {})
        risk = output.risk_assessment
        proposal.title = output.title[:240]
        proposal.problem_statement = output.problem_statement
        proposal.proposed_solution = "\n".join(
            [output.expected_behavior_change, *[f"{f.path}: {f.explanation}" for f in output.files]]
        ).strip()
        proposal.risk_level = risk.level if risk else RiskLevel.HIGH.value
        proposal.affected_components = sorted(
            {f.path.rsplit("/", 1)[0] if "/" in f.path else "." for f in output.files}
        )
        proposal.patch_text = structured.patch_text
        proposal.patch_hash = structured.patch_hash
        proposal.files_changed = structured.files_changed
        proposal.lines_added = structured.added
        proposal.lines_deleted = structured.deleted
        proposal.current_revision_id = revision.id
        proposal.revision_count = number
        proposal.generator_prompt_version = PROMPT_VERSION
        proposal.generator_provider = self.generator.provider_name
        proposal.generator_model = self.generator.model_name
        proposal.structured_output = {
            "scope": previous_scope,
            "evidence_ids": [str(item) for item in output.evidence_ids],
            "files": [f.model_dump() for f in output.files],
            "expected_behavior_change": output.expected_behavior_change,
            "risk_assessment": risk.model_dump() if risk else None,
            "verification_command_ids": output.verification_command_ids,
            "rollback_guidance": output.rollback_guidance,
            "known_limitations": output.known_limitations,
        }
        proposal.updated_at = utcnow()
        return revision

    # -- step 5: policy ---------------------------------------------------------------------
    def current_revision(
        self, session: Session, proposal: RemediationProposal
    ) -> RemediationRevision:
        revision = binding_revision(session, proposal)
        if revision is None:
            raise RemediationFailureError("revision_missing", "no current revision")
        return revision

    def policy_check(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status == S.DRAFTING.value:
                self.transition(session, proposal, S.POLICY_CHECK)
            if proposal.status != S.POLICY_CHECK.value:
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "policy_check", "Deterministic policy check")
            try:
                scope = self.scope(session, proposal)
                revision = self.current_revision(session, proposal)
            except RemediationFailureError as exc:
                self.complete_step(session, step, "Policy check failed", None, "failed", exc.code)
                self.fail(session, proposal, exc.code, str(exc))
                return {"halt": True}
            result = evaluate_patch(revision.patch_text, self.policy_context(proposal, scope))
            evaluation = PolicyEvaluation(
                id=uuid4(),
                workspace_id=proposal.workspace_id,
                remediation_id=proposal.id,
                revision_id=revision.id,
                policy_version=result.version,
                decision=result.decision.value,
                risk_level=result.risk_level.value,
                violations=result.violations,
                warnings=result.warnings,
            )
            session.add(evaluation)
            risk = (
                RiskLevel.PROHIBITED.value
                if result.blocked
                else _risk_max(proposal.risk_level, result.risk_level.value)
            )
            self.audit(
                session,
                proposal,
                "policy_decision",
                "revision",
                revision.id,
                "blocked" if result.blocked else "allowed",
                {
                    "decision": result.decision.value,
                    "version": result.version,
                    "violations": [v["code"] for v in result.violations],
                },
            )
            summary = (
                f"Policy {result.version}: {result.decision.value}"
                f" ({len(result.violations)} violations, {len(result.warnings)} warnings)."
            )
            self.complete_step(
                session,
                step,
                summary,
                {
                    "decision": result.decision.value,
                    "violations": [v["code"] for v in result.violations],
                },
            )
            if result.blocked:
                self.transition(
                    session,
                    proposal,
                    S.POLICY_BLOCKED,
                    policy_version=result.version,
                    policy_decision=result.decision.value,
                    risk_level=risk,
                    completed_at=utcnow(),
                )
                self.emit(
                    proposal,
                    "remediation.policy_blocked",
                    {"violations": [v["code"] for v in result.violations]},
                )
                return {"halt": True}
            self.transition(
                session,
                proposal,
                S.SANDBOX_QUEUED,
                policy_version=result.version,
                policy_decision=result.decision.value,
                risk_level=risk,
            )
            self.emit(proposal, "remediation.policy_allowed", {"decision": result.decision.value})
            return {"halt": False}

    # -- steps 6-9: sandbox -----------------------------------------------------------------
    def prepare_sandbox(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.SANDBOX_QUEUED.value:
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "prepare_sandbox", "Prepare sandbox")
            try:
                if proposal.sandbox_attempts >= self.settings.REMEDIATION_SANDBOX_MAX_ATTEMPTS:
                    raise RemediationFailureError(
                        "sandbox_attempts_exhausted", "maximum sandbox attempts reached"
                    )
                scope = self.scope(session, proposal)
                if isinstance(self.sandbox_provider, DockerSandboxProvider):
                    self.sandbox_provider.check_available()
                self._ensure_base_current(scope, proposal)
                self.checkout(proposal, scope)
            except SandboxUnavailableError as exc:
                self.complete_step(session, step, "Sandbox unavailable", None, "failed", exc.code)
                self.fail(session, proposal, exc.code, str(exc))
                return {"halt": True}
            except RemediationFailureError as exc:
                self.complete_step(
                    session,
                    step,
                    f"Sandbox preparation failed: {exc.code}",
                    None,
                    "failed",
                    exc.code,
                )
                if exc.code == "base_commit_stale":
                    self._mark_stale(session, proposal, str(exc))
                else:
                    self.fail(session, proposal, exc.code, str(exc))
                return {"halt": True}
            self.transition(
                session, proposal, S.SANDBOX_RUNNING, sandbox_attempts=proposal.sandbox_attempts + 1
            )
            self.complete_step(session, step, "Sandbox prepared (network disabled, non-root).")
            self.audit(
                session,
                proposal,
                "sandbox_started",
                "remediation",
                proposal.id,
                "success",
                {"provider": self.sandbox_provider.name},
            )
            session.commit()
            self.emit(
                proposal, "remediation.sandbox_started", {"provider": self.sandbox_provider.name}
            )
            return {"halt": False}

    def _ensure_base_current(self, scope: RepositoryScope, proposal: RemediationProposal) -> None:
        try:
            head = self.repo_provider.get_branch(
                scope.installation_provider_id,
                scope.repository.owner,
                scope.repository.name,
                scope.default_branch,
            ).head_sha
        except ProviderError as exc:
            raise RemediationFailureError(exc.code, "cannot confirm repository state") from exc
        if head != proposal.base_commit_sha:
            raise RemediationFailureError(
                "base_commit_stale", "the default branch advanced past the approved base commit"
            )

    def _mark_stale(self, session: Session, proposal: RemediationProposal, reason: str) -> None:
        if proposal.status in {S.STALE.value, S.CANCELLED.value}:
            return
        self.transition(
            session, proposal, S.STALE, stale_reason=reason[:200], completed_at=utcnow()
        )
        self.audit(
            session,
            proposal,
            "remediation_stale",
            "remediation",
            proposal.id,
            "stale",
            {"reason": reason[:200]},
        )
        session.commit()
        self.emit(proposal, "remediation.stale", {"reason": reason[:200]})

    def command_ids(self, proposal: RemediationProposal, files: set[str]) -> list[str]:
        selected = [
            item
            for item in proposal.structured_output.get("verification_command_ids", [])
            if is_registered(item)
        ]
        return selected or default_command_ids(files)

    def _limits(self) -> SandboxLimits:
        return SandboxLimits.from_settings(self.settings)

    def _persist_run(
        self,
        session: Session,
        proposal: RemediationProposal,
        revision: RemediationRevision,
        outcome: RunOutcome,
        baseline_run_id: UUID | None,
    ) -> SandboxRun:
        limits = self._limits()
        failing = next(
            (
                c
                for c in outcome.checks
                if c.status is not CheckStatus.PASSED and c.status is not CheckStatus.SKIPPED
            ),
            None,
        )
        run = SandboxRun(
            id=uuid4(),
            workspace_id=proposal.workspace_id,
            remediation_id=proposal.id,
            revision_id=revision.id,
            run_kind=outcome.kind,
            attempt=proposal.sandbox_attempts,
            status=outcome.status.value,
            sandbox_provider=self.sandbox_provider.name,
            base_commit_sha=proposal.base_commit_sha,
            patch_hash=revision.patch_hash,
            baseline_run_id=baseline_run_id,
            cpu_limit=limits.cpu,
            memory_limit_mb=limits.memory_mb,
            disk_limit_mb=limits.disk_mb,
            timeout_seconds=limits.timeout_seconds,
            network_enabled=limits.network_enabled,
            exit_code=failing.exit_code if failing else 0,
            stdout_excerpt=excerpt(failing.stdout) if failing else None,
            stderr_excerpt=excerpt(failing.stderr) if failing else None,
            output_truncated=any(c.truncated for c in outcome.checks),
            base_tree_hash=outcome.tree_hash,
            cleanup_verified=outcome.cleanup_verified,
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
            failed_at=outcome.completed_at if outcome.failure_code else None,
            failure_code=outcome.failure_code,
            failure_message=outcome.failure_message,
        )
        session.add(run)
        session.flush()
        for check in outcome.checks:
            session.add(
                VerificationCheck(
                    id=uuid4(),
                    workspace_id=proposal.workspace_id,
                    sandbox_run_id=run.id,
                    check_type="baseline" if outcome.kind == "baseline" else check.check_type,
                    command_id=check.command_id,
                    display_name=check.display_name,
                    status=check.status.value,
                    exit_code=check.exit_code,
                    duration_ms=check.duration_ms,
                    stdout_excerpt=excerpt(check.stdout) if check.stdout else None,
                    stderr_excerpt=excerpt(check.stderr) if check.stderr else None,
                    output_truncated=check.truncated,
                    failing_tests=check.failing_tests[:100],
                )
            )
        session.commit()
        return run

    def _existing_run(
        self, session: Session, proposal: RemediationProposal, kind: str
    ) -> SandboxRun | None:
        return session.scalar(
            select(SandboxRun).where(
                SandboxRun.remediation_id == proposal.id,
                SandboxRun.workspace_id == proposal.workspace_id,
                SandboxRun.revision_id == proposal.current_revision_id,
                SandboxRun.run_kind == kind,
                SandboxRun.attempt == proposal.sandbox_attempts,
            )
        )

    def baseline_checks(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.SANDBOX_RUNNING.value or self._existing_run(
                session, proposal, "baseline"
            ):
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "baseline_checks", "Baseline checks")
            scope = self.scope(session, proposal)
            revision = self.current_revision(session, proposal)
            checkout = self.checkout(proposal, scope)
            outcome = run_baseline(
                self.sandbox_provider,
                self._limits(),
                checkout,
                self.command_ids(proposal, set(checkout.files)),
                scope.path_prefix,
            )
            self._persist_run(session, proposal, revision, outcome, None)
            self.complete_step(
                session,
                step,
                f"Baseline: {sum(c.status is CheckStatus.PASSED for c in outcome.checks)}/"
                f"{len(outcome.checks)} checks passed.",
                {"status": outcome.status.value},
            )
            self.audit(
                session,
                proposal,
                "sandbox_stopped",
                "sandbox_run",
                None,
                outcome.status.value,
                {"kind": "baseline", "cleanup_verified": outcome.cleanup_verified},
            )
            session.commit()
            self.emit(
                proposal,
                "remediation.baseline_completed",
                {"baseline_status": outcome.status.value},
            )
            return {"halt": False}

    def patched_checks(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.SANDBOX_RUNNING.value or self._existing_run(
                session, proposal, "patched"
            ):
                return self._skip(proposal)
            step = self.begin_step(session, proposal, "patched_checks", "Apply patch and verify")
            scope = self.scope(session, proposal)
            revision = self.current_revision(session, proposal)
            checkout = self.checkout(proposal, scope)
            baseline = self._existing_run(session, proposal, "baseline")
            diffs = parse_unified_diff(revision.patch_text)
            outcome = run_patched(
                self.sandbox_provider,
                self._limits(),
                checkout,
                diffs,
                self.command_ids(proposal, set(checkout.files)),
                scope.path_prefix,
            )
            self._persist_run(
                session, proposal, revision, outcome, baseline.id if baseline else None
            )
            self.complete_step(
                session,
                step,
                f"Patched run: {outcome.status.value}.",
                {"status": outcome.status.value},
            )
            self.audit(
                session,
                proposal,
                "sandbox_stopped",
                "sandbox_run",
                None,
                outcome.status.value,
                {"kind": "patched", "cleanup_verified": outcome.cleanup_verified},
            )
            session.commit()
            return {"halt": False}

    def _outcome_from_rows(self, session: Session, run: SandboxRun) -> RunOutcome:
        return outcome_from_rows(session, run)

    def compare_results(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.SANDBOX_RUNNING.value:
                return self._skip(proposal)
            step = self.begin_step(
                session, proposal, "compare_results", "Compare baseline and patched"
            )
            baseline_run = self._existing_run(session, proposal, "baseline")
            patched_run = self._existing_run(session, proposal, "patched")
            if baseline_run is None or patched_run is None:
                self.complete_step(
                    session, step, "Verification runs missing", None, "failed", "runs_missing"
                )
                self.fail(session, proposal, "verification_incomplete", "sandbox runs are missing")
                return {"halt": True}
            baseline = self._outcome_from_rows(session, baseline_run)
            patched = self._outcome_from_rows(session, patched_run)
            infra = (patched.failure_code or baseline.failure_code) in INFRASTRUCTURE_FAILURES
            comparison = compare_runs(baseline, patched)
            vhash = verification_hash(baseline, patched)
            patched_run.result_hash = vhash
            baseline_run.result_hash = vhash
            proposal.structured_output = {
                **proposal.structured_output,
                "verification": comparison.model_dump(),
            }
            self.audit(
                session,
                proposal,
                "verification_result",
                "sandbox_run",
                patched_run.id,
                comparison.state,
                {
                    "state": comparison.state,
                    "verification_hash": vhash,
                    "new_failures": len(comparison.new_failures),
                },
            )
            self.complete_step(
                session,
                step,
                f"Verification {comparison.state}: "
                f"{len(comparison.new_failures)} new, "
                f"{len(comparison.existing_failures)} existing failures.",
                {"state": comparison.state},
            )
            if infra:
                self.fail(
                    session,
                    proposal,
                    patched.failure_code or baseline.failure_code or "sandbox_error",
                    "sandbox infrastructure failed during verification",
                )
                return {"halt": True}
            if comparison.state in {"passed", "partial"}:
                self.transition(
                    session,
                    proposal,
                    S.AWAITING_APPROVAL,
                    verification_hash=vhash,
                    verification_state=comparison.state,
                )
                self.emit(
                    proposal, "remediation.verification_completed", {"state": comparison.state}
                )
                return {"halt": False}
            self.transition(
                session,
                proposal,
                S.VERIFICATION_FAILED,
                verification_hash=vhash,
                verification_state=comparison.state,
                completed_at=utcnow(),
            )
            self.emit(proposal, "remediation.verification_failed", {"state": comparison.state})
            return {"halt": True}

    def verification_report(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status != S.AWAITING_APPROVAL.value:
                return self._skip(proposal)
            already = session.scalars(
                select(RemediationStep).where(
                    RemediationStep.remediation_id == proposal.id,
                    RemediationStep.node_name == "verification_report",
                    RemediationStep.status == "completed",
                )
            ).all()
            if any(
                isinstance(item.structured_output, dict)
                and item.structured_output.get("verification_hash") == proposal.verification_hash
                for item in already
            ):
                return {"halt": False}  # this exact result was already reported (retry/resume)
            step = self.begin_step(session, proposal, "verification_report", "Verification report")
            comparison = VerificationComparison.model_validate(
                proposal.structured_output.get("verification", {})
            )
            self.complete_step(
                session,
                step,
                f"Verification {comparison.state}; awaiting human approval of patch "
                f"{(proposal.patch_hash or '')[:12]}.",
                {"state": comparison.state, "verification_hash": proposal.verification_hash},
            )
            self.audit(
                session, proposal, "awaiting_approval", "remediation", proposal.id, "success"
            )
            session.commit()
            self.emit(proposal, "remediation.awaiting_approval")
            return {"halt": False}

    # -- approval binding -------------------------------------------------------------------
    def current_binding(self, session: Session, proposal: RemediationProposal) -> dict[str, str]:
        from opspilot_api.remediation.binding import current_binding

        return current_binding(session, proposal)

    def recompute_hash(self, session: Session, proposal: RemediationProposal) -> str:
        return recompute_verification_hash(session, proposal)

    def _existing_run_any(
        self, session: Session, proposal: RemediationProposal
    ) -> SandboxRun | None:
        return latest_run(session, proposal, "patched")

    def invalidate_approval(
        self, session: Session, proposal: RemediationProposal, reasons: list[str]
    ) -> None:
        """Approval no longer matches: return to policy/verification review. Publication stops."""
        self.audit(
            session,
            proposal,
            "approval_invalidated",
            "remediation",
            proposal.id,
            "invalidated",
            {"reasons": reasons},
        )
        self.transition(
            session,
            proposal,
            S.POLICY_CHECK,
            verification_hash=None,
            verification_state=None,
            failure_code=None,
            failure_message=None,
        )
        session.commit()
        self.emit(proposal, "remediation.approval_invalidated", {"reasons": reasons})

    # -- step 15: publish -------------------------------------------------------------------
    def publish(self, workspace_id: UUID, remediation_id: UUID) -> dict[str, Any]:
        """Create the fix branch, commit, and pull request for the exact approved artifact."""
        with Session(self.engine) as session:
            proposal = self.load(session, workspace_id, remediation_id)
            if proposal.status == S.PULL_REQUEST_CREATED.value:
                return {"outcome": "already_published"}
            if proposal.status not in {S.PUBLISHING.value, S.APPROVED.value}:
                return {"outcome": "not_publishable"}
            if proposal.status == S.APPROVED.value:
                self.transition(session, proposal, S.PUBLISHING)
            approval = self._valid_approval(session, proposal)
            scope = self.scope(session, proposal)
            self._preflight(session, proposal, scope, approval)
            pr_row = self._pr_row(session, proposal, approval)
            if pr_row.status == PullRequestStatus.CREATED.value and pr_row.pull_request_url:
                self.transition(session, proposal, S.PULL_REQUEST_CREATED, completed_at=utcnow())
                return {"outcome": "already_published"}
            self.emit(proposal, "remediation.publish_started")
            self.audit(
                session,
                proposal,
                "publication_attempt",
                "pull_request",
                pr_row.id,
                "started",
                {"branch": pr_row.branch_name},
            )
            session.commit()
            try:
                plan = self._plan(session, proposal, scope, approval, pr_row.branch_name)
                result = publish_to_provider(
                    self.write_provider,
                    plan,
                    lambda stage, data: self._publish_progress(
                        session, proposal, pr_row, stage, data
                    ),
                )
            except ProviderError as exc:
                return self._publish_failure(session, proposal, pr_row, exc)
            pr_row.commit_sha = result.commit_sha
            pr_row.provider_pull_request_id = result.pull_request.id
            pr_row.pull_request_number = result.pull_request.number
            pr_row.pull_request_url = result.pull_request.url
            pr_row.status = PullRequestStatus.CREATED.value
            pr_row.failure_code = None
            pr_row.failure_message = None
            pr_row.updated_at = utcnow()
            self.audit(
                session,
                proposal,
                "pull_request_created",
                "pull_request",
                pr_row.id,
                "success",
                {"number": result.pull_request.number},
            )
            session.commit()
            self.transition(session, proposal, S.PULL_REQUEST_CREATED, completed_at=utcnow())
            self.emit(
                proposal,
                "remediation.pull_request_created",
                {
                    "pull_request_number": result.pull_request.number,
                    "pull_request_url": result.pull_request.url,
                },
            )
            return {"outcome": "created", "number": result.pull_request.number}

    def _valid_approval(
        self, session: Session, proposal: RemediationProposal
    ) -> RemediationApproval:
        approval = latest_approval(session, proposal)
        if approval is None:
            raise ApprovalInvalidError(["no_approval"])
        return approval

    def check_binding(
        self, session: Session, proposal: RemediationProposal, approval: RemediationApproval
    ) -> list[str]:
        return binding_check(session, proposal, approval)

    def _preflight(
        self,
        session: Session,
        proposal: RemediationProposal,
        scope: RepositoryScope,
        approval: RemediationApproval,
    ) -> None:
        """Every condition must hold now; any doubt stops publication (fail closed)."""
        full_name = scope.repository.full_name
        # Re-authorize at execution time: the requester must still hold publish permission and the
        # approver must still be an authorized, active reviewer (revocation voids the approval).
        if not has_permission_sync(
            session,
            proposal.workspace_id,
            proposal.publish_requested_by_user_id,
            Permission.REMEDIATIONS_PUBLISH,
        ):
            self._publish_stop(session, proposal, "publisher_not_authorized")
        approver_ok = False
        try:
            approver_ok = has_permission_sync(
                session,
                proposal.workspace_id,
                UUID(str(approval.actor_id)),
                Permission.REMEDIATIONS_REVIEW,
            )
        except ValueError:
            approver_ok = False
        if not approver_ok:
            self.invalidate_approval(session, proposal, ["approver_not_authorized"])
            raise ApprovalInvalidError(["approver_not_authorized"])
        if not write_enabled_for(self.settings, proposal.workspace_id, full_name):
            self._publish_stop(session, proposal, "write_not_enabled")
        if scope.repository.status != "active" or scope.repository.is_archived:
            self._publish_stop(session, proposal, "repository_inactive")
        problems = self.check_binding(session, proposal, approval)
        if problems:
            self.invalidate_approval(session, proposal, problems)
            raise ApprovalInvalidError(problems)
        revision = self.current_revision(session, proposal)
        evaluation = evaluate_patch(revision.patch_text, self.policy_context(proposal, scope))
        if evaluation.blocked or evaluation.version != approval.approval_context.get(
            "policy_version"
        ):
            self.invalidate_approval(session, proposal, ["policy_decision"])
            raise ApprovalInvalidError(["policy_decision"])
        try:
            self._ensure_base_current(scope, proposal)
        except RemediationFailureError as exc:
            if exc.code == "base_commit_stale":
                self.audit(
                    session,
                    proposal,
                    "approval_invalidated",
                    "remediation",
                    proposal.id,
                    "invalidated",
                    {"reasons": ["base_commit_stale"]},
                )
                self._mark_stale(session, proposal, str(exc))
                raise ApprovalInvalidError(["base_commit_stale"]) from exc
            self._publish_stop(session, proposal, exc.code)

    def _publish_stop(self, session: Session, proposal: RemediationProposal, code: str) -> None:
        self.audit(
            session,
            proposal,
            "publication_blocked",
            "remediation",
            proposal.id,
            "failure",
            {"code": code},
        )
        self.transition(
            session,
            proposal,
            S.FAILED,
            failed_at=utcnow(),
            failure_code=code,
            failure_message=f"publication blocked: {code}",
        )
        session.commit()
        self.emit(proposal, "remediation.failed", {"failure_code": code})
        raise RemediationFailureError(code, f"publication blocked: {code}")

    def _pr_row(
        self, session: Session, proposal: RemediationProposal, approval: RemediationApproval
    ) -> FixPullRequest:
        existing = session.scalar(
            select(FixPullRequest).where(
                FixPullRequest.approval_id == approval.id,
                FixPullRequest.workspace_id == proposal.workspace_id,
            )
        )
        if existing is not None:
            if existing.status == PullRequestStatus.FAILED.value:
                existing.status = PullRequestStatus.CREATING.value
                existing.updated_at = utcnow()
                session.commit()
            return existing
        revision = self.current_revision(session, proposal)
        row = FixPullRequest(
            id=uuid4(),
            workspace_id=proposal.workspace_id,
            remediation_id=proposal.id,
            revision_id=revision.id,
            approval_id=approval.id,
            repository_id=proposal.repository_id,
            base_commit_sha=proposal.base_commit_sha,
            patch_hash=revision.patch_hash,
            branch_name=branch_name_for(
                self.settings.REMEDIATION_BRANCH_PREFIX, proposal.id, revision.patch_hash
            ),
            status=PullRequestStatus.CREATING.value,
        )
        session.add(row)
        session.commit()
        return row

    def _plan(
        self,
        session: Session,
        proposal: RemediationProposal,
        scope: RepositoryScope,
        approval: RemediationApproval,
        branch: str,
    ) -> PublishPlan:
        revision = self.current_revision(session, proposal)
        checkout = self.checkout(proposal, scope)
        baseline = session.scalar(
            select(SandboxRun)
            .where(
                SandboxRun.remediation_id == proposal.id,
                SandboxRun.run_kind == "baseline",
                SandboxRun.revision_id == revision.id,
            )
            .order_by(SandboxRun.created_at.desc())
        )
        if (
            baseline is not None
            and baseline.base_tree_hash
            and baseline.base_tree_hash != checkout.tree_hash
        ):
            raise RemediationFailureError(
                "base_tree_changed", "base tree differs from verification"
            )
        diffs = parse_unified_diff(revision.patch_text)
        changes = apply_patch(
            diffs,
            lambda path: (
                None if path not in checkout.files else checkout.files[path].decode("utf-8")
            ),
        )
        investigation = session.get(Investigation, proposal.investigation_id)
        evidence = [
            {"id": str(item.id), "title": item.title}
            for item in self._evidence(session, proposal)
            if str(item.id) in set(proposal.structured_output.get("evidence_ids", []))
        ]
        comparison = VerificationComparison.model_validate(
            proposal.structured_output["verification"]
        )
        checks = self._check_summary(session, proposal)
        title, body = build_pull_request_content(
            remediation_id=proposal.id,
            incident_id=proposal.incident_id,
            investigation_id=proposal.investigation_id,
            title=proposal.title,
            root_cause=(investigation.root_cause if investigation else "") or "",
            evidence=evidence,
            base_commit_sha=proposal.base_commit_sha,
            patch_hash=revision.patch_hash,
            files=[{"path": d.path, "added": d.added, "deleted": d.deleted} for d in diffs],
            risk_level=proposal.risk_level,
            policy_decision=proposal.policy_decision or "",
            policy_version=proposal.policy_version or "",
            checks=checks,
            verification_state=comparison.state,
            limitations=list(proposal.structured_output.get("known_limitations", [])),
            rollback_guidance=str(proposal.structured_output.get("rollback_guidance", "")),
            approver=approval.actor_id,
        )
        return PublishPlan(
            installation_id=scope.installation_provider_id,
            owner=scope.repository.owner,
            name=scope.repository.name,
            default_branch=scope.default_branch,
            base_sha=proposal.base_commit_sha,
            branch=branch,
            branch_prefix=self.settings.REMEDIATION_BRANCH_PREFIX,
            changes=changes,
            patch_hash=revision.patch_hash,
            title=title,
            body=body,
        )

    def _check_summary(
        self, session: Session, proposal: RemediationProposal
    ) -> list[dict[str, Any]]:
        baseline = session.scalar(
            select(SandboxRun)
            .where(
                SandboxRun.remediation_id == proposal.id,
                SandboxRun.run_kind == "baseline",
                SandboxRun.revision_id == proposal.current_revision_id,
            )
            .order_by(SandboxRun.created_at.desc())
        )
        patched = self._existing_run_any(session, proposal)
        names: dict[str, dict[str, Any]] = {}
        for label, run in (("baseline", baseline), ("patched", patched)):
            if run is None:
                continue
            for check in session.scalars(
                select(VerificationCheck).where(VerificationCheck.sandbox_run_id == run.id)
            ):
                names.setdefault(check.command_id, {"name": check.display_name})[label] = (
                    check.status
                )
        return list(names.values())

    def _publish_progress(
        self,
        session: Session,
        proposal: RemediationProposal,
        row: FixPullRequest,
        stage: str,
        data: dict[str, Any],
    ) -> None:
        if stage == "commit":
            row.commit_sha = str(data.get("commit_sha"))
        row.updated_at = utcnow()
        self.audit(session, proposal, f"github_{stage}", "pull_request", row.id, "success", data)
        session.commit()

    def _publish_failure(
        self,
        session: Session,
        proposal: RemediationProposal,
        row: FixPullRequest,
        exc: ProviderError,
    ) -> dict[str, Any]:
        row.status = PullRequestStatus.FAILED.value
        row.failure_code = exc.code
        row.failure_message = sanitize_error(exc)[:500]
        row.updated_at = utcnow()
        self.audit(
            session,
            proposal,
            "publication_attempt",
            "pull_request",
            row.id,
            "failure",
            {"code": exc.code},
        )
        session.commit()
        if exc.retryable:
            # Transient: return to approved so the same approval can be published again.
            self.transition(session, proposal, S.APPROVED)
            self.emit(proposal, "remediation.failed", {"failure_code": exc.code, "retryable": True})
            return {"outcome": "retryable_failure", "code": exc.code}
        self.transition(
            session,
            proposal,
            S.FAILED,
            failed_at=utcnow(),
            failure_code=exc.code,
            failure_message=sanitize_error(exc)[:500],
        )
        self.emit(proposal, "remediation.failed", {"failure_code": exc.code})
        return {"outcome": "failed", "code": exc.code}

    def purge(self, remediation_id: UUID) -> None:
        for key in [k for k in self._checkouts if k[0] == remediation_id]:
            self._checkouts.pop(key, None)


def approval_expiry(settings: Settings, now: datetime) -> datetime:
    return now + timedelta(seconds=settings.REMEDIATION_APPROVAL_TTL_SECONDS)


__all__ = [
    "ApprovalContext",
    "ApprovalInvalidError",
    "RemediationCancelledError",
    "RemediationFailureError",
    "RemediationPipeline",
    "approval_expiry",
]
