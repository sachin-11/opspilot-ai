"""Baseline-versus-patched verification inside sandboxes (pure orchestration, no database)."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from opspilot_api.incidents.service import utcnow
from opspilot_api.remediation.approval import canonical_hash
from opspilot_api.remediation.checkout import Checkout, verify_checkout
from opspilot_api.remediation.commands import (
    COMMANDS,
    CommandError,
    ResolvedCommand,
    extract_failures,
    resolve_command,
)
from opspilot_api.remediation.diffparse import DiffError, FileDiff, apply_patch
from opspilot_api.remediation.models import CheckStatus, CheckType, SandboxStatus
from opspilot_api.remediation.sandbox import (
    CommandResult,
    Sandbox,
    SandboxError,
    SandboxLimits,
    SandboxProvider,
)
from opspilot_api.remediation.schemas import VerificationComparison

logger = logging.getLogger(__name__)
TEST_TYPES = {CheckType.UNIT_TEST.value, CheckType.TARGETED_TEST.value}
FAILING = {CheckStatus.FAILED, CheckStatus.TIMED_OUT, CheckStatus.ERROR}


@dataclass
class CheckOutcome:
    check_type: str
    command_id: str
    display_name: str
    status: CheckStatus
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    failing_tests: list[str] = field(default_factory=list)


@dataclass
class RunOutcome:
    kind: str
    status: SandboxStatus
    checks: list[CheckOutcome]
    started_at: datetime
    completed_at: datetime
    tree_hash: str | None = None
    cleanup_verified: bool = False
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass
class VerificationResult:
    baseline: RunOutcome
    patched: RunOutcome
    comparison: VerificationComparison
    verification_hash: str


def _classify(result: CommandResult) -> CheckStatus:
    if result.timed_out:
        return CheckStatus.TIMED_OUT
    return CheckStatus.PASSED if result.exit_code == 0 else CheckStatus.FAILED


def _run_command(sandbox: Sandbox, command: ResolvedCommand) -> CheckOutcome:
    result = sandbox.run(command)
    status = _classify(result)
    return CheckOutcome(
        check_type=command.spec.check_type.value,
        command_id=command.command_id,
        display_name=command.spec.display_name,
        status=status,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        failing_tests=[]
        if status is CheckStatus.PASSED
        else extract_failures(command.command_id, result.stdout, result.stderr),
    )


def _skipped(command_id: str, reason: str) -> CheckOutcome:
    spec = COMMANDS[command_id]
    return CheckOutcome(
        check_type=spec.check_type.value,
        command_id=command_id,
        display_name=spec.display_name,
        status=CheckStatus.SKIPPED,
        stderr=reason,
    )


def execute_checks(
    sandbox: Sandbox,
    command_ids: list[str],
    path_prefix: str | None,
    targeted_paths: list[str] | None = None,
) -> list[CheckOutcome]:
    files = sandbox.list_files()
    outcomes: list[CheckOutcome] = []
    for command_id in command_ids:
        try:
            resolved = resolve_command(command_id, files, path_prefix)
        except CommandError as exc:
            outcomes.append(_skipped(command_id, str(exc)))
            continue
        if resolved is None:
            outcomes.append(_skipped(command_id, "required project marker files are absent"))
            continue
        outcomes.append(_run_command(sandbox, resolved))
    if targeted_paths:
        targeted = resolve_command("python.pytest_targeted", files, path_prefix, targeted_paths)
        if targeted is not None:
            outcomes.append(_run_command(sandbox, targeted))
    return outcomes


def _overall(checks: list[CheckOutcome]) -> SandboxStatus:
    if any(item.status is CheckStatus.TIMED_OUT for item in checks):
        return SandboxStatus.TIMED_OUT
    if any(item.status in {CheckStatus.FAILED, CheckStatus.ERROR} for item in checks):
        return SandboxStatus.FAILED
    return SandboxStatus.PASSED


def _failed_run(
    kind: str, started: datetime, code: str, message: str, checks: list[CheckOutcome], cleaned: bool
) -> RunOutcome:
    return RunOutcome(
        kind,
        SandboxStatus.FAILED,
        checks,
        started,
        utcnow(),
        cleanup_verified=cleaned,
        failure_code=code,
        failure_message=message[:500],
    )


def run_baseline(
    provider: SandboxProvider,
    limits: SandboxLimits,
    checkout: Checkout,
    command_ids: list[str],
    path_prefix: str | None,
) -> RunOutcome:
    started = utcnow()
    sandbox = provider.create(limits)
    checks: list[CheckOutcome] = []
    failure: SandboxError | None = None
    try:
        verify_checkout(checkout)
        sandbox.prepare(checkout.files)
        checks = execute_checks(sandbox, command_ids, path_prefix)
    except SandboxError as exc:
        failure = exc
    finally:
        cleaned = sandbox.cleanup()
    if failure is not None:
        return _failed_run("baseline", started, failure.code, str(failure), checks, cleaned)
    return RunOutcome(
        "baseline",
        _overall(checks),
        checks,
        started,
        utcnow(),
        tree_hash=checkout.tree_hash,
        cleanup_verified=cleaned,
    )


def _patch_check(status: CheckStatus, message: str = "") -> CheckOutcome:
    return CheckOutcome(
        CheckType.PATCH_APPLY.value,
        "patch.apply",
        "apply patch",
        status,
        exit_code=0 if status is CheckStatus.PASSED else 1,
        stderr=message[:500],
    )


def run_patched(
    provider: SandboxProvider,
    limits: SandboxLimits,
    checkout: Checkout,
    diffs: list[FileDiff],
    command_ids: list[str],
    path_prefix: str | None,
) -> RunOutcome:
    started = utcnow()
    sandbox = provider.create(limits)
    checks: list[CheckOutcome] = []
    failure: SandboxError | None = None
    apply_error: str | None = None
    try:
        verify_checkout(checkout)
        sandbox.prepare(checkout.files)
        try:
            changes = apply_patch(diffs, sandbox.read_text)
        except (DiffError, UnicodeDecodeError) as exc:
            apply_error = str(exc)
            checks.append(_patch_check(CheckStatus.FAILED, apply_error))
        else:
            sandbox.write_changes(changes)
            checks.append(_patch_check(CheckStatus.PASSED))
            targeted = [
                diff.path
                for diff in diffs
                if diff.kind != "deleted"
                and diff.path.endswith(".py")
                and "test" in diff.path.lower()
            ]
            checks.extend(execute_checks(sandbox, command_ids, path_prefix, targeted or None))
    except SandboxError as exc:
        failure = exc
    finally:
        cleaned = sandbox.cleanup()
    if failure is not None:
        return _failed_run("patched", started, failure.code, str(failure), checks, cleaned)
    if apply_error is not None:
        outcome = _failed_run(
            "patched", started, "patch_apply_failed", apply_error, checks, cleaned
        )
        outcome.tree_hash = checkout.tree_hash
        return outcome
    return RunOutcome(
        "patched",
        _overall(checks),
        checks,
        started,
        utcnow(),
        tree_hash=checkout.tree_hash,
        cleanup_verified=cleaned,
    )


def _summary(checks: list[CheckOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "check_type": item.check_type,
            "command_id": item.command_id,
            "status": item.status.value,
            "exit_code": item.exit_code,
            "failing": sorted(item.failing_tests),
        }
        for item in sorted(checks, key=lambda c: (c.check_type, c.command_id))
    ]


def verification_hash(baseline: RunOutcome, patched: RunOutcome) -> str:
    """Hash of check outcomes only (no durations or output) so equal results hash equally."""
    return canonical_hash(
        {
            "baseline": _summary(baseline.checks),
            "patched": _summary(patched.checks),
            "baseline_status": baseline.status.value,
            "patched_status": patched.status.value,
        }
    )


def _early_failure(notes: list[str]) -> VerificationComparison:
    return VerificationComparison(
        state="failed",
        required_executed=False,
        new_failures=[],
        existing_failures=[],
        fixed=[],
        skipped=[],
        notes=notes,
    )


def compare_runs(baseline: RunOutcome, patched: RunOutcome) -> VerificationComparison:
    """Separate pre-existing failures from failures the patch introduced."""
    if patched.failure_code == "patch_apply_failed":
        result = _early_failure(["The patch did not apply to the base commit."])
        result.new_failures = [{"command_id": "patch.apply", "tests": ["patch_apply_failed"]}]
        return result
    if baseline.failure_code or patched.failure_code:
        detail = patched.failure_message or baseline.failure_message or "sandbox failure"
        return _early_failure(["A sandbox run failed before all checks completed.", detail])
    new_failures: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    fixed: list[str] = []
    skipped: list[dict[str, str]] = []
    notes: list[str] = []
    before_by_command = {item.command_id: item for item in baseline.checks}
    test_executed = False
    for check in patched.checks:
        if check.check_type == CheckType.PATCH_APPLY.value:
            continue
        before = before_by_command.get(check.command_id)
        if check.status is CheckStatus.SKIPPED:
            skipped.append({"command_id": check.command_id, "reason": check.stderr})
            continue
        if check.check_type in TEST_TYPES:
            test_executed = True
        if check.status is CheckStatus.PASSED:
            if before is not None and before.status in FAILING:
                fixed.extend(before.failing_tests or [check.command_id])
            continue
        now_failing = set(check.failing_tests)
        before_failing = set(before.failing_tests) if before and before.status in FAILING else set()
        introduced = sorted(now_failing - before_failing)
        carried = sorted(now_failing & before_failing)
        if introduced:
            new_failures.append({"command_id": check.command_id, "tests": introduced})
        if carried:
            existing.append({"command_id": check.command_id, "tests": carried})
    if not test_executed:
        notes.append("No test check executed; the patch is unverified.")
    if existing:
        notes.append("Failures present before the patch remain and are not caused by it.")
    if skipped:
        notes.append("Some selected checks did not run (partial verification).")
    state: Literal["passed", "partial", "failed", "not_run"]
    if new_failures:
        state = "failed"
    elif not test_executed:
        state = "not_run"
    elif existing or skipped:
        state = "partial"
    else:
        state = "passed"
    return VerificationComparison(
        state=cast(Any, state),
        required_executed=test_executed,
        new_failures=new_failures,
        existing_failures=existing,
        fixed=sorted(set(fixed)),
        skipped=skipped,
        notes=notes,
    )


def run_verification(
    provider: SandboxProvider,
    limits: SandboxLimits,
    checkout: Checkout,
    diffs: list[FileDiff],
    command_ids: list[str],
    path_prefix: str | None,
) -> VerificationResult:
    baseline = run_baseline(provider, limits, checkout, command_ids, path_prefix)
    patched = run_patched(provider, limits, checkout, diffs, command_ids, path_prefix)
    comparison = compare_runs(baseline, patched)
    return VerificationResult(baseline, patched, comparison, verification_hash(baseline, patched))
