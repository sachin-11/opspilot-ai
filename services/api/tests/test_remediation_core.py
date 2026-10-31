"""Command registry, sandbox construction, verification comparison, approval binding, writers."""

import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from opspilot_api.incidents.service import utcnow
from opspilot_api.remediation import writer as writer_module
from opspilot_api.remediation.approval import ApprovalContext, binding_mismatches, canonical_hash
from opspilot_api.remediation.commands import (
    COMMANDS,
    CommandError,
    default_command_ids,
    extract_failures,
    resolve_command,
)
from opspilot_api.remediation.generator import FakeRemediationGenerator, make_diff
from opspilot_api.remediation.models import CheckStatus, RemediationStatus, SandboxStatus
from opspilot_api.remediation.sandbox import (
    CommandResult,
    DockerSandbox,
    FakeSandbox,
    FakeSandboxProvider,
    SandboxError,
    SandboxLimits,
    bounded_run,
    build_docker_args,
    purge_expired_workspaces,
    safe_join,
)
from opspilot_api.remediation.schemas import EvidenceContext, GenerationContext
from opspilot_api.remediation.state import RETRYABLE, TERMINAL, TRANSITIONS, can_transition
from opspilot_api.remediation.verification import (
    CheckOutcome,
    RunOutcome,
    compare_runs,
    verification_hash,
)
from opspilot_api.remediation.writer import (
    BranchConflictError,
    FixtureWriteProvider,
    GitHubWriteProvider,
    UnsafeBranchError,
    WritePermissionError,
    assert_fix_branch,
    write_enabled_for,
)
from opspilot_api.repositories.fixture_provider import FixtureProvider
from tests.conftest import make_settings

LIMITS = SandboxLimits(cpu=1, memory_mb=128, disk_mb=32, pids=32, timeout_seconds=10)


# ---- state machine --------------------------------------------------------------------------
def test_every_status_has_an_explicit_transition_entry() -> None:
    assert set(TRANSITIONS) == set(RemediationStatus)
    assert {s for s in RemediationStatus if s in TERMINAL} >= {
        RemediationStatus.REJECTED,
        RemediationStatus.PULL_REQUEST_CREATED,
        RemediationStatus.POLICY_BLOCKED,
    }
    assert RETRYABLE <= TERMINAL


def test_invalid_transitions_are_refused() -> None:
    assert can_transition("drafting", RemediationStatus.POLICY_CHECK)
    assert can_transition("awaiting_approval", RemediationStatus.APPROVED)
    assert not can_transition("drafting", RemediationStatus.APPROVED)
    assert not can_transition("awaiting_approval", RemediationStatus.PUBLISHING)
    assert not can_transition("rejected", RemediationStatus.APPROVED)
    assert not can_transition("pull_request_created", RemediationStatus.DRAFTING)
    assert not can_transition("policy_blocked", RemediationStatus.SANDBOX_QUEUED)
    assert not can_transition("verification_failed", RemediationStatus.AWAITING_APPROVAL)


# ---- command registry -----------------------------------------------------------------------
def test_registry_commands_are_argv_vectors_without_shell_syntax() -> None:
    for spec in COMMANDS.values():
        assert isinstance(spec.argv, tuple)
        assert all(
            isinstance(part, str) and ";" not in part and "|" not in part for part in spec.argv
        )
        assert spec.timeout_seconds > 0 and spec.max_output_bytes > 0


def test_unregistered_or_shell_commands_never_resolve() -> None:
    files = {"pyproject.toml", "tests/test_a.py"}
    for bad in ("rm -rf /", "python.pytest && curl x", "bash", ""):
        with pytest.raises(CommandError):
            resolve_command(bad, files, None)


