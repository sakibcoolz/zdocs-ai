"""Read-side lookups over the stage directory: what repos are staged.

Kept separate from the write paths (``github_downloader``, ``zip_stager``) so
a future non-local storage backend (e.g. MinIO) can reimplement just this
module's three functions without touching ``server.py`` or ``agent.py``.
"""

from __future__ import annotations

from pathlib import Path

from tools.github_downloader import _resolve_stage


def list_staged_repos(stage_dir: str | Path | None = None) -> list[str]:
    """Sorted names of repo directories under the stage directory."""
    stage = _resolve_stage(stage_dir)
    return sorted(p.name for p in stage.iterdir() if p.is_dir())


def is_staged(reponame: str, stage_dir: str | Path | None = None) -> bool:
    """Whether ``reponame`` is a staged repo directory."""
    try:
        staged_repo_dir(reponame, stage_dir=stage_dir)
    except (FileNotFoundError, ValueError):
        return False
    return True


def staged_repo_dir(reponame: str, stage_dir: str | Path | None = None) -> Path:
    """Resolve ``reponame`` to its directory inside the stage directory.

    Raises ``ValueError`` if ``reponame`` would escape the stage directory
    (it may arrive from a URL path segment and must not be trusted blindly —
    same guard pattern as ``tools.file_reader._safe_path``), and
    ``FileNotFoundError`` if it is not a staged directory.
    """
    stage = _resolve_stage(stage_dir)
    candidate = (stage / reponame).resolve()
    try:
        candidate.relative_to(stage)
    except ValueError as exc:
        raise ValueError(f"Repo name escapes stage directory: {reponame!r}") from exc
    if not candidate.is_dir():
        raise FileNotFoundError(f"Repo not staged: {reponame!r}")
    return candidate
