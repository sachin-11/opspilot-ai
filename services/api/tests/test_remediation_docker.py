"""Real Docker sandbox behavior. Needs Docker and the ``opspilot-sandbox:latest`` image.

Build it with ``docker build -f infra/docker/sandbox.Dockerfile -t opspilot-sandbox:latest
infra/docker``. Probes are repository test files executed through the registered pytest command,
exactly as untrusted repository code would run.
"""

import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from opspilot_api.remediation.commands import resolve_command
from opspilot_api.remediation.sandbox import (
    CommandResult,
    DockerSandbox,
    DockerSandboxProvider,
    SandboxLimitError,
    SandboxLimits,
    SandboxUnavailableError,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

LIMITS = SandboxLimits(cpu=1, memory_mb=128, disk_mb=32, pids=32, timeout_seconds=20)


@pytest.fixture(scope="module", autouse=True)
def require_sandbox_image() -> None:
    try:
        DockerSandboxProvider(make_settings()).check_available()
    except SandboxUnavailableError as exc:
        pytest.fail(f"Docker sandbox integration tests need the sandbox image: {exc}")


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sandbox(workspace_root: Path) -> Iterator[DockerSandbox]:
    box = DockerSandbox(make_settings(REMEDIATION_WORKSPACE_ROOT=str(workspace_root)), LIMITS)
    try:
        yield box
    finally:
        box.cleanup()


def probe(box: DockerSandbox, source: str, limits: SandboxLimits | None = None) -> CommandResult:
    box.prepare({"tests/test_probe.py": textwrap.dedent(source).encode(), "pyproject.toml": b""})
    if limits is not None:
        box.limits = limits
    command = resolve_command("python.pytest", box.list_files(), None)
    assert command is not None
    return box.run(command)


def test_probe_runs_non_root_with_dropped_privileges(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        import os


        def test_identity():
            assert os.getuid() == 10001 and os.getgid() == 10001
            status = dict(
                line.split(":\\t", 1) for line in open("/proc/self/status").read().splitlines() if ":\\t" in line
            )
            assert int(status["CapEff"], 16) == 0
            assert status["NoNewPrivs"].strip() == "1"
        """,
    )
    assert result.exit_code == 0, result.stdout + result.stderr


def test_root_filesystem_is_read_only_and_workspace_is_writable(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        import pytest


        def test_fs():
            with pytest.raises(OSError):
                open("/etc/opspilot-probe", "w")
            with pytest.raises(OSError):
                open("/usr/local/bin/opspilot-probe", "w")
            open("/workspace/written.txt", "w").write("ok")
            open("/tmp/scratch", "w").write("ok")
        """,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert sandbox.read_text("written.txt") == "ok"


def test_no_host_secrets_docker_socket_or_home_are_visible(
    sandbox: DockerSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_APP_PRIVATE_KEY",
        "REDIS_URL",
        "POSTGRES_PASSWORD",
        "AI_PRIMARY_API_KEY",
        "KAFKA_BOOTSTRAP_SERVERS",
    ):
        monkeypatch.setenv(name, "must-not-leak-9f3a")
    result = probe(
        sandbox,
        """
        import os


        def test_environment_is_sanitized():
            assert "must-not-leak-9f3a" not in " ".join(os.environ.values())
            allowed = {
                "PATH", "HOME", "LANG", "CI", "NO_COLOR", "PYTHONDONTWRITEBYTECODE",
                "PYTHONHASHSEED", "HOSTNAME", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                "PYTHONUNBUFFERED", "PYTEST_CURRENT_TEST", "PYTEST_VERSION",
            }
            assert not [key for key in os.environ if key not in allowed], sorted(os.environ)
            assert not os.path.exists("/var/run/docker.sock")
            assert not os.path.exists("/run/docker.sock")
        """,
    )
    assert result.exit_code == 0, result.stdout + result.stderr


def test_network_is_disabled_by_default(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        import socket


        def test_no_network():
            with socket.socket() as sock:
                sock.settimeout(3)
                sock.connect(("1.1.1.1", 53))
        """,
    )
    assert result.exit_code != 0
    assert "FAILED" in result.stdout
    assert "unreachable" in (result.stdout + result.stderr).lower() or "OSError" in result.stdout


def test_hard_timeout_kills_the_container(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        import time


        def test_hang():
            time.sleep(120)
        """,
        SandboxLimits(cpu=1, memory_mb=128, disk_mb=32, pids=32, timeout_seconds=3),
    )
    assert result.timed_out and result.exit_code is None
    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=opspilot-sbx-", "-q"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert leftovers == ""


def test_memory_limit_is_enforced(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        def test_alloc():
            blocks = []
            for _ in range(60):
                blocks.append(bytearray(10 * 1024 * 1024))
        """,
    )
    assert result.exit_code != 0


def test_pid_limit_is_enforced(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        import subprocess
        import sys

        import pytest


        def test_fork_limit():
            children = []
            with pytest.raises(OSError):
                for _ in range(200):
                    children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"]))
            for child in children:
                child.kill()
        """,
    )
    assert result.exit_code == 0, result.stdout + result.stderr


def test_output_is_bounded_and_marked_truncated(sandbox: DockerSandbox) -> None:
    result = probe(
        sandbox,
        """
        def test_noisy():
            print("x" * 2_000_000)
            assert False, "x" * 2_000_000  # failing tests echo captured output
        """,
    )
    assert result.truncated
    assert len(result.stdout) <= 16_384 and len(result.stderr) <= 16_384


def test_disk_limit_is_enforced(sandbox: DockerSandbox) -> None:
    with pytest.raises(SandboxLimitError):
        probe(
            sandbox,
            """
            def test_fill_disk():
                open("/workspace/big.bin", "wb").write(b"0" * (40 * 1024 * 1024))
            """,
        )


def test_cleanup_removes_workspace_and_containers(
    workspace_root: Path, sandbox: DockerSandbox
) -> None:
    probe(sandbox, "def test_ok():\n    assert True\n")
    assert any(workspace_root.iterdir())
    assert sandbox.cleanup() is True
    assert not any(workspace_root.iterdir())
    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=opspilot-sbx-", "-q"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert leftovers == ""


def test_cleanup_runs_after_failure_paths(workspace_root: Path) -> None:
    box = DockerSandbox(make_settings(REMEDIATION_WORKSPACE_ROOT=str(workspace_root)), LIMITS)
    try:
        with pytest.raises(SandboxLimitError):
            probe(
                box,
                """
                def test_fill_disk():
                    open("/workspace/big.bin", "wb").write(b"0" * (40 * 1024 * 1024))
                """,
            )
    finally:
        assert box.cleanup() is True
    assert not any(workspace_root.iterdir())