def test_markers_gate_commands_and_bounded_args_are_validated() -> None:
    assert resolve_command("python.pytest", {"tests/test_a.py"}, None) is not None
    assert resolve_command("python.pytest", {"README.md"}, None) is None
    assert resolve_command("node.test", {"src/a.ts"}, None) is None
    assert resolve_command("python.pytest_targeted", {"tests/a.py"}, None) is None
    ok = resolve_command("python.pytest_targeted", {"tests/a.py"}, None, ["tests/test_a.py"])
    assert ok is not None and ok.argv[-1] == "tests/test_a.py"
    for unsafe in (["--rootdir=/"], ["../x.py"], ["/etc/passwd"], ["a b.py"], ["x;rm.py"]):
        with pytest.raises(CommandError):
            resolve_command("python.pytest_targeted", {"tests/a.py"}, None, unsafe)
    with pytest.raises(CommandError):
        resolve_command("python.mypy", {"pyproject.toml"}, None, ["x.py"])


def test_default_commands_follow_languages_and_failures_are_extracted() -> None:
    assert default_command_ids({"a.py"}) == ["python.pytest"]
    assert default_command_ids({"a.ts", "b.py"}) == ["python.pytest", "node.test"]
    assert default_command_ids({"README.md"}) == []
    out = "FAILED tests/test_a.py::test_x - assert 1 == 2\nFAILED tests/test_a.py::test_y\n"
    assert extract_failures("python.pytest", out, "") == [
        "tests/test_a.py::test_x",
        "tests/test_a.py::test_y",
    ]
    assert extract_failures("node.test", "not ok 1 - adds numbers\nok 2 - fine\n", "") == [
        "adds numbers"
    ]
    signature = extract_failures("python.ruff_check", "E501 line too long 12.3s", "")
    assert signature[0].startswith("signature:")
    assert signature == extract_failures("python.ruff_check", "E501 line too long 99.9s", "")


# ---- docker argument construction (no daemon needed) ------------------------------------------
def test_docker_args_are_hardened_and_fixed(tmp_path: Path) -> None:
    command = resolve_command("python.pytest", {"tests/test_a.py"}, None)
    assert command is not None
    args = build_docker_args("opspilot-sandbox:latest", "sbx-1", tmp_path, LIMITS, command)
    joined = " ".join(args)
    assert args[:3] == ["docker", "run", "--rm"]
    for flag in (
        "--read-only",
        "--cap-drop",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
    ):
        assert flag in args
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--user") + 1] == "10001:10001"
    assert args[args.index("--memory") + 1] == "128m"
    assert "--privileged" not in args
    assert "docker.sock" not in joined
    assert joined.count("--mount") == 1 and joined.count("target=/workspace") == 1
    assert "-v" not in args and "--volume" not in args
    assert "--env-file" not in args and "--add-host" not in args
    env_keys = {args[i + 1].split("=")[0] for i, a in enumerate(args) if a == "--env"}
    assert env_keys <= {
        "PATH",
        "HOME",
        "LANG",
        "CI",
        "NO_COLOR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
    }
    assert not any(
        word in key.upper() for key in env_keys for word in ("SECRET", "TOKEN", "KEY", "PASSWORD")
    )


def test_network_is_never_enabled_from_settings() -> None:
    assert SandboxLimits.from_settings(make_settings()).network_enabled is False


