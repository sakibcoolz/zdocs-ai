"""File-reader tool for the zdocs-ai agent.

Exposes files staged under a configurable base directory (default ``stage/``)
to the agent as callable functions. All paths are resolved and constrained to
the stage directory, so the tool can never read outside it (no path-traversal).

The module exposes two layers:

* ``read_file`` / ``list_files`` — the raw functions an agent calls.
* ``FileReaderTool`` — a factory that binds those functions to a specific
  stage directory and returns ADK ``FunctionTool`` instances ready to attach
  to an ``LlmAgent``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from google.adk.tools import FunctionTool

# Default location of the staging directory, relative to the project root.
DEFAULT_STAGE_DIR = "stage"


def _resolve_stage_dir(stage_dir: str | Path | None = None) -> Path:
    """Resolve the stage directory to an absolute, existing path.

    Raises ``FileNotFoundError`` if the directory does not exist so that a
    misconfigured stage dir fails loudly instead of silently reading nothing.
    """
    base = Path(stage_dir) if stage_dir is not None else Path(DEFAULT_STAGE_DIR)
    base = base.expanduser().resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"Stage directory does not exist: {base}")
    return base


def _safe_path(stage_dir: Path, filename: str) -> Path:
    """Resolve ``filename`` inside ``stage_dir`` and reject traversal escapes.

    Raises ``ValueError`` if the resolved path escapes the stage directory or
    is not a regular file.
    """
    candidate = (stage_dir / filename).resolve()
    # ensure_is_relative_to available on py3.9+; use commonprefix fallback.
    try:
        candidate.relative_to(stage_dir)
    except ValueError as exc:  # pragma: no cover - depends on Python version
        raise ValueError(f"Path escapes stage directory: {filename!r}") from exc

    if not candidate.is_file():
        raise FileNotFoundError(f"File not found in stage directory: {filename!r}")
    return candidate


def read_file(filename: str, stage_dir: str | Path | None = None) -> str:
    """Read a file from the stage directory and return its text content.

    Args:
        filename: Path of the file, relative to the stage directory.
        stage_dir: Optional override of the stage directory root.

    Returns:
        The decoded text contents of the file.
    """
    stage = _resolve_stage_dir(stage_dir)
    path = _safe_path(stage, filename)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")


def read_file_with_limit(
    filename: str,
    max_chars: int = 20_000,
    stage_dir: str | Path | None = None,
) -> str:
    """Read a file, truncating very large files with a clear marker.

    Useful as the primary agent-facing function so a single oversized file
    cannot flood the model context window.
    """
    content = read_file(filename, stage_dir=stage_dir)
    if len(content) > max_chars:
        head = content[:max_chars]
        return (
            f"# {filename} (truncated: showing {max_chars:,} of "
            f"{len(content):,} characters)\n\n{head}\n\n[... truncated ...]"
        )
    return content


def list_files(stage_dir: str | Path | None = None) -> str:
    """List files (and their sizes) available in the stage directory."""
    stage = _resolve_stage_dir(stage_dir)
    lines: list[str] = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            rel = path.relative_to(stage)
            lines.append(f"{rel} ({path.stat().st_size:,} bytes)")
    if not lines:
        return "No files in stage directory."
    return "\n".join(lines)


def FileReaderTool(stage_dir: str | Path | None = None) -> list[FunctionTool]:
    """Build the ADK tool set bound to ``stage_dir``.

    Returns a list of ``FunctionTool`` instances ready to pass to
    ``LlmAgent(tools=...)``.

    The free functions are bound to the resolved stage dir via small closures
    (using ``functools.wraps``) so the agent never controls the root, only the
    relative filename — while the wrapped functions still advertise their real
    ``__name__`` and ``__doc__`` to the model.

    ``__wrapped__`` (set by ``functools.wraps``) is deleted after wrapping:
    ``inspect.signature`` follows it by default, and ADK uses that signature
    to build the tool's schema — left in place, the model would see (and could
    pass) the wrapped function's ``stage_dir`` parameter, which the closure
    below doesn't actually accept.
    """
    from functools import wraps

    stage = str(_resolve_stage_dir(stage_dir))

    @wraps(read_file_with_limit)
    def _read_file(filename: str, max_chars: int = 20_000) -> str:
        return read_file_with_limit(filename, max_chars=max_chars, stage_dir=stage)

    del _read_file.__wrapped__

    @wraps(list_files)
    def _list_files() -> str:
        return list_files(stage_dir=stage)

    del _list_files.__wrapped__

    return [
        FunctionTool(_read_file),
        FunctionTool(_list_files),
    ]
