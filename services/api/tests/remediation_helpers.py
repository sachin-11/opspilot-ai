"""Shared synthetic repositories and scenario builders for remediation tests (fixture providers only)."""

import copy
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident, MonitoredService, Workspace
from opspilot_api.investigations.models import (
    EvidenceItem,
    Investigation,
    InvestigationReview,
)
from opspilot_api.remediation.commands import ResolvedCommand
from opspilot_api.remediation.generator import (
    FakeRemediationGenerator,
    make_diff,
    make_new_file_diff,
)
from opspilot_api.remediation.sandbox import CommandResult, FakeSandboxProvider
from opspilot_api.remediation.schemas import (
    FileExplanation,
    GenerationContext,
    RemediationOutput,
    RiskAssessment,
)
from opspilot_api.repositories.fixture_provider import FixtureProvider
from opspilot_api.repositories.models import Repository
from opspilot_api.repositories.queue import get_provider  # noqa: F401  (re-export for tests)

from .repo_helpers import (
    connect_installation,
    installation_id,
    map_service,
    new_service,
    new_workspace,
    sync,
)

BUGGY_OPS = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    return a - b


def double(value: int) -> int:
    return value * 2
'''
FIXED_OPS = BUGGY_OPS.replace("return a - b", "return a + b")
TEST_OPS = """from calc.ops import add, double


def test_add() -> None:
    assert add(2, 3) == 5


def test_double() -> None:
    assert double(4) == 8
"""
CONFTEST = 'import sys\n\nsys.path.insert(0, "src")\n'
CALC_FILES = {
    "pyproject.toml": '[project]\nname = "calc"\nversion = "0.1.0"\n',
    "conftest.py": CONFTEST,
    "src/calc/__init__.py": "",
    "src/calc/ops.py": BUGGY_OPS,
    "tests/test_ops.py": TEST_OPS,
}
HEAD_TIME = "2026-03-10T11:00:00Z"
BASE_INSTALLATION = 7001


def calc_repo_data(
    offset: int, files: Mapping[str, str] | None = None, extra_commits: int = 0
) -> dict[str, Any]:
    commits = [
        {
            "sha": f"{index + 1:040x}",
            "message": f"commit {index + 1}",
            "author_name": "dev",
            "author_email": "dev@example.test",
            "committed_at": (datetime(2026, 3, 10, 11, 0, tzinfo=UTC) + timedelta(minutes=index))
            .isoformat()
            .replace("+00:00", "Z"),
            "parents": [f"{index:040x}"] if index else [],
            "files": [],
        }
        for index in range(1 + extra_commits)
    ]
    return {
        "installations": [
            {
                "id": BASE_INSTALLATION + offset,
                "account_login": "acme",
                "account_type": "Organization",
                "repository_selection": "all",
                "permissions": {
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                },
                "suspended": False,
                "installed_at": "2026-01-01T00:00:00Z",
                "repositories": [
                    {
                        "id": 70010 + offset,
                        "owner": "acme",
                        "name": "calc",
                        "default_branch": "main",
                        "private": True,
                        "archived": False,
                        "files": {
                            path: {"content": content}
                            for path, content in (files or CALC_FILES).items()
                        },
                        "commits": commits,
                        "deployments": [],
                        "pull_requests": [],
                    }
                ],
            }
        ]
    }


def advance_head(provider: FixtureProvider) -> str:
    """Add a newer commit to the fixture repository so the base commit becomes stale."""
    repo = provider.data["installations"][0]["repositories"][0]
    newest = max(repo["commits"], key=lambda item: item["committed_at"])
    stamp = datetime.fromisoformat(newest["committed_at"].replace("Z", "+00:00")) + timedelta(
        minutes=5
    )
    sha = f"{len(repo['commits']) + 1:040x}"
    repo["commits"].append(
        {
            "sha": sha,
            "message": "advance",
            "author_name": "dev",
            "author_email": "dev@example.test",
            "committed_at": stamp.isoformat().replace("+00:00", "Z"),
            "parents": [newest["sha"]],
            "files": [],
        }
    )
    return sha


@dataclass
class Scenario:
    settings: Settings
    provider: FixtureProvider
    workspace: Workspace
    service: MonitoredService
    repository: Repository
    incident: Incident
    investigation: Investigation
    evidence: list[EvidenceItem]
    head_sha: str
    files: dict[str, str]

    @property
    def installation_provider_id(self) -> int:
        return installation_id(self.provider, BASE_INSTALLATION)


def build_scenario(
    session: Session,
    settings: Settings,
    *,
    files: Mapping[str, str] | None = None,
    path_prefix: str | None = None,
    review_action: str = "approved",
    status: str = "completed",
    root_cause: str | None = "calc.ops.add subtracts instead of adding the operands.",
    with_evidence: bool = True,
    map_repository: bool = True,
) -> Scenario:
    offset = secrets.randbelow(10**8) * 1000
    provider = FixtureProvider(data=copy.deepcopy(calc_repo_data(offset, files)))
    provider.offset = offset  # type: ignore[attr-defined]
    workspace = new_workspace(session, "rem")
    service = new_service(session, workspace, f"calc-{secrets.token_hex(3)}")
    _installation, repositories = connect_installation(
        session, settings, provider, workspace, BASE_INSTALLATION
    )
    repository = repositories["acme/calc"]
    sync(session, settings, provider, workspace, repository)
    session.refresh(repository)
    if map_repository:
        map_service(session, workspace, service, repository, path_prefix=path_prefix)
    incident = Incident(
        workspace_id=workspace.id,
        service_id=service.id,
        title="Totals are wrong",
        description="add() returns the wrong value",
        severity="sev2",
        status="open",
        source="manual",
        opened_at=datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
    )
    session.add(incident)
    session.flush()
    investigation = Investigation(
        id=uuid4(),
        workspace_id=workspace.id,
        incident_id=incident.id,
        graph_thread_id=f"inv-{uuid4()}",
        status=status,
        trigger="manual",
        prompt_version="v1",
        primary_provider="fake",
        primary_model="deterministic-v1",
        title="add() subtracts",
        root_cause=root_cause,
        confidence=0.9,
        version=1,
    )
    session.add(investigation)
    session.flush()
    evidence: list[EvidenceItem] = []
    if with_evidence:
        item = EvidenceItem(
            id=uuid4(),
            workspace_id=workspace.id,
            investigation_id=investigation.id,
            evidence_type="code",
            source_type="code_chunk",
            source_id="chunk-1",
            title="src/calc/ops.py add() subtracts",
            excerpt="def add(a, b): return a - b in src/calc/ops.py",
            evidence_metadata={"path": "src/calc/ops.py", "start_line": 4},
            content_hash=secrets.token_hex(32),
            collected_at=datetime(2026, 3, 10, 12, 1, tzinfo=UTC),
        )
        session.add(item)
        evidence.append(item)
    session.add(
        InvestigationReview(
            id=uuid4(),
            workspace_id=workspace.id,
            investigation_id=investigation.id,
            action=review_action,
            comment="looks right",
        )
    )
    session.commit()
    head = provider.get_branch(
        installation_id(provider, BASE_INSTALLATION), "acme", "calc", "main"
    ).head_sha
    return Scenario(
        settings,
        provider,
        workspace,
        service,
        repository,
        incident,
        investigation,
        evidence,
        head,
        dict(files or CALC_FILES),
    )


Edits = Mapping[str, str | None]


def edit_output(
    context: GenerationContext,
    edits: Edits,
    new_files: Mapping[str, str] | None = None,
    base_files: Mapping[str, str] | None = None,
    **overrides: Any,
) -> RemediationOutput:
    """A well-formed model output whose diff is computed from before/after file contents."""
    sources = dict(base_files or CALC_FILES)
    diff = ""
    paths: list[str] = []
    for path, after in edits.items():
        before = sources.get(path, context.code_files.get(path, ""))
        diff += make_diff(path, before, after) if after is not None else _deletion(path, before)
        paths.append(path)
    for path, content in (new_files or {}).items():
        diff += make_new_file_diff(path, content)
        paths.append(path)
    values: dict[str, Any] = {
        "title": "Fix add() operands",
        "problem_statement": "add() subtracts its operands",
        "evidence_ids": [item.id for item in context.evidence][:2],
        "target_repository": context.repository_full_name,
        "base_commit_sha": context.base_commit_sha,
        "files": [FileExplanation(path=path, explanation="fix") for path in paths],
        "unified_diff": diff,
        "expected_behavior_change": "add() now returns the sum",
        "risk_assessment": RiskAssessment(level="low", rationale="one-line fix"),
        "verification_command_ids": ["python.pytest"],
        "rollback_guidance": "Revert the pull request.",
        "known_limitations": ["Only the unit tests were run."],
    }
    values.update(overrides)
    return RemediationOutput.model_validate(values)


def _deletion(path: str, before: str) -> str:
    lines = before.splitlines(keepends=True)
    return f"--- a/{path}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n" + "".join(
        f"-{line}" if line.endswith("\n") else f"-{line}\n" for line in lines
    )


FIX_EDITS: Edits = {"src/calc/ops.py": FIXED_OPS}


def good_generator(
    new_files: Mapping[str, str] | None = None, **overrides: Any
) -> FakeRemediationGenerator:
    return FakeRemediationGenerator(lambda ctx: edit_output(ctx, FIX_EDITS, new_files, **overrides))


def scripted_runner(
    fixed_marker: str = "return a + b",
    extra_failing: tuple[str, ...] = (),
    path: str = "src/calc/ops.py",
) -> Callable[[ResolvedCommand, Mapping[str, str]], CommandResult]:
    """Deterministic stand-in for pytest: passes when the source is fixed."""

    def run(command: ResolvedCommand, files: Mapping[str, str]) -> CommandResult:
        targeted = len(command.argv) > len(command.spec.argv)
        failing = [] if targeted else list(extra_failing)
        if fixed_marker not in files.get(path, ""):
            failing.append("tests/test_ops.py::test_add")
        if not failing:
            return CommandResult(0, "2 passed", "", 5)
        out = "\n".join(f"FAILED {name} - assert" for name in failing)
        return CommandResult(1, out, "", 5)

    return run


def fake_sandboxes(**kwargs: Any) -> FakeSandboxProvider:
    return FakeSandboxProvider(scripted_runner(**kwargs))