# ---- sandboxes ---------------------------------------------------------------------------------
def test_safe_join_rejects_escapes(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a/b.py").parent.parent == tmp_path.resolve()
    for bad in ("../x", "/abs", "a/../../x", "", "\\x"):
        with pytest.raises(SandboxError):
            safe_join(tmp_path, bad)


def test_bounded_run_truncates_output_and_enforces_timeout() -> None:
    code, out, _err, timed_out, truncated = bounded_run(
        [sys.executable, "-c", "print('x' * 100000)"], 20, 1000
    )
    assert code == 0 and truncated and not timed_out and len(out) <= 1001
    code, _out, _err, timed_out, _ = bounded_run(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1, 1000
    )
    assert timed_out and code is None


def test_docker_sandbox_workspace_is_dedicated_and_cleaned(tmp_path: Path) -> None:
    settings = make_settings(REMEDIATION_WORKSPACE_ROOT=str(tmp_path))
    sandbox = DockerSandbox(settings, LIMITS)
    sandbox.prepare({"src/a.py": b"x = 1\n"})
    assert sandbox.workspace.parent == tmp_path.resolve()
    assert sandbox.read_text("src/a.py") == "x = 1\n"
    sandbox.write_changes({"src/a.py": "x = 2\n", "src/new.py": "y = 1\n"})
    assert sandbox.list_files() == {"src/a.py", "src/new.py"}
    with pytest.raises(SandboxError):
        sandbox.prepare({"../escape.py": b"x"})
    assert sandbox.cleanup() is True and sandbox.cleaned
    assert not any(tmp_path.iterdir())


def test_purge_removes_only_expired_sandbox_workspaces(tmp_path: Path) -> None:
    old = tmp_path / "opspilot-sbx-old"
    fresh = tmp_path / "opspilot-sbx-fresh"
    keep = tmp_path / "other-dir"
    for directory in (old, fresh, keep):
        directory.mkdir()
    os.utime(old, (1, 1))
    os.utime(keep, (1, 1))
    assert purge_expired_workspaces(tmp_path, 3600) == 1
    assert fresh.exists() and keep.exists() and not old.exists()


def test_fake_sandbox_cleanup_blocks_reuse() -> None:
    sandbox = FakeSandbox(LIMITS)
    sandbox.prepare({"a.py": b"x"})
    command = resolve_command("python.pytest", {"tests/x.py"}, None)
    assert command is not None
    assert sandbox.run(command).exit_code == 0
    assert sandbox.cleanup() and sandbox.cleaned
    with pytest.raises(SandboxError):
        sandbox.run(command)


# ---- verification comparison -----------------------------------------------------------------
def check(
    command_id: str,
    status: CheckStatus,
    failing: list[str] | None = None,
    check_type: str = "unit_test",
) -> CheckOutcome:
    return CheckOutcome(
        check_type,
        command_id,
        command_id,
        status,
        0 if status is CheckStatus.PASSED else 1,
        failing_tests=failing or [],
    )


def run(kind: str, *checks: CheckOutcome, failure: str | None = None) -> RunOutcome:
    now = utcnow()
    overall = SandboxStatus.PASSED
    return RunOutcome(kind, overall, list(checks), now, now, failure_code=failure)


def test_clean_pass_requires_executed_passing_tests() -> None:
    result = compare_runs(
        run("baseline", check("python.pytest", CheckStatus.FAILED, ["t::a"])),
        run("patched", check("python.pytest", CheckStatus.PASSED)),
    )
    assert result.state == "passed" and result.fixed == ["t::a"] and not result.new_failures


def test_preexisting_failure_is_not_blamed_on_the_patch() -> None:
    result = compare_runs(
        run("baseline", check("python.pytest", CheckStatus.FAILED, ["t::old"])),
        run("patched", check("python.pytest", CheckStatus.FAILED, ["t::old"])),
    )
    assert result.state == "partial"
    assert not result.new_failures
    assert result.existing_failures == [{"command_id": "python.pytest", "tests": ["t::old"]}]


def test_new_regression_blocks_verification() -> None:
    result = compare_runs(
        run("baseline", check("python.pytest", CheckStatus.FAILED, ["t::old"])),
        run("patched", check("python.pytest", CheckStatus.FAILED, ["t::old", "t::new"])),
    )
    assert result.state == "failed"
    assert result.new_failures == [{"command_id": "python.pytest", "tests": ["t::new"]}]
    passing_before = compare_runs(
        run("baseline", check("python.pytest", CheckStatus.PASSED)),
        run("patched", check("python.pytest", CheckStatus.FAILED, ["t::x"])),
    )
    assert passing_before.state == "failed"


def test_unexecuted_required_checks_are_never_reported_as_verified() -> None:
    skipped = check("python.pytest", CheckStatus.SKIPPED)
    result = compare_runs(run("baseline", skipped), run("patched", skipped))
    assert result.state == "not_run" and not result.required_executed
    only_lint = check("python.ruff_check", CheckStatus.PASSED, check_type="lint")
    assert compare_runs(run("baseline", only_lint), run("patched", only_lint)).state == "not_run"


def test_skipped_secondary_check_makes_verification_partial() -> None:
    patched = run(
        "patched",
        check("python.pytest", CheckStatus.PASSED),
        check("python.mypy", CheckStatus.SKIPPED, check_type="typecheck"),
    )
    result = compare_runs(run("baseline", check("python.pytest", CheckStatus.PASSED)), patched)
    assert result.state == "partial" and result.skipped[0]["command_id"] == "python.mypy"


def test_patch_apply_and_sandbox_failures_fail_verification() -> None:
    applied = run("patched", failure="patch_apply_failed")
    assert compare_runs(run("baseline"), applied).state == "failed"
    assert (
        compare_runs(run("baseline", failure="sandbox_unavailable"), run("patched")).state
        == "failed"
    )


def test_verification_hash_ignores_timing_but_tracks_outcomes() -> None:
    base = run("baseline", check("python.pytest", CheckStatus.PASSED))
    patched = run("patched", check("python.pytest", CheckStatus.PASSED))
    first = verification_hash(base, patched)
    patched.checks[0].duration_ms = 999
    patched.checks[0].stdout = "noise"
    assert verification_hash(base, patched) == first
    patched.checks[0].status = CheckStatus.FAILED
    assert verification_hash(base, patched) != first


# ---- approval binding ------------------------------------------------------------------------
def context(**overrides: object) -> ApprovalContext:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "repository_id": uuid4(),
        "base_commit_sha": "a" * 40,
        "revision_id": uuid4(),
        "patch_hash": "b" * 64,
        "policy_version": "v1",
        "verification_hash": "c" * 64,
        "expires_at": utcnow() + timedelta(hours=1),
    }
    values.update(overrides)
    return ApprovalContext(**values)  # type: ignore[arg-type]


