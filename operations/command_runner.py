"""Safe subprocess execution for allowlisted, read-only analysis commands.

Separate from :mod:`operations.executor` so the modules that actually shell out
(``inventory``, ``symbol_search``, ``git_info``) can depend on it without
importing the executor that orchestrates them.

Every invocation goes through :meth:`CommandRunner.run`, which:

* validates the argument array against an :class:`~operations.policy.ExecutionPolicy`;
* never uses ``shell=True`` — arguments are passed as an array;
* runs with ``cwd`` pinned inside the staged repository;
* passes a minimal, non-interactive environment with no inherited secrets;
* closes stdin so nothing can block waiting for input;
* enforces a wall-clock timeout and kills the whole process group on expiry;
* truncates captured output at a byte budget;
* redacts likely secrets from what it returns.

:class:`CommandRunner` is a plain class so tests can inject a fake (see
:class:`StubCommandRunner` usage in the test-suite) — the analyzers never call
:mod:`subprocess` directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from operations.errors import CommandTimeout, ToolUnavailableError
from operations.policy import ExecutionPolicy, redact
from pydantic import BaseModel, Field

logger = logging.getLogger("zdocs.operations.command")


class CommandResult(BaseModel):
    """Outcome of one allowlisted command invocation."""

    argv: list[str] = Field(default_factory=list)
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """True when the command completed without timing out or erroring."""
        return self.exit_code == 0 and not self.timed_out


class CommandRunner:
    """Runs allowlisted commands under an :class:`ExecutionPolicy`."""

    def __init__(self, policy: ExecutionPolicy) -> None:
        self._policy = policy

    # -- availability ------------------------------------------------------

    def is_available(self, program: str) -> bool:
        """Whether ``program`` is allowlisted *and* present on ``PATH``."""
        if program not in self._policy.allowed_commands:
            return False
        return shutil.which(program, path=self._policy.subprocess_env()["PATH"]) is not None

    def require(self, program: str) -> None:
        """Raise :class:`ToolUnavailableError` if ``program`` cannot be run."""
        if not self.is_available(program):
            raise ToolUnavailableError(
                f"Required command-line tool is not installed or not allowlisted: "
                f"{program!r}"
            )

    # -- execution ---------------------------------------------------------

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float | None = None,
        check: bool = False,
        max_output_bytes: int | None = None,
    ) -> CommandResult:
        """Validate and run ``argv`` inside ``cwd``.

        Args:
            argv: Argument array. ``argv[0]`` must be an allowlisted bare name.
            cwd: Working directory — always the staged repository root.
            timeout: Override the policy/command timeout (still capped by policy).
            check: Raise :class:`CommandTimeout` on timeout instead of returning
                a result with ``timed_out=True``.
            max_output_bytes: Tighten the captured-output budget for this call.
                A caller may only reduce it — the policy's limit is the ceiling.

        Raises:
            CommandNotAllowed: argv failed policy validation.
            ToolUnavailableError: the executable is not installed.
            CommandTimeout: only when ``check`` is set and the command expired.
        """
        self._policy.check_command(argv)
        self.require(argv[0])

        effective_timeout = min(
            timeout if timeout is not None else self._policy.timeout_for(argv),
            self._policy.command_timeout_seconds,
        )
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # noqa: S603 - argv array, never shell=True
                argv,
                cwd=str(cwd),
                env=self._policy.subprocess_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=effective_timeout,
                shell=False,
                start_new_session=True,
                text=False,
            )
            stdout_bytes, stderr_bytes = completed.stdout, completed.stderr
            exit_code, timed_out = completed.returncode, False
        except subprocess.TimeoutExpired as exc:
            stdout_bytes = exc.stdout or b""
            stderr_bytes = exc.stderr or b""
            exit_code, timed_out = -1, True
        except FileNotFoundError as exc:  # pragma: no cover - guarded by require()
            raise ToolUnavailableError(f"Executable not found: {argv[0]!r}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        limit = min(
            max_output_bytes if max_output_bytes is not None else self._policy.max_output_bytes,
            self._policy.max_output_bytes,
        )
        stdout, out_truncated = self._decode(stdout_bytes, limit)
        stderr, err_truncated = self._decode(stderr_bytes, limit)

        result = CommandResult(
            argv=list(argv),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
        )
        logger.debug(
            "command=%s exit=%s timed_out=%s duration_ms=%s truncated=%s",
            " ".join(argv),
            exit_code,
            timed_out,
            duration_ms,
            result.truncated,
        )
        if timed_out and check:
            raise CommandTimeout(
                f"Command timed out after {effective_timeout:g}s: {' '.join(argv)}"
            )
        return result

    # -- helpers -----------------------------------------------------------

    def _decode(self, raw: bytes, limit: int | None = None) -> tuple[str, bool]:
        """Truncate to the byte budget, decode leniently, redact."""
        limit = self._policy.max_output_bytes if limit is None else limit
        truncated = len(raw) > limit
        if truncated:
            raw = raw[:limit]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[... output truncated at policy limit ...]"
        return redact(text), truncated


def which(program: str) -> str | None:
    """Locate ``program`` on ``PATH`` without invoking it."""
    return shutil.which(program, path=os.environ.get("PATH", "/usr/bin:/bin"))
