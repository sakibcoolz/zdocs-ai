"""Isolated execution for the development-validation profile.

Repository *analysis* never executes project code. Running a project's tests,
linters or type checkers does — the repository is untrusted, so its
``conftest.py`` or ``eslint.config.js`` must be assumed hostile. That is a
categorically different risk from reading files, and it gets a categorically
different mechanism: a disposable container with no network, a read-only
filesystem, dropped capabilities and hard CPU/memory/PID/time limits.

Design notes:

* **The caller picks a tool, never a command line.** :data:`VALIDATION_COMMANDS`
  maps a name to a fixed argument vector — the same "enumerate, don't accept
  strings" rule the operation layer uses.
* **:func:`DockerSandbox.docker_argv` is pure.** The hardening flags can be
  asserted in a unit test on a machine with no Docker, which is where the
  security properties actually get verified.
* **Absent means absent.** With no usable backend the sandbox reports
  unavailable and the operation fails loudly. It never silently falls back to
  running project code on the host — that would be the one outcome worse than
  not running it at all.

Nothing here is enabled by default; see :meth:`operations.policy.ExecutionPolicy.development_validation`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from operations.command_runner import CommandRunner
from operations.errors import OperationError, PolicyViolation
from operations.policy import ExecutionPolicy, redact
from pydantic import BaseModel, Field

#: Container image used for validation runs. Overridable because the right
#: image depends entirely on the repository's toolchain.
DEFAULT_IMAGE = os.getenv("ZDOCS_VALIDATION_IMAGE", "python:3.12-slim")

#: Mount point of the analyzed repository inside the container.
WORKSPACE = "/workspace"

#: Unprivileged uid/gid used inside the container (``nobody`` on most images).
SANDBOX_UID = 65534
SANDBOX_GID = 65534


class SandboxUnavailable(OperationError):
    """No usable sandbox backend is configured."""


class SandboxLimits(BaseModel):
    """Resource ceiling for one validation run."""

    memory_mb: int = Field(default=512, ge=64, le=8192)
    cpus: float = Field(default=1.0, gt=0, le=8)
    pids: int = Field(default=256, ge=16, le=4096)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=20_000_000)


@dataclass(frozen=True)
class ValidationCommand:
    """One approved validation invocation."""

    name: str
    argv: tuple[str, ...]
    description: str
    language: str
    accepts_path: bool = True
    """Whether a repository-relative path may be appended to ``argv``."""


#: Approved validation tools. The argument vectors are fixed here; a caller
#: supplies a name and (optionally) a validated repository-relative path.
#: Every entry is read-only with respect to the repository and makes no network
#: request — the container has no network in any case.
VALIDATION_COMMANDS: dict[str, ValidationCommand] = {
    "pytest": ValidationCommand(
        name="pytest",
        argv=("python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"),
        description="Run the repository's Python test suite.",
        language="python",
    ),
    "ruff": ValidationCommand(
        name="ruff",
        argv=("python", "-m", "ruff", "check", "--no-cache", "--output-format", "concise"),
        description="Lint Python sources with Ruff.",
        language="python",
    ),
    "mypy": ValidationCommand(
        name="mypy",
        argv=("python", "-m", "mypy", "--no-incremental", "--cache-dir", "/tmp/mypy"),
        description="Type-check Python sources with mypy.",
        language="python",
    ),
    "go-vet": ValidationCommand(
        name="go-vet",
        argv=("go", "vet", "./..."),
        description="Report suspicious constructs in Go sources.",
        language="go",
        accepts_path=False,
    ),
    "go-build": ValidationCommand(
        name="go-build",
        argv=("go", "build", "-o", "/tmp/build-output", "./..."),
        description="Type-check Go sources by building them.",
        language="go",
        accepts_path=False,
    ),
    "tsc": ValidationCommand(
        name="tsc",
        argv=("npx", "--no-install", "tsc", "--noEmit"),
        description="Type-check TypeScript sources.",
        language="typescript",
        accepts_path=False,
    ),
    "eslint": ValidationCommand(
        name="eslint",
        argv=("npx", "--no-install", "eslint", "--no-color"),
        description="Lint JavaScript/TypeScript sources.",
        language="javascript",
    ),
}


class SandboxResult(BaseModel):
    """Outcome of one validation run."""

    tool: str
    argv: list[str] = Field(default_factory=list)
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    backend: str = ""
    image: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    """Backend that can run a validation command in isolation."""

    def available(self) -> bool:
        """Whether this backend can actually run something right now."""

    def describe(self) -> str:
        """Human-readable description of the backend and why it is (un)available."""

    def run(
        self, command: ValidationCommand, repo_root: Path, limits: SandboxLimits, *, path: str | None = None
    ) -> SandboxResult:
        """Run ``command`` against ``repo_root`` under ``limits``."""


class UnavailableSandbox:
    """The default backend: refuses, and says exactly why.

    Failing loudly is deliberate. Running a repository's own test suite on the
    host because no container runtime was found would execute untrusted code
    with the server's privileges — strictly worse than declining.
    """

    def __init__(self, reason: str = "no sandbox backend is configured") -> None:
        self.reason = reason

    def available(self) -> bool:
        return False

    def describe(self) -> str:
        return f"unavailable: {self.reason}"

    def run(
        self, command: ValidationCommand, repo_root: Path, limits: SandboxLimits, *, path: str | None = None
    ) -> SandboxResult:
        raise SandboxUnavailable(
            f"Cannot run {command.name!r}: {self.reason}. Validation commands are "
            f"never run on the host."
        )


class DockerSandbox:
    """Runs validation commands in a disposable, locked-down container."""

    backend = "docker"

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        runner: CommandRunner | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.image = image
        self._policy = policy or ExecutionPolicy.sandbox_host()
        self._runner = runner if runner is not None else CommandRunner(self._policy)

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        """Whether the Docker CLI is installed and its daemon responds."""
        if not self._runner.is_available("docker"):
            return False
        result = self._runner.run(["docker", "version", "--format", "{{.Server.Version}}"], cwd=Path.cwd())
        return result.ok and bool(result.stdout.strip())

    def describe(self) -> str:
        if not self._runner.is_available("docker"):
            return "unavailable: the docker CLI is not installed"
        if not self.available():
            return "unavailable: the docker daemon is not responding"
        return f"docker, image {self.image}"

    def has_image(self) -> bool:
        """Whether :attr:`image` is already present on this host."""
        result = self._runner.run(
            ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"],
            cwd=Path.cwd(),
        )
        return result.ok and bool(result.stdout.strip())

    # -- argv construction (pure, so the hardening is unit-testable) -------

    def docker_argv(
        self,
        command: ValidationCommand,
        repo_root: Path,
        limits: SandboxLimits,
        *,
        path: str | None = None,
    ) -> list[str]:
        """Build the ``docker run`` argument vector for one validation command.

        Pure and side-effect free on purpose: the security properties of this
        feature *are* these flags, and a unit test asserts every one of them on
        a machine with no Docker installed.
        """
        argv = [
            "docker", "run", "--rm",
            # No network at all: validation must not reach a package index, a
            # telemetry endpoint, or anything on the host's network.
            "--network", "none",
            # Immutable root filesystem, with one small noexec scratch area so
            # tools that insist on a temp dir still work.
            "--read-only",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={min(limits.memory_mb, 256)}m",
            # Hard resource ceilings. memory-swap == memory disables swap, so a
            # runaway process cannot trade memory pressure for disk thrashing.
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", f"{limits.cpus:g}",
            "--pids-limit", str(limits.pids),
            # Privilege reduction: no capabilities, no setuid escalation, and an
            # unprivileged uid so a container escape lands as nobody.
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", f"{SANDBOX_UID}:{SANDBOX_GID}",
            # The repository is mounted read-only: validation reports on code,
            # it never rewrites it.
            "--volume", f"{repo_root}:{WORKSPACE}:ro",
            "--workdir", WORKSPACE,
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--label", "zdocs-ai=validation",
            # Never reach the network to fetch an image. A missing image is a
            # fast, explicit failure — not a silent multi-hundred-megabyte
            # download triggered by an analysis request.
            "--pull", "never",
            self.image,
            *command.argv,
        ]
        if path and command.accepts_path:
            argv.append(path)
        return argv

    # -- execution ---------------------------------------------------------

    def run(
        self,
        command: ValidationCommand,
        repo_root: Path,
        limits: SandboxLimits,
        *,
        path: str | None = None,
    ) -> SandboxResult:
        """Run ``command`` in a container and return its captured result."""
        if not self.available():
            raise SandboxUnavailable(self.describe())

        argv = self.docker_argv(command, repo_root, limits, path=path)
        if not self.has_image():
            raise SandboxUnavailable(
                f"container image {self.image!r} is not present locally. "
                f"Pull it once with `docker pull {self.image}` — validation runs "
                f"never fetch images themselves."
            )
        result = self._runner.run(
            argv,
            cwd=repo_root,
            timeout=limits.timeout_seconds,
            max_output_bytes=limits.max_output_bytes,
        )
        return SandboxResult(
            tool=command.name,
            argv=list(command.argv) + ([path] if path and command.accepts_path else []),
            exit_code=result.exit_code,
            stdout=redact(result.stdout),
            stderr=redact(result.stderr),
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            truncated=result.truncated,
            backend=self.backend,
            image=self.image,
        )


def resolve_validation_command(name: str, allowed: frozenset[str]) -> ValidationCommand:
    """Look up an approved validation command, or raise :class:`PolicyViolation`."""
    command = VALIDATION_COMMANDS.get((name or "").strip())
    if command is None:
        raise PolicyViolation(
            f"Unknown validation tool {name!r}. Approved tools: "
            f"{', '.join(sorted(VALIDATION_COMMANDS))}"
        )
    if command.name not in allowed:
        raise PolicyViolation(
            f"Validation tool {command.name!r} is not permitted by the active policy."
        )
    return command


def build_sandbox(
    *,
    image: str | None = None,
    runner: CommandRunner | None = None,
) -> Sandbox:
    """Return the best available sandbox backend.

    Docker today; the :class:`Sandbox` protocol is the seam for adding another
    (Podman, gVisor, Firecracker, a remote runner) without touching the
    executor.
    """
    docker = DockerSandbox(image=image or DEFAULT_IMAGE, runner=runner)
    if docker.available():
        return docker
    return UnavailableSandbox(
        docker.describe().removeprefix("unavailable: ")
        or "no container runtime is available"
    )


__all__ = [
    "DEFAULT_IMAGE",
    "DockerSandbox",
    "SANDBOX_GID",
    "SANDBOX_UID",
    "Sandbox",
    "SandboxLimits",
    "SandboxResult",
    "SandboxUnavailable",
    "UnavailableSandbox",
    "VALIDATION_COMMANDS",
    "WORKSPACE",
    "ValidationCommand",
    "build_sandbox",
    "resolve_validation_command",
]
