"""Typed sandbox abstraction with a hardened Docker implementation and a deterministic fake.

Repository code runs only inside a sandbox. The model never influences Docker flags: they are
built by ``build_docker_args`` from the registry command and the server-side limits alone.
"""

import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from opspilot_api.config import Settings
from opspilot_api.remediation.commands import ResolvedCommand

logger = logging.getLogger(__name__)
SANDBOX_USER = "10001:10001"
CONTAINER_WORKSPACE = "/workspace"
WORKSPACE_PREFIX = "opspilot-sbx-"


class SandboxError(Exception):
    code = "sandbox_error"


class SandboxUnavailableError(SandboxError):
    code = "sandbox_unavailable"


class SandboxLimitError(SandboxError):
    code = "sandbox_limit_exceeded"


@dataclass(frozen=True)
class SandboxLimits:
    cpu: float
    memory_mb: int
    disk_mb: int
    pids: int
    timeout_seconds: int
    network_enabled: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "SandboxLimits":
        return cls(
            cpu=settings.REMEDIATION_SANDBOX_CPU,
            memory_mb=settings.REMEDIATION_SANDBOX_MEMORY_MB,
            disk_mb=settings.REMEDIATION_SANDBOX_DISK_MB,
            pids=settings.REMEDIATION_SANDBOX_PIDS,
            timeout_seconds=settings.REMEDIATION_SANDBOX_TIMEOUT_SECONDS,
            # Network is never enabled by configuration in this module.
            network_enabled=False,
        )


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False


class Sandbox(ABC):
    """One isolated workspace; created per run and always cleaned up."""

    provider: str
    limits: SandboxLimits

    @abstractmethod
    def prepare(self, files: Mapping[str, bytes]) -> None:
        """Materialize the checkout inside the sandbox's dedicated workspace."""

    @abstractmethod
    def list_files(self) -> set[str]: ...

    @abstractmethod
    def read_text(self, path: str) -> str | None: ...

    @abstractmethod
    def write_changes(self, changes: Mapping[str, str | None]) -> None:
        """Apply already-computed new file contents (``None`` deletes) inside the workspace."""

    @abstractmethod
    def run(self, command: ResolvedCommand) -> CommandResult: ...

    @abstractmethod
    def cleanup(self) -> bool:
        """Remove the workspace and any container; return whether everything is gone."""

    @property
    @abstractmethod
    def cleaned(self) -> bool: ...


class SandboxProvider(ABC):
    name: str

    @abstractmethod
    def create(self, limits: SandboxLimits) -> Sandbox: ...