def mismatches(ctx: ApprovalContext, **changes: str) -> list[str]:
    current = {k: v for k, v in ctx.as_dict().items() if k != "expires_at"}
    current.update(changes)
    return binding_mismatches(ctx.as_dict(), ctx.digest, ctx.expires_at, current, utcnow())


def test_binding_accepts_the_exact_artifact() -> None:
    assert mismatches(context()) == []


@pytest.mark.parametrize(
    "field",
    [
        "workspace_id",
        "repository_id",
        "base_commit_sha",
        "revision_id",
        "patch_hash",
        "policy_version",
        "verification_hash",
    ],
)
def test_every_bound_value_invalidates_the_approval(field: str) -> None:
    assert field in mismatches(context(), **{field: "changed"})


def test_expired_or_tampered_approvals_are_invalid() -> None:
    expired = context(expires_at=utcnow() - timedelta(seconds=1))
    assert "expired" in mismatches(expired)
    ctx = context()
    tampered = ctx.as_dict() | {"patch_hash": "d" * 64}
    problems = binding_mismatches(
        tampered, ctx.digest, ctx.expires_at, {k: v for k, v in tampered.items()}, utcnow()
    )
    assert "approval_record_tampered" in problems
    assert binding_mismatches({}, "", None, {}, utcnow())


def test_canonical_hash_is_key_order_independent() -> None:
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


# ---- write provider --------------------------------------------------------------------------
def fixture_pair(**kwargs: object) -> tuple[FixtureProvider, FixtureWriteProvider, int, str]:
    provider = FixtureProvider()
    installation = provider.data["installations"][0]
    repo = installation["repositories"][0]
    head = provider.get_branch(
        installation["id"], repo["owner"], repo["name"], repo["default_branch"]
    )
    return provider, FixtureWriteProvider(provider, **kwargs), installation["id"], head.head_sha  # type: ignore[arg-type]


def test_branch_guard_refuses_default_and_foreign_branches() -> None:
    assert_fix_branch("opspilot/fix-1234abcd-ffff0000", "opspilot/fix-", "main")
    for bad in ("main", "feature/x", "opspilot/fix-", "opspilot/fix-../x", "opspilot/fix-x.lock"):
        with pytest.raises(UnsafeBranchError):
            assert_fix_branch(bad, "opspilot/fix-", "main")
    with pytest.raises(UnsafeBranchError):
        assert_fix_branch("opspilot/fix-main", "opspilot/fix-", "opspilot/fix-main")


