"""Read-only git metadata for a staged repository.

Only the allowlisted, read-only subcommands in
:data:`operations.policy.GIT_READ_ONLY_SUBCOMMANDS` are used, and every
invocation goes through :class:`~operations.command_runner.CommandRunner`, so
the same timeout, output cap, redaction and no-network environment apply here
as everywhere else.

A repository staged from a ``.zip`` upload has no ``.git`` directory at all.
That is a normal, expected state: these helpers report ``is_git_repository:
False`` rather than raising, and callers degrade gracefully (the cache falls
back to a content fingerprint, for instance).
"""

from __future__ import annotations

from pathlib import Path

from operations.command_runner import CommandRunner
from operations.errors import ToolUnavailableError
from operations.policy import redact


def is_git_repository(root: Path) -> bool:
    """Whether ``root`` contains a git checkout (worktree file or directory)."""
    return (root / ".git").exists()


def commit_sha(root: Path, runner: CommandRunner) -> str | None:
    """Current ``HEAD`` SHA, or ``None`` when unavailable."""
    if not is_git_repository(root) or not runner.is_available("git"):
        return None
    result = runner.run(["git", "rev-parse", "HEAD"], cwd=root)
    if not result.ok:
        return None
    sha = result.stdout.strip()
    return sha or None


def git_metadata(root: Path, runner: CommandRunner) -> dict[str, object]:
    """Collect branch, HEAD, remotes, counts and recent commits.

    Returns a dict with ``is_git_repository`` and, when git is usable, the
    metadata fields. Failures of individual subcommands are reported in
    ``errors`` rather than aborting the whole operation.
    """
    if not is_git_repository(root):
        return {
            "is_git_repository": False,
            "reason": "No .git directory — repository was staged from an archive.",
        }
    if not runner.is_available("git"):
        raise ToolUnavailableError(
            "git is not installed; repository metadata cannot be read. "
            "Install git or use operations that do not require it."
        )

    metadata: dict[str, object] = {"is_git_repository": True}
    errors: list[str] = []

    def capture(name: str, argv: list[str], *, transform=lambda text: text.strip()) -> None:
        result = runner.run(argv, cwd=root)
        if result.ok:
            metadata[name] = transform(result.stdout)
        else:
            errors.append(f"{' '.join(argv)}: {redact(result.stderr.strip()) or 'failed'}")

    capture("head_commit", ["git", "rev-parse", "HEAD"])
    capture("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    capture(
        "remotes",
        ["git", "remote", "-v"],
        transform=lambda text: sorted(
            {line.split("\t")[0] for line in text.splitlines() if line.strip()}
        ),
    )
    capture(
        "commit_count",
        ["git", "rev-list", "--count", "HEAD"],
        transform=lambda text: int(text.strip() or 0),
    )
    capture(
        "tracked_file_count",
        ["git", "ls-files"],
        transform=lambda text: len([line for line in text.splitlines() if line.strip()]),
    )
    capture(
        "recent_commits",
        ["git", "log", "-10", "--date=short", "--pretty=format:%h%x09%ad%x09%s"],
        transform=_parse_log,
    )
    capture(
        "contributors",
        ["git", "shortlog", "-sn", "--all", "--no-merges"],
        transform=lambda text: [
            {"commits": int(parts[0]), "name": redact(parts[1])}
            for line in text.splitlines()
            if (parts := line.strip().split("\t", 1)) and len(parts) == 2 and parts[0].isdigit()
        ][:20],
    )

    if errors:
        metadata["errors"] = errors
    return metadata


def _parse_log(text: str) -> list[dict[str, str]]:
    """Parse tab-separated ``git log`` output into structured commits."""
    commits: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        commits.append(
            {
                "sha": parts[0].strip(),
                "date": parts[1].strip(),
                "subject": redact(parts[2].strip()),
            }
        )
    return commits


__all__ = ["commit_sha", "git_metadata", "is_git_repository"]