def safe_join(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root`` or raise; guards traversal and escaping links."""
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise SandboxError(f"path escapes the workspace: {relative!r}")
    target = (root / relative).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise SandboxError(f"path escapes the workspace: {relative!r}")
    return target


def _make_writable(root: Path) -> None:
    if os.name == "nt":
        return
    for current, directories, files in os.walk(root):
        for name in directories:
            os.chmod(Path(current) / name, 0o777)  # noqa: S103
        for name in files:
            os.chmod(Path(current) / name, 0o666)  # noqa: S103
    os.chmod(root, 0o777)  # noqa: S103 - throwaway workspace for the non-root user


def _rmtree(path: Path) -> bool:
    def on_error(function: Callable[..., object], target: str, _exc: object) -> None:
        try:
            os.chmod(target, stat.S_IRWXU)
            function(target)
        except OSError:
            logger.warning("sandbox cleanup could not remove an entry")

    shutil.rmtree(path, onerror=on_error)
    return not path.exists()


def workspace_size_bytes(root: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                continue
    return total


def purge_expired_workspaces(root: Path | None, max_age_seconds: int) -> int:
    """Delete leftover sandbox workspaces older than the retention bound (crash recovery)."""
    base = root or Path(tempfile.gettempdir())
    removed = 0
    cutoff = time.time() - max_age_seconds
    if not base.exists():
        return 0
    for entry in base.iterdir():
        if entry.name.startswith(WORKSPACE_PREFIX) and entry.is_dir():
            try:
                if entry.stat().st_mtime < cutoff and _rmtree(entry):
                    removed += 1
            except OSError:
                continue
    return removed


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit


def bounded_run(
    argv: list[str], timeout: float, max_bytes: int, env: Mapping[str, str] | None = None
) -> tuple[int | None, bytes, bytes, bool, bool]:
    """Run without a shell, bounding captured output and wall time.

    Returns ``(exit_code, stdout, stderr, timed_out, truncated)``; excess output is drained and
    discarded so a noisy process can neither block nor exhaust memory.
    """
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    buffers: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}
    overflow = {"flag": False}

    def drain(stream: object, key: str) -> None:
        read = getattr(stream, "read")  # noqa: B009 - typing helper for IO streams
        while chunk := read(8192):
            room = max_bytes + 1 - len(buffers[key])
            if room > 0:
                buffers[key].extend(chunk[:room])
            if len(buffers[key]) > max_bytes:
                overflow["flag"] = True

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    for thread in threads:
        thread.join(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    exit_code = None if timed_out else process.returncode
    return exit_code, bytes(buffers["out"]), bytes(buffers["err"]), timed_out, overflow["flag"]


def build_docker_args(
    image: str,
    container_name: str,
    workspace: Path,
    limits: SandboxLimits,
    command: ResolvedCommand,
) -> list[str]:
    """The complete, fixed ``docker run`` vector. Nothing here comes from model output."""
    cwd = CONTAINER_WORKSPACE + (f"/{command.cwd}" if command.cwd else "")
    env_args: list[str] = []
    container_home = "/tmp"  # noqa: S108 - container-local tmpfs, not a host path
    base_env = (
        ("PATH", "/usr/local/bin:/usr/bin:/bin"),
        ("HOME", container_home),
        ("LANG", "C.UTF-8"),
    )
    for key, value in (*base_env, *command.spec.env):
        env_args += ["--env", f"{key}={value}"]
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "bridge" if limits.network_enabled else "none",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={limits.disk_mb}m",  # noqa: S108 - container-local tmpfs
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        f"{limits.memory_mb}m",
        "--memory-swap",
        f"{limits.memory_mb}m",
        "--cpus",
        str(limits.cpu),
        "--user",
        SANDBOX_USER,
        "--ulimit",
        "nofile=1024:1024",
        "--mount",
        f"type=bind,source={workspace},target={CONTAINER_WORKSPACE}",
        "--workdir",
        cwd,
        *env_args,
        "--entrypoint",
        command.argv[0],
        image,
        *command.argv[1:],
    ]


class DockerSandbox(Sandbox):
    provider = "docker"

    def __init__(self, settings: Settings, limits: SandboxLimits) -> None:
        self.settings = settings
        self.limits = limits
        root = settings.REMEDIATION_WORKSPACE_ROOT
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=root or None)).resolve()
        self._containers: list[str] = []
        self._cleaned = False

    @property
    def cleaned(self) -> bool:
        return self._cleaned

    def prepare(self, files: Mapping[str, bytes]) -> None:
        for relative, content in files.items():
            target = safe_join(self.workspace, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _make_writable(self.workspace)

    def list_files(self) -> set[str]:
        return {
            path.relative_to(self.workspace).as_posix()
            for path in self.workspace.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def read_text(self, path: str) -> str | None:
        target = safe_join(self.workspace, path)
        if not target.is_file() or target.is_symlink():
            return None
        return target.read_bytes().decode("utf-8", errors="strict")

    def write_changes(self, changes: Mapping[str, str | None]) -> None:
        for relative, content in changes.items():
            target = safe_join(self.workspace, relative)
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        _make_writable(self.workspace)

    def run(self, command: ResolvedCommand) -> CommandResult:
        if self._cleaned:
            raise SandboxError("sandbox already cleaned up")
        name = f"opspilot-sbx-{uuid.uuid4().hex[:16]}"
        self._containers.append(name)
        args = build_docker_args(
            self.settings.REMEDIATION_SANDBOX_IMAGE, name, self.workspace, self.limits, command
        )
        timeout = min(command.spec.timeout_seconds, self.limits.timeout_seconds)
        started = time.monotonic()
        try:
            code, out, err, timed_out, truncated = bounded_run(
                args, timeout + 5, command.spec.max_output_bytes
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError("docker executable not found") from exc
        finally:
            self._kill(name)
        duration = int((time.monotonic() - started) * 1000)
        if code in {125, 126, 127} and not timed_out:
            message = err.decode("utf-8", errors="replace")[:200]
            if "Unable to find image" in message or "No such image" in message or code == 125:
                raise SandboxUnavailableError("sandbox image or docker daemon unavailable")
        stdout, cut_out = _truncate(out, command.spec.max_output_bytes)
        stderr, cut_err = _truncate(err, command.spec.max_output_bytes)
        if workspace_size_bytes(self.workspace) > self.limits.disk_mb * 1024 * 1024:
            raise SandboxLimitError("workspace disk limit exceeded")
        return CommandResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            truncated=truncated or cut_out or cut_err,
        )

    @staticmethod
    def _kill(name: str) -> None:
        try:
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["docker", "rm", "-f", name],  # noqa: S607
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("sandbox container removal failed")

    def cleanup(self) -> bool:
        for name in self._containers:
            self._kill(name)
        removed = _rmtree(self.workspace) if self.workspace.exists() else True
        if not removed:
            # Files written by the container user may be unremovable by the host user on Linux.
            self._cleanup_in_container()
            removed = _rmtree(self.workspace) if self.workspace.exists() else True
        self._cleaned = removed
        return removed

    def _cleanup_in_container(self) -> None:
        args = [
            "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", SANDBOX_USER,
            "--mount", f"type=bind,source={self.workspace},target={CONTAINER_WORKSPACE}",
            "--entrypoint", "find", self.settings.REMEDIATION_SANDBOX_IMAGE,
            CONTAINER_WORKSPACE, "-mindepth", "1", "-delete",
        ]  # fmt: skip
        try:
            bounded_run(args, 60, 4096)
        except OSError:
            logger.warning("sandbox in-container cleanup failed")


class DockerSandboxProvider(SandboxProvider):
    name = "docker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_available(self) -> None:
        try:
            code, _out, _err, timed_out, _ = bounded_run(
                ["docker", "image", "inspect", self.settings.REMEDIATION_SANDBOX_IMAGE], 20, 2048
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError("docker executable not found") from exc
        if timed_out or code != 0:
            raise SandboxUnavailableError(
                "docker unavailable or sandbox image missing; build infra/docker/sandbox.Dockerfile"
            )

    def create(self, limits: SandboxLimits) -> Sandbox:
        self.check_available()
        return DockerSandbox(self.settings, limits)


FakeRunner = Callable[[ResolvedCommand, Mapping[str, str]], CommandResult]


def passing_runner(command: ResolvedCommand, files: Mapping[str, str]) -> CommandResult:
    return CommandResult(0, f"fake sandbox: {command.command_id} simulated", "", 1)


class FakeSandbox(Sandbox):
    """In-memory sandbox. Executes nothing; results come from a deterministic runner."""

    provider = "fake"

    def __init__(self, limits: SandboxLimits, runner: FakeRunner = passing_runner) -> None:
        self.limits = limits
        self.runner = runner
        self.files: dict[str, str] = {}
        self.runs: list[tuple[str, ...]] = []
        self._cleaned = False
        self.env_seen: dict[str, str] = {}

    @property
    def cleaned(self) -> bool:
        return self._cleaned

    def prepare(self, files: Mapping[str, bytes]) -> None:
        self.files = {
            path: content.decode("utf-8", errors="replace") for path, content in files.items()
        }

    def list_files(self) -> set[str]:
        return set(self.files)

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def write_changes(self, changes: Mapping[str, str | None]) -> None:
        for path, content in changes.items():
            if content is None:
                self.files.pop(path, None)
            else:
                self.files[path] = content

    def run(self, command: ResolvedCommand) -> CommandResult:
        if self._cleaned:
            raise SandboxError("sandbox already cleaned up")
        self.runs.append(command.argv)
        self.env_seen = dict(command.spec.env)
        return self.runner(command, self.files)

    def cleanup(self) -> bool:
        self.files = {}
        self._cleaned = True
        return True


class FakeSandboxProvider(SandboxProvider):
    name = "fake"

    def __init__(self, runner: FakeRunner = passing_runner) -> None:
        self.runner = runner
        self.created: list[FakeSandbox] = []

    def create(self, limits: SandboxLimits) -> Sandbox:
        sandbox = FakeSandbox(limits, self.runner)
        self.created.append(sandbox)
        return sandbox


def create_sandbox_provider(settings: Settings) -> SandboxProvider:
    if settings.REMEDIATION_SANDBOX_PROVIDER == "docker":
        return DockerSandboxProvider(settings)
    return FakeSandboxProvider()
