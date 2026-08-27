"""Security tests for the operations policy: paths, commands, redaction.

These are the tests that matter most: they assert the Repository Operations
Agent cannot escape the staged repository, cannot run an unapproved command,
cannot hang, cannot flood a caller with output, and cannot leak a credential
into a result or a log line.

Everything here runs offline and makes no LLM calls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from operations.command_runner import CommandResult, CommandRunner
from operations.errors import (
    CommandNotAllowed,
    CommandTimeout,
    OperationNotAllowed,
    PathEscapeError,
)
from operations.policy import (
    DENIED_EXECUTABLES,
    ExecutionPolicy,
    ExecutionProfile,
    redact,
    redact_arguments,
    resolve_repo_path,
    resolve_repo_root,
)
from operations.schemas import OperationType


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository directory with one file and an outside-secret neighbour."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    return resolve_repo_root(root)


# --------------------------------------------------------------------------
# Path containment
# --------------------------------------------------------------------------


def test_resolves_a_contained_path(repo: Path) -> None:
    resolved = resolve_repo_path(repo, "pkg/main.py")
    assert resolved == repo / "pkg" / "main.py"


def test_rejects_parent_traversal(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "../secret.txt")


def test_rejects_nested_traversal(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "pkg/../../secret.txt")


def test_rejects_absolute_posix_path(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "/etc/passwd")


def test_rejects_absolute_path_to_a_real_outside_file(repo: Path) -> None:
    outside = repo.parent / "secret.txt"
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, str(outside))


def test_rejects_windows_style_absolute_path(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, r"C:\\Windows\\system.ini")


def test_rejects_backslash_traversal(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, r"..\\secret.txt")


def test_rejects_nul_byte(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "pkg/main.py\x00.txt")


def test_rejects_empty_path(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "   ")


def test_rejects_symlink_escaping_the_repository(repo: Path) -> None:
    link = repo / "escape.txt"
    link.symlink_to(repo.parent / "secret.txt")
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "escape.txt")


def test_rejects_symlinked_directory_component(repo: Path) -> None:
    (repo / "linkdir").symlink_to(repo.parent, target_is_directory=True)
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "linkdir/secret.txt")


def test_rejects_symlink_that_stays_inside_when_following_is_disabled(repo: Path) -> None:
    # Even a harmless internal symlink is refused by default: allowing them
    # would mean auditing every link target, and analysis never needs them.
    (repo / "alias.py").symlink_to(repo / "pkg" / "main.py")
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "alias.py")


def test_follows_internal_symlink_only_when_explicitly_enabled(repo: Path) -> None:
    (repo / "alias.py").symlink_to(repo / "pkg" / "main.py")
    resolved = resolve_repo_path(repo, "alias.py", follow_symlinks=True)
    assert resolved == repo / "pkg" / "main.py"


def test_following_symlinks_still_rejects_escape(repo: Path) -> None:
    (repo / "escape.txt").symlink_to(repo.parent / "secret.txt")
    with pytest.raises(PathEscapeError):
        resolve_repo_path(repo, "escape.txt", follow_symlinks=True)


def test_missing_file_raises_file_not_found(repo: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_repo_path(repo, "pkg/nope.py")


def test_missing_repo_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_repo_root(tmp_path / "absent")


# --------------------------------------------------------------------------
# Operation allowlist
# --------------------------------------------------------------------------


def test_analysis_profile_permits_read_only_operations() -> None:
    policy = ExecutionPolicy.repository_analysis()
    policy.check_operation(OperationType.ANALYZE_OOP)
    policy.check_operation(OperationType.FIND_CLASS)


def test_analysis_profile_refuses_static_analysis() -> None:
    policy = ExecutionPolicy.repository_analysis()
    with pytest.raises(OperationNotAllowed):
        policy.check_operation(OperationType.RUN_STATIC_ANALYSIS)


def test_validation_profile_is_disabled_by_default() -> None:
    policy = ExecutionPolicy.development_validation()
    assert policy.profile is ExecutionProfile.DEVELOPMENT_VALIDATION
    assert policy.validation_enabled is False
    with pytest.raises(OperationNotAllowed):
        policy.check_operation(OperationType.RUN_STATIC_ANALYSIS)


def test_validation_profile_can_be_enabled_explicitly() -> None:
    policy = ExecutionPolicy.development_validation(enabled=True)
    policy.check_operation(OperationType.RUN_STATIC_ANALYSIS)


def test_policy_is_immutable() -> None:
    policy = ExecutionPolicy.repository_analysis()
    with pytest.raises(Exception):
        policy.max_output_bytes = 1  # type: ignore[misc]


def test_with_overrides_keeps_the_profile() -> None:
    policy = ExecutionPolicy.repository_analysis().with_overrides(max_matches=5)
    assert policy.max_matches == 5
    assert policy.profile is ExecutionProfile.REPOSITORY_ANALYSIS
    assert policy.read_only is True
    assert policy.network_enabled is False


# --------------------------------------------------------------------------
# Command allowlist
# --------------------------------------------------------------------------


@pytest.fixture()
def policy() -> ExecutionPolicy:
    return ExecutionPolicy.repository_analysis()


def test_allows_an_allowlisted_command(policy: ExecutionPolicy) -> None:
    assert policy.check_command(["rg", "--files"]).name == "rg"


def test_rejects_empty_command(policy: ExecutionPolicy) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command([])


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],
        ["mv", "a", "b"],
        ["chmod", "777", "."],
        ["chown", "root", "."],
        ["kill", "-9", "1"],
        ["shutdown", "now"],
        ["reboot"],
        ["eval", "ls"],
        ["sudo", "rg", "--files"],
        ["bash", "-c", "ls"],
        ["sh", "-c", "ls"],
        ["curl", "http://example.com"],
        ["pip", "install", "requests"],
        ["python", "-c", "print(1)"],
    ],
)
def test_rejects_destructive_and_unapproved_commands(
    policy: ExecutionPolicy, argv: list[str]
) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command(argv)


def test_every_denied_executable_is_refused(policy: ExecutionPolicy) -> None:
    for program in sorted(DENIED_EXECUTABLES):
        with pytest.raises(CommandNotAllowed):
            policy.check_command([program])


def test_rejects_path_qualified_executable(policy: ExecutionPolicy) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["/usr/bin/rg", "--files"])
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["./rg"])


@pytest.mark.parametrize(
    "argument",
    ["a;rm -rf /", "a|b", "a&b", "$(whoami)", "`id`", "a>b", "a<b", "a\nb", "a\x00b"],
)
def test_rejects_shell_metacharacters(policy: ExecutionPolicy, argument: str) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["rg", argument])


def test_rejects_non_string_argument(policy: ExecutionPolicy) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["rg", 5])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "subcommand", ["push", "commit", "clean", "reset", "checkout", "fetch", "clone"]
)
def test_rejects_write_or_network_git_subcommands(
    policy: ExecutionPolicy, subcommand: str
) -> None:
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["git", subcommand])


def test_allows_read_only_git_subcommands(policy: ExecutionPolicy) -> None:
    policy.check_command(["git", "rev-parse", "HEAD"])
    policy.check_command(["git", "log", "-1", "--pretty=format:%h"])


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-C", "/etc", "log"],
        ["git", "--git-dir=/etc/.git", "log"],
        ["git", "--work-tree", "/", "status"],
    ],
)
def test_rejects_git_flags_that_redirect_the_repository(
    policy: ExecutionPolicy, argv: list[str]
) -> None:
    # The executor pins cwd; an argument must never be able to move it.
    with pytest.raises(CommandNotAllowed):
        policy.check_command(argv)


def test_subprocess_env_is_non_interactive_and_carries_no_secrets(
    policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-forwarded")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaShouldNotBeForwarded")
    env = policy.subprocess_env()
    assert "OPENAI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PAGER"] == "cat"
    assert env["EDITOR"] == "false"


def test_timeout_is_the_stricter_of_command_and_policy(policy: ExecutionPolicy) -> None:
    tightened = policy.with_overrides(command_timeout_seconds=1.0)
    assert tightened.timeout_for(["rg", "--files"]) == 1.0
    assert policy.timeout_for(["git", "log"]) <= policy.command_timeout_seconds


# --------------------------------------------------------------------------
# CommandRunner: timeout, truncation, no shell
# --------------------------------------------------------------------------


def test_runner_never_uses_a_shell(
    tmp_path: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(CommandRunner, "is_available", lambda self, program: True)

    runner = CommandRunner(policy)
    result = runner.run(["rg", "--files"], cwd=tmp_path)

    assert result.stdout == "ok"
    assert seen["shell"] is False
    assert isinstance(seen["argv"], list)
    assert seen["stdin"] is subprocess.DEVNULL


def test_runner_reports_a_timeout(
    tmp_path: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1), output=b"partial")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(CommandRunner, "is_available", lambda self, program: True)

    runner = CommandRunner(policy)
    result = runner.run(["rg", "--files"], cwd=tmp_path)
    assert result.timed_out is True
    assert result.ok is False
    assert result.exit_code == -1


def test_runner_raises_on_timeout_when_check_is_set(
    tmp_path: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(argv, 0.01)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(CommandRunner, "is_available", lambda self, program: True)

    with pytest.raises(CommandTimeout):
        CommandRunner(policy).run(["rg", "--files"], cwd=tmp_path, check=True)


def test_runner_truncates_oversized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ExecutionPolicy.repository_analysis().with_overrides(max_output_bytes=64)

    def fake_run(argv, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(argv, 0, b"A" * 10_000, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(CommandRunner, "is_available", lambda self, program: True)

    result = CommandRunner(policy).run(["rg", "--files"], cwd=tmp_path)
    assert result.truncated is True
    assert "output truncated" in result.stdout
    assert result.stdout.count("A") == 64


def test_runner_redacts_secrets_in_output(
    tmp_path: Path, policy: ExecutionPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(
            argv, 0, b'config.py:api_key = "sk-abcdefghijklmnopqrstuvwxyz"\n', b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(CommandRunner, "is_available", lambda self, program: True)

    result = CommandRunner(policy).run(["rg", "secret"], cwd=tmp_path)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.stdout
    assert "[REDACTED]" in result.stdout


def test_runner_refuses_an_unapproved_command(tmp_path: Path, policy: ExecutionPolicy) -> None:
    with pytest.raises(CommandNotAllowed):
        CommandRunner(policy).run(["rm", "-rf", "."], cwd=tmp_path)


def test_runner_reports_a_missing_tool(tmp_path: Path, policy: ExecutionPolicy) -> None:
    from operations.errors import ToolUnavailableError

    runner = CommandRunner(policy)
    if runner.is_available("ctags"):  # pragma: no cover - depends on the machine
        pytest.skip("ctags is installed on this machine")
    with pytest.raises(ToolUnavailableError):
        runner.run(["ctags", "-R"], cwd=tmp_path)


def test_command_result_ok_property() -> None:
    assert CommandResult(exit_code=0).ok is True
    assert CommandResult(exit_code=1).ok is False
    assert CommandResult(exit_code=0, timed_out=True).ok is False


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        'api_key = "sk-abcdefghijklmnopqrstuvwxyz"',
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        "password: hunter2hunter2",
        "client_secret='abcdefghijklmnop'",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
        "xoxb-123456789012-abcdefghijkl",
        "AIzaSy012345678901234567890123456789abc",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_redacts_known_secret_shapes(secret: str) -> None:
    redacted = redact(secret)
    assert "[REDACTED]" in redacted
    assert secret not in redacted


def test_redacts_private_key_blocks() -> None:
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
    )
    assert redact(key) == "[REDACTED]"


def test_does_not_mangle_ordinary_source_code() -> None:
    code = "def compute(total: int) -> int:\n    return total * 2  # simple\n"
    assert redact(code) == code


def test_redaction_is_idempotent() -> None:
    once = redact('api_key = "sk-abcdefghijklmnopqrstuvwxyz"')
    assert redact(once) == once


def test_redacts_argument_values_for_audit_logs() -> None:
    arguments = redact_arguments({"symbol": "ghp_abcdefghijklmnopqrstuvwxyz", "limit": 5})
    assert arguments["symbol"] == "[REDACTED]"
    assert arguments["limit"] == 5