def test_fixture_write_is_idempotent_and_never_touches_default_branch() -> None:
    provider, writer, installation, head = fixture_pair()
    repo = provider.data["installations"][0]["repositories"][0]
    owner, name = repo["owner"], repo["name"]
    branch = "opspilot/fix-12345678-abcdef01"
    first = writer.create_branch(installation, owner, name, branch, head, "main", "opspilot/fix-")
    again = writer.create_branch(installation, owner, name, branch, head, "main", "opspilot/fix-")
    assert first.created and not again.created
    commit = writer.create_commit(
        installation, owner, name, branch, head, {"a.py": "x\n"}, "m", "h" * 8
    )
    assert (
        writer.create_commit(installation, owner, name, branch, head, {"a.py": "x\n"}, "m", "h" * 8)
        == commit
    )
    pr = writer.open_pull_request(installation, owner, name, branch, "main", "t", "b")
    assert writer.open_pull_request(installation, owner, name, branch, "main", "t", "b") == pr
    assert len(writer.pulls[(owner, name)]) == 1
    assert (owner, name, "main") not in writer.branches
    with pytest.raises(UnsafeBranchError):
        writer.create_branch(installation, owner, name, "main", head, "main", "opspilot/fix-")
    other = "opspilot/fix-99999999-abcdef01"
    writer.create_branch(installation, owner, name, other, head, "main", "opspilot/fix-")
    writer.branches[(owner, name, other)]["head"] = "f" * 40
    with pytest.raises(BranchConflictError):
        writer.create_commit(installation, owner, name, other, head, {}, "m", "z" * 8)


def test_write_requires_granted_permissions() -> None:
    provider, writer, installation, _ = fixture_pair(granted={"contents": "read"})
    repo = provider.data["installations"][0]["repositories"][0]
    with pytest.raises(WritePermissionError):
        writer.verify_write_access(installation, repo["owner"], repo["name"])


def test_write_surface_has_no_merge_force_or_deploy_capability() -> None:
    forbidden = (
        "merge",
        "force",
        "delete",
        "deploy",
        "release",
        "dispatch",
        "protect",
        "approve_pull",
    )
    for cls in (writer_module.WriteProvider, FixtureWriteProvider, GitHubWriteProvider):
        names = [n.lower() for n in dir(cls) if not n.startswith("__")]
        assert not [n for n in names if any(word in n for word in forbidden)], cls


def test_write_enablement_is_off_by_default_and_per_workspace_repo() -> None:
    workspace = uuid4()
    assert not write_enabled_for(make_settings(), workspace, "acme/checkout")
    flag_only = make_settings(REMEDIATION_GITHUB_WRITE_ENABLED=True)
    assert not write_enabled_for(flag_only, workspace, "acme/checkout")
    listed = make_settings(
        REMEDIATION_GITHUB_WRITE_ENABLED=True,
        REMEDIATION_WRITE_ENABLED_REPOSITORIES=f"{workspace}:acme/checkout",
    )
    assert write_enabled_for(listed, workspace, "acme/checkout")
    assert not write_enabled_for(listed, uuid4(), "acme/checkout")
    assert not write_enabled_for(listed, workspace, "acme/other")


