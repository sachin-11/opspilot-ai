"""Registry of verification commands. The model may select IDs; it can never supply a command.

Every command is an argv vector (no shell), runs only inside a sandbox, and declares its working
directory policy, timeout, allowed environment, output bound, and required project markers.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from opspilot_api.remediation.models import CheckType

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")
_FAILED_PYTEST = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_FAILED_TAP = re.compile(r"^\s*not ok \d+ - (.+?)(?:\s+#.*)?$", re.MULTILINE)
MAX_BOUNDED_ARGS = 10


class CommandError(ValueError):
    code = "invalid_command"


@dataclass(frozen=True)
class RegisteredCommand:
    command_id: str
    display_name: str
    check_type: CheckType
    argv: tuple[str, ...]
    # The command is applicable when *any* marker exists (relative to the working directory).
    markers: tuple[str, ...]
    timeout_seconds: int = 120
    max_output_bytes: int = 16_384
    # Environment variable names the registry may set; values are fixed, never copied from the host.
    env: tuple[tuple[str, str], ...] = (("CI", "1"), ("NO_COLOR", "1"))
    working_dir: str = "root"  # "root" or "path_prefix"
    # Optional bounded path arguments (validated, appended after ``argv``).
    accepts_paths: bool = False
    languages: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ResolvedCommand:
    spec: RegisteredCommand
    argv: tuple[str, ...]
    cwd: str  # relative to the workspace root; "" is the root

    @property
    def command_id(self) -> str:
        return self.spec.command_id


_PYTHON_ENV = (
    ("CI", "1"),
    ("NO_COLOR", "1"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
    ("PYTHONHASHSEED", "0"),
)
_PY_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py", "tests")

COMMANDS: dict[str, RegisteredCommand] = {
    command.command_id: command
    for command in (
        RegisteredCommand(
            "python.pytest",
            "pytest (full suite)",
            CheckType.UNIT_TEST,
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--maxfail=50", "-rf"),
            _PY_MARKERS,
            env=_PYTHON_ENV,
            languages=("python",),
        ),
        RegisteredCommand(
            "python.pytest_targeted",
            "pytest (targeted)",
            CheckType.TARGETED_TEST,
            ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--maxfail=50", "-rf"),
            _PY_MARKERS,
            env=_PYTHON_ENV,
            accepts_paths=True,
            languages=("python",),
        ),
        RegisteredCommand(
            "python.ruff_check",
            "ruff lint",
            CheckType.LINT,
            ("ruff", "check", "--no-cache", "."),
            ("pyproject.toml", "ruff.toml", ".ruff.toml"),
            env=_PYTHON_ENV,
            languages=("python",),
        ),
        RegisteredCommand(
            "python.ruff_format",
            "ruff format check",
            CheckType.FORMAT,
            ("ruff", "format", "--check", "--no-cache", "."),
            ("pyproject.toml", "ruff.toml", ".ruff.toml"),
            env=_PYTHON_ENV,
            languages=("python",),
        ),
        RegisteredCommand(
            "python.ruff_security",
            "ruff security rules",
            CheckType.SECURITY_SCAN,
            ("ruff", "check", "--no-cache", "--select", "S", "."),
            ("pyproject.toml", "ruff.toml", ".ruff.toml"),
            env=_PYTHON_ENV,
            languages=("python",),
        ),
        RegisteredCommand(
            "python.mypy",
            "mypy type check",
            CheckType.TYPECHECK,
            ("mypy", "--no-incremental", "--cache-dir=/tmp/mypy-cache", "."),
            ("mypy.ini", "pyproject.toml", "setup.cfg"),
            env=_PYTHON_ENV,
            timeout_seconds=180,
            languages=("python",),
        ),
        RegisteredCommand(
            "node.test",
            "node test runner",
            CheckType.UNIT_TEST,
            ("node", "--test", "--test-reporter=tap"),
            ("package.json",),
            languages=("node",),
        ),
        RegisteredCommand(
            "node.lint",
            "node syntax check",
            CheckType.LINT,
            ("node", "--check"),
            ("package.json",),
            accepts_paths=True,
            languages=("node",),
        ),
        RegisteredCommand(
            "node.typecheck",
            "tsc type check",
            CheckType.TYPECHECK,
            ("tsc", "--noEmit"),
            ("tsconfig.json",),
            timeout_seconds=180,
            languages=("node",),
        ),
        RegisteredCommand(
            "node.build",
            "package build",
            CheckType.BUILD,
            ("npm", "run", "build", "--silent"),
            ("package.json",),
            timeout_seconds=240,
            env=(("CI", "1"), ("NO_COLOR", "1"), ("npm_config_update_notifier", "false")),
            languages=("node",),
        ),
    )
}


def is_registered(command_id: str) -> bool:
    return command_id in COMMANDS


def validate_bounded_paths(paths: list[str]) -> tuple[str, ...]:
    if len(paths) > MAX_BOUNDED_ARGS:
        raise CommandError("too many path arguments")
    checked: list[str] = []
    for raw in paths:
        pure = PurePosixPath(raw)
        if (
            not _SAFE_ARG.match(raw)
            or raw.startswith(("-", "/"))
            or ".." in pure.parts
            or "\\" in raw
        ):
            raise CommandError(f"unsafe path argument: {raw!r}")
        checked.append(raw)
    return tuple(checked)


def resolve_command(
    command_id: str,
    workspace_files: set[str],
    path_prefix: str | None,
    paths: list[str] | None = None,
) -> ResolvedCommand | None:
    """Return the resolved command, or ``None`` when its project markers are absent.

    Unknown IDs raise :class:`CommandError`; free-form strings can never resolve.
    """
    spec = COMMANDS.get(command_id)
    if spec is None:
        raise CommandError(f"unregistered command id: {command_id!r}")
    cwd = (path_prefix or "").strip("/") if spec.working_dir == "path_prefix" else ""
    prefix = f"{cwd}/" if cwd else ""

    def present(marker: str) -> bool:
        full = prefix + marker
        return full in workspace_files or any(
            item.startswith(full + "/") for item in workspace_files
        )

    if not any(present(marker) for marker in spec.markers):
        return None
    extra: tuple[str, ...] = ()
    if paths:
        if not spec.accepts_paths:
            raise CommandError(f"{command_id} does not accept path arguments")
        extra = validate_bounded_paths(paths)
    elif spec.accepts_paths:
        # Path-taking commands run only when bounded paths were supplied.
        return None
    return ResolvedCommand(spec=spec, argv=spec.argv + extra, cwd=cwd)


def detect_languages(workspace_files: set[str]) -> set[str]:
    languages: set[str] = set()
    for item in workspace_files:
        suffix = PurePosixPath(item).suffix
        if suffix == ".py":
            languages.add("python")
        elif suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx"}:
            languages.add("node")
    return languages


def default_command_ids(workspace_files: set[str]) -> list[str]:
    languages = detect_languages(workspace_files)
    ids: list[str] = []
    if "python" in languages:
        ids.append("python.pytest")
    if "node" in languages:
        ids.append("node.test")
    return ids


def extract_failures(command_id: str, stdout: str, stderr: str) -> list[str]:
    """Opaque, bounded failure identifiers; compared set-wise between baseline and patched runs."""
    combined = f"{stdout}\n{stderr}"
    if command_id.startswith("python.pytest"):
        found = sorted(set(_FAILED_PYTEST.findall(combined)))
    elif command_id == "node.test":
        found = sorted(set(match.strip() for match in _FAILED_TAP.findall(combined)))
    else:
        found = []
    if found:
        return found[:100]
    normalized = re.sub(r"\d+(\.\d+)?s?\b", "N", combined)
    normalized = re.sub(r"/tmp/\S+|opspilot-sbx-\S+", "<tmp>", normalized)  # noqa: S108
    signature = hashlib.sha256(normalized.strip().encode("utf-8", "replace")).hexdigest()[:16]
    return [f"signature:{signature}"]
