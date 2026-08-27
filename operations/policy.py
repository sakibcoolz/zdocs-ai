"""Security policy for repository operations: what may run, and on what.

This module is the single place where "is this allowed?" is decided. It has no
dependency on FastAPI, the ADK, or any analyzer — it is pure validation so it
can be unit-tested in isolation and reused by any caller.

Three concerns live here:

1. **Path containment** (:func:`resolve_repo_path`) — every path an operation
   touches is re-resolved against the staged repository root and rejected if it
   is absolute, contains ``..``, or escapes via a symlink.
2. **Command allowlisting** (:meth:`ExecutionPolicy.check_command`) — only
   named, read-only executables may run, always as an argument array, never
   through a shell.
3. **Secret redaction** (:func:`redact`) — output and log lines are scrubbed
   before they reach a result, a log file or generated documentation.

Repository content is treated as untrusted input throughout: nothing read from
a file can widen the policy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath

from operations.errors import CommandNotAllowed, OperationNotAllowed, PathEscapeError
from operations.schemas import OperationType


class ExecutionProfile(str, Enum):
    """Named bundle of permissions an executor runs under."""

    #: Read-only repository analysis. Safe for automatic invocation by agents.
    REPOSITORY_ANALYSIS = "repository_analysis"
    #: Runs project tooling (tests/linters/type-checkers). Disabled by default;
    #: requires an isolated, disposable environment (see ``docs/ARCHITECTURE.md``).
    DEVELOPMENT_VALIDATION = "development_validation"


# --------------------------------------------------------------------------
# Executable allowlist
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowedCommand:
    """One allowlisted executable and the constraints on invoking it."""

    name: str
    #: Flags/arguments starting with ``-`` must appear here (exact match, or
    #: the part before ``=`` for ``--flag=value`` forms). ``None`` disables
    #: flag checking for commands whose flag surface we fully control.
    allowed_flags: frozenset[str] | None = None
    timeout_seconds: float = 20.0
    description: str = ""


#: Read-only analysis executables. Every one is non-interactive, makes no
#: network requests as invoked here, and never modifies the repository.
ANALYSIS_COMMANDS: dict[str, AllowedCommand] = {
    "rg": AllowedCommand(
        name="rg",
        timeout_seconds=20.0,
        description="ripgrep — file discovery and candidate text search",
    ),
    "git": AllowedCommand(
        name="git",
        timeout_seconds=15.0,
        description="git CLI — read-only repository metadata",
    ),
    "ast-grep": AllowedCommand(
        name="ast-grep",
        timeout_seconds=30.0,
        description="ast-grep — structural pattern matching (optional)",
    ),
    "ctags": AllowedCommand(
        name="ctags",
        timeout_seconds=30.0,
        description="Universal Ctags — symbol index (optional)",
    ),
    "tree-sitter": AllowedCommand(
        name="tree-sitter",
        timeout_seconds=30.0,
        description="tree-sitter CLI — syntax-tree parsing (optional)",
    ),
}

#: Read-only ``git`` subcommands. ``git`` is powerful enough that the
#: subcommand itself is allowlisted, not just the binary.
GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "rev-parse",
        "rev-list",
        "log",
        "show",
        "status",
        "branch",
        "remote",
        "ls-files",
        "ls-tree",
        "shortlog",
        "describe",
        "config",
    }
)

#: ``git`` global flags that change *which* repository is operated on. The
#: working directory is set by the executor; letting an argument override it
#: would defeat the path sandbox, so these are refused outright.
GIT_REDIRECT_FLAGS: frozenset[str] = frozenset(
    {"-C", "--git-dir", "--work-tree", "--exec-path", "--namespace", "--super-prefix"}
)

#: Never runnable, under any profile. Redundant with the allowlist — kept as a
#: second, explicit barrier so a future edit that widens the allowlist by
#: accident still cannot reach a destructive binary.
DENIED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "rm", "rmdir", "mv", "cp", "dd", "chmod", "chown", "chgrp", "ln",
        "kill", "pkill", "killall", "shutdown", "reboot", "halt", "poweroff",
        "sudo", "su", "doas", "eval", "exec", "sh", "bash", "zsh", "dash",
        "ksh", "fish", "csh", "env", "xargs", "find", "curl", "wget", "nc",
        "ncat", "ssh", "scp", "rsync", "ftp", "telnet", "pip", "pip3", "npm",
        "yarn", "pnpm", "apt", "apt-get", "yum", "dnf", "brew", "docker",
        "systemctl", "service", "mkfs", "mount", "umount", "crontab", "at",
        "python", "python3", "node", "perl", "ruby", "make",
    }
)

#: The only ``docker`` invocations permitted, matched as an exact leading
#: sequence of positional words. ``docker`` is reachable solely from
#: :meth:`ExecutionPolicy.sandbox_host`, and even there it is confined to these:
#: launching a container, probing the daemon, and checking whether an image is
#: present locally. Nothing that mutates any Docker state is included — note
#: that ``image`` alone is *not* allowed, or ``docker image rm`` would be.
DOCKER_ALLOWED_SUBCOMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {("run",), ("version",), ("info",), ("image", "inspect")}
)

#: ``docker run`` flags that would undo the sandbox. Every one of them hands the
#: container some part of the host's privileges, namespaces or devices.
DOCKER_DENIED_FLAGS: frozenset[str] = frozenset(
    {
        "--privileged", "--cap-add", "--device", "--devices", "--pid", "--ipc",
        "--uts", "--userns", "--sysctl", "--cgroup-parent", "--mount",
        "--volumes-from",
    }
)

#: Flags that mount a host path into the container.
DOCKER_VOLUME_FLAGS: frozenset[str] = frozenset({"-v", "--volume"})

#: Host paths that must never be mounted, read-only or otherwise. The docker
#: socket is the classic container escape; the rest expose host credentials.
DOCKER_FORBIDDEN_MOUNT_SOURCES: tuple[str, ...] = (
    "/", "/etc", "/root", "/home", "/var/run", "/run", "/proc", "/sys", "/dev",
    "/boot", "/usr", "/bin", "/sbin", "/lib", "/var/lib",
)

#: Characters that only mean something to a shell. We never use ``shell=True``,
#: so these are inert — but their presence in an argument is a strong signal
#: that a caller is trying to smuggle a command line, so we reject it.
_SHELL_METACHARACTERS = re.compile(r"[;&|`$><\n\r\x00]")


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

#: Validation tools permitted when the validation profile is switched on
#: without an explicit list. Read-only checkers only.
DEFAULT_VALIDATION_TOOLS: frozenset[str] = frozenset(
    {"pytest", "ruff", "mypy", "go-vet", "go-build", "tsc", "eslint"}
)

_ANALYSIS_OPERATIONS: frozenset[OperationType] = frozenset(
    op for op in OperationType if op is not OperationType.RUN_STATIC_ANALYSIS
)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Immutable permission set for one executor.

    Construct via :meth:`repository_analysis` or
    :meth:`development_validation`; use :meth:`with_overrides` to tune limits
    without losing the profile's guarantees.
    """

    profile: ExecutionProfile
    allowed_operations: frozenset[OperationType]
    allowed_commands: dict[str, AllowedCommand] = field(default_factory=dict)
    #: Executables refused before the allowlist is even consulted. Narrowed in
    #: exactly one place — :meth:`sandbox_host` — and never for analysis.
    denied_executables: frozenset[str] = DENIED_EXECUTABLES
    #: Wall-clock cap applied to any subprocess, on top of each command's own.
    command_timeout_seconds: float = 20.0
    #: Captured stdout/stderr is truncated past this many bytes.
    max_output_bytes: int = 2_000_000
    #: Largest single source file that will be read or parsed.
    max_file_bytes: int = 2_000_000
    #: Upper bound on files walked/parsed by one operation.
    max_files_scanned: int = 20_000
    #: Upper bound on matches returned by one operation.
    max_matches: int = 2_000
    #: Symlinks inside the repository are never followed by default.
    follow_symlinks: bool = False
    #: Analysis never needs the network. Recorded here and enforced by the
    #: command allowlist (no network-capable binary is allowlisted).
    network_enabled: bool = False
    #: Write access to the repository is never granted.
    read_only: bool = True
    #: The development-validation profile is inert until this is switched on.
    validation_enabled: bool = False
    #: Validation tools this policy permits (names from
    #: :data:`operations.sandbox.VALIDATION_COMMANDS`). Empty for analysis.
    validation_tools: frozenset[str] = frozenset()

    # -- factories ---------------------------------------------------------

    @classmethod
    def repository_analysis(cls) -> ExecutionPolicy:
        """Read-only analysis profile — safe for automatic agent invocation."""
        return cls(
            profile=ExecutionProfile.REPOSITORY_ANALYSIS,
            allowed_operations=_ANALYSIS_OPERATIONS,
            allowed_commands=dict(ANALYSIS_COMMANDS),
        )

    @classmethod
    def development_validation(
        cls, *, enabled: bool = False, tools: frozenset[str] | None = None
    ) -> ExecutionPolicy:
        """Development-validation profile. **Disabled by default.**

        The interface exists so tests/linters/type-checkers can be added later,
        Even when ``enabled``, nothing runs on the host: an isolated container
        sandbox must also be available (see :mod:`operations.sandbox`), and the
        operation fails loudly if it is not.

        Args:
            enabled: Switch the profile on. Off by default.
            tools: Validation tool names to permit. Defaults to
                :data:`DEFAULT_VALIDATION_TOOLS`.
        """
        return cls(
            profile=ExecutionProfile.DEVELOPMENT_VALIDATION,
            allowed_operations=_ANALYSIS_OPERATIONS | {OperationType.RUN_STATIC_ANALYSIS},
            allowed_commands=dict(ANALYSIS_COMMANDS),
            validation_enabled=enabled,
            validation_tools=frozenset(tools) if tools is not None else DEFAULT_VALIDATION_TOOLS,
        )

    @classmethod
    def sandbox_host(
        cls, *, timeout_seconds: float = 180.0, max_output_bytes: int = 1_000_000
    ) -> ExecutionPolicy:
        """Policy for *launching* the validation sandbox. Not an analysis policy.

        This is the only policy in the system that permits ``docker``, and it
        permits nothing else: no operations run under it, every other
        executable stays denied, and ``docker`` itself is confined to
        :data:`DOCKER_ALLOWED_SUBCOMMANDS` with :data:`DOCKER_DENIED_FLAGS`
        refused.

        It exists so that launching a container reuses the same argv
        validation, timeout, output cap and secret redaction as every other
        command, instead of a second hand-rolled ``subprocess`` call. The
        analysis profiles are unaffected and continue to deny ``docker``.
        """
        return cls(
            profile=ExecutionProfile.DEVELOPMENT_VALIDATION,
            allowed_operations=frozenset(),
            allowed_commands={
                "docker": AllowedCommand(
                    name="docker",
                    timeout_seconds=timeout_seconds,
                    description="Launch a disposable validation container",
                )
            },
            denied_executables=DENIED_EXECUTABLES - {"docker"},
            command_timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    def with_overrides(self, **kwargs: object) -> ExecutionPolicy:
        """Return a copy with individual limits replaced."""
        return replace(self, **kwargs)  # type: ignore[arg-type]

    # -- checks ------------------------------------------------------------

    def check_operation(self, operation: OperationType) -> None:
        """Raise :class:`OperationNotAllowed` unless ``operation`` is enabled."""
        if operation not in self.allowed_operations:
            raise OperationNotAllowed(
                f"Operation {operation.value!r} is not permitted by profile "
                f"{self.profile.value!r}"
            )
        if operation is OperationType.RUN_STATIC_ANALYSIS and not self.validation_enabled:
            raise OperationNotAllowed(
                "The development-validation profile is disabled; "
                "run_static_analysis is unavailable"
            )

    def check_command(self, argv: list[str]) -> AllowedCommand:
        """Validate an argument array and return its allowlist entry.

        Raises :class:`CommandNotAllowed` for an empty argv, a path-qualified
        or denied executable, an executable that is not allowlisted, an
        argument containing shell metacharacters, or a ``git`` subcommand that
        is not read-only.
        """
        if not argv:
            raise CommandNotAllowed("Empty command")

        program = argv[0]
        if "/" in program or "\\" in program:
            raise CommandNotAllowed(
                f"Executable must be a bare allowlisted name, got {program!r}"
            )
        if program in self.denied_executables:
            raise CommandNotAllowed(f"Executable is explicitly denied: {program!r}")

        allowed = self.allowed_commands.get(program)
        if allowed is None:
            raise CommandNotAllowed(f"Executable is not allowlisted: {program!r}")

        for arg in argv[1:]:
            if not isinstance(arg, str):
                raise CommandNotAllowed(f"Non-string argument: {arg!r}")
            if _SHELL_METACHARACTERS.search(arg):
                raise CommandNotAllowed(
                    f"Argument contains shell metacharacters: {arg!r}"
                )
            if allowed.allowed_flags is not None and arg.startswith("-"):
                flag = arg.split("=", 1)[0]
                if flag not in allowed.allowed_flags:
                    raise CommandNotAllowed(f"Flag not allowed for {program}: {arg!r}")

        if program == "docker":
            self._check_docker(argv)

        if program == "git":
            for arg in argv[1:]:
                if arg.split("=", 1)[0] in GIT_REDIRECT_FLAGS:
                    raise CommandNotAllowed(
                        f"git flag would redirect the repository location: {arg!r}"
                    )
            subcommand = next((a for a in argv[1:] if not a.startswith("-")), None)
            if subcommand not in GIT_READ_ONLY_SUBCOMMANDS:
                raise CommandNotAllowed(
                    f"git subcommand is not read-only/allowlisted: {subcommand!r}"
                )

        return allowed

    def _check_docker(self, argv: list[str]) -> None:
        """Validate a ``docker`` invocation: subcommand, flags and every mount.

        Blanket-denying volume flags would be useless — the sandbox needs one
        mount to do its job. So the *mount itself* is checked: it must be
        read-only, and it must not expose the docker socket or any host
        directory holding credentials or devices.
        """
        arguments = argv[1:]
        for index, argument in enumerate(arguments):
            flag, _, inline_value = argument.partition("=")
            if flag in DOCKER_DENIED_FLAGS:
                raise CommandNotAllowed(
                    f"docker flag would defeat the sandbox: {argument!r}"
                )
            if flag in DOCKER_VOLUME_FLAGS:
                value = inline_value or (
                    arguments[index + 1] if index + 1 < len(arguments) else ""
                )
                self._check_docker_mount(value)
            if flag == "--security-opt" and "unconfined" in (
                inline_value
                or (arguments[index + 1] if index + 1 < len(arguments) else "")
            ):
                raise CommandNotAllowed(
                    f"docker security-opt would disable confinement: {argument!r}"
                )

        positional = tuple(a for a in arguments if not a.startswith("-"))
        if not any(
            positional[: len(allowed)] == allowed
            for allowed in DOCKER_ALLOWED_SUBCOMMANDS
        ):
            raise CommandNotAllowed(
                f"docker subcommand is not allowlisted: {' '.join(positional[:2]) or None!r}"
            )

    @staticmethod
    def _check_docker_mount(value: str) -> None:
        """Require a bind mount to be read-only and to expose nothing sensitive."""
        parts = value.split(":")
        if len(parts) < 3:
            raise CommandNotAllowed(
                f"docker mount must specify a source, target and options: {value!r}"
            )
        source, _target, options = parts[0], parts[1], parts[2]
        if "ro" not in options.split(","):
            raise CommandNotAllowed(f"docker mount must be read-only: {value!r}")
        normalized = os.path.normpath(source)
        if normalized in DOCKER_FORBIDDEN_MOUNT_SOURCES or normalized.startswith(
            ("/var/run/", "/run/", "/proc/", "/sys/", "/dev/")
        ):
            raise CommandNotAllowed(
                f"docker mount source is not permitted: {source!r}"
            )

    def timeout_for(self, argv: list[str]) -> float:
        """Effective timeout for ``argv``: the stricter of command and policy."""
        allowed = self.allowed_commands.get(argv[0]) if argv else None
        per_command = allowed.timeout_seconds if allowed else self.command_timeout_seconds
        return min(per_command, self.command_timeout_seconds)

    def subprocess_env(self) -> dict[str, str]:
        """Minimal, non-interactive environment for allowlisted subprocesses.

        Secrets from the parent process (API keys, tokens) are deliberately not
        forwarded, and every knob that could make a command prompt, page, or
        reach the network is disabled.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            # Non-interactive: never open a pager, an editor or a credential prompt.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_PAGER": "cat",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GCM_INTERACTIVE": "never",
            "PAGER": "cat",
            "EDITOR": "false",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        if "SYSTEMROOT" in os.environ:  # pragma: no cover - Windows only
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env


# --------------------------------------------------------------------------
# Path containment
# --------------------------------------------------------------------------


def resolve_repo_root(repo_root: str | Path) -> Path:
    """Resolve a staged repository root, raising if it is not a directory."""
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Repository directory does not exist: {root}")
    return root


def resolve_repo_path(
    repo_root: Path,
    relative_path: str,
    *,
    follow_symlinks: bool = False,
    must_exist: bool = True,
) -> Path:
    """Resolve ``relative_path`` inside ``repo_root`` or raise.

    Rejects, in order: NUL bytes, absolute paths (POSIX or Windows-style),
    any ``..`` component, symlinked components when ``follow_symlinks`` is
    ``False``, and anything that still resolves outside the root.

    Args:
        repo_root: Already-resolved repository root.
        relative_path: Untrusted, repository-relative path.
        follow_symlinks: Whether symlinks inside the repo may be traversed.
            Even when ``True``, the resolved target must remain inside the root.
        must_exist: Require the resolved path to exist.

    Returns:
        The absolute, contained path.

    Raises:
        PathEscapeError: for any of the rejections above.
        FileNotFoundError: when ``must_exist`` and the path is absent.
    """
    raw = (relative_path or "").strip()
    if not raw:
        raise PathEscapeError("Empty path")
    if "\x00" in raw:
        raise PathEscapeError("Path contains a NUL byte")

    normalized = raw.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PathEscapeError(f"Absolute paths are not permitted: {relative_path!r}")
    if any(part == ".." for part in pure.parts):
        raise PathEscapeError(f"Path traversal is not permitted: {relative_path!r}")

    candidate = repo_root.joinpath(*(p for p in pure.parts if p not in ("", ".")))

    if not follow_symlinks:
        # Walk each component: a symlink anywhere on the way in is refused
        # outright, which is stricter (and easier to reason about) than only
        # checking where it points.
        walked = repo_root
        for part in pure.parts:
            if part in ("", "."):
                continue
            walked = walked / part
            if walked.is_symlink():
                raise PathEscapeError(
                    f"Symlinked path components are not permitted: {relative_path!r}"
                )

    resolved = candidate.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise PathEscapeError(
            f"Path escapes the repository root: {relative_path!r}"
        ) from exc

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"Path not found in repository: {relative_path!r}")
    return resolved


def relative_to_repo(repo_root: Path, path: Path) -> str:
    """Repository-relative POSIX path for ``path`` (falls back to the name)."""
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:  # pragma: no cover - defensive
        return path.name


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------

REDACTED = "[REDACTED]"

#: Ordered high-signal secret patterns. Deliberately conservative: these match
#: well-known credential shapes rather than "any long string", so redaction
#: does not mangle ordinary source code.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    # key = "value" / key: 'value' / KEY=value for credential-ish names.
    re.compile(
        r"(?i)\b([A-Za-z0-9_.\-]*(?:api[_-]?key|secret|password|passwd|token|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret))\b"
        r"(\s*[:=]\s*)"
        r"(?!\[REDACTED\])"
        r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;)\]}\n]{4,})"
    ),
)


_REPEATED_REDACTION = re.compile(r"(?:\[REDACTED\]){2,}")


def redact(text: str) -> str:
    """Replace likely secrets in ``text`` with :data:`REDACTED`.

    Applied to every command output, evidence excerpt, audit log line and
    generated documentation fragment, so credentials committed to a repository
    never leak into results or docs.
    """
    if not text:
        return text
    # The key/value rule runs first: it consumes the whole assignment, so a
    # later token-shaped pattern cannot re-match a fragment of an already
    # redacted value and leave stray delimiters behind.
    out = _SECRET_PATTERNS[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    for pattern in _SECRET_PATTERNS[:-1]:
        out = pattern.sub(REDACTED, out)
    return _REPEATED_REDACTION.sub(REDACTED, out)


def redact_arguments(arguments: dict[str, object]) -> dict[str, object]:
    """Redact string values inside an arguments mapping (for audit logs)."""
    return {
        key: redact(value) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