def test_github_write_provider_only_uses_branch_object_and_pull_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        if "/git/ref/heads/" in path:
            return httpx.Response(404, json={})
        if path.endswith("/git/refs") and request.method == "POST":
            return httpx.Response(201, json={})
        if "/git/commits/" in path and request.method == "GET":
            return httpx.Response(
                200, json={"tree": {"sha": "tree1"}, "parents": [], "message": ""}
            )
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree2"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={
                    "id": 1,
                    "number": 7,
                    "html_url": "https://x/pull/7",
                    "state": "open",
                    "head": {"ref": "opspilot/fix-12345678-abcdef01", "sha": "c" * 40},
                },
            )
        return httpx.Response(500)

    class StubRead:
        def __init__(self) -> None:
            self._client = httpx.Client(
                base_url="https://api.github.test", transport=httpx.MockTransport(handler)
            )

        def _app_jwt(self) -> str:
            return "jwt"

        def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
            response = self._client.request(method, path, **kwargs)  # type: ignore[arg-type]
            if response.status_code == 404:
                from opspilot_api.repositories.provider import ProviderNotFoundError

                raise ProviderNotFoundError("nf")
            response.raise_for_status()
            return response

    provider = GitHubWriteProvider(make_settings(), StubRead())  # type: ignore[arg-type]
    branch = "opspilot/fix-12345678-abcdef01"
    sha = "a" * 40
    provider.create_branch(1, "acme", "app", branch, sha, "main", "opspilot/fix-")
    commit = provider.get_branch_head(1, "acme", "app", branch)
    assert commit is None
    seen.clear()
    provider._call = lambda *a, **k: (_ for _ in ()).throw(AssertionError)  # type: ignore[method-assign]
    methods = {method for method, _ in seen}
    assert methods <= {"GET", "POST", "PATCH"}
    with pytest.raises(UnsafeBranchError):
        provider.create_branch(1, "acme", "app", "main", sha, "main", "opspilot/fix-")


def test_github_write_calls_never_use_destructive_methods_or_merge_paths() -> None:
    sent: list[tuple[str, str]] = []

    class Recorder:
        def _app_jwt(self) -> str:
            return "jwt"

        def _send(self, method: str, path: str, **_kwargs: object) -> httpx.Response:
            sent.append((method, path))
            if path.endswith("/access_tokens"):
                return httpx.Response(201, json={"token": "t"})
            if "/git/ref/heads/" in path:
                from opspilot_api.repositories.provider import ProviderNotFoundError

                raise ProviderNotFoundError("nf")
            return httpx.Response(201, json={"sha": "c" * 40, "tree": {"sha": "t"}})

    provider = GitHubWriteProvider(make_settings(), Recorder())  # type: ignore[arg-type]
    provider.create_branch(
        1, "acme", "app", "opspilot/fix-12345678-abcdef01", "a" * 40, "main", "opspilot/fix-"
    )
    assert ("POST", "/repos/acme/app/git/refs") in sent
    assert all(method in {"GET", "POST", "PATCH"} for method, _ in sent)
    assert not any("merge" in path or "protection" in path for _, path in sent)
    with pytest.raises(Exception, match="not permitted"):
        provider._call("DELETE", 1, "/repos/acme/app/git/refs/heads/x")
    with pytest.raises(Exception, match="not permitted"):
        provider._call("PUT", 1, "/repos/acme/app/pulls/1/merge")


# ---- generator -------------------------------------------------------------------------------
def test_demo_generator_is_deterministic_and_can_abstain() -> None:
    evidence = [EvidenceContext(id=uuid4(), title="e", excerpt="x", source_type="code_chunk")]
    base = "POOL_SIZE = 2  # reduced from 20 in the latest release\n"
    context = GenerationContext(
        investigation_title="t",
        root_cause="pool",
        evidence=evidence,
        repository_full_name="acme/checkout",
        base_commit_sha="a" * 40,
        path_prefix=None,
        code_files={"src/checkout/db.py": base},
        available_command_ids=sorted(COMMANDS),
    )
    generator = FakeRemediationGenerator()
    first = generator.generate(context)
    assert first.output.unified_diff == generator.generate(context).output.unified_diff
    assert "POOL_SIZE = 20" in first.output.unified_diff
    empty = generator.generate(context.model_copy(update={"code_files": {"a.py": "x = 1\n"}}))
    assert empty.output.abstain and empty.output.abstain_reason == "no_safe_patch"
    assert make_diff("a.py", "a\n", "b\n").startswith("--- a/a.py")


def test_fake_provider_records_runs_per_sandbox() -> None:
    provider = FakeSandboxProvider(
        lambda command, files: (
            CommandResult(1, "FAILED t::x", "", 1)
            if "bad" in files
            else CommandResult(0, "", "", 1)
        )
    )
    sandbox = provider.create(LIMITS)
    sandbox.prepare({"a.py": b"x"})
    assert provider.created == [sandbox]
