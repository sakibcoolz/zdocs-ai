"""Stage an already-in-hand zip (e.g. an uploaded file) for the agent.

Mirrors the on-disk layout ``tools.github_downloader`` produces
(``stage/<reponame>/<reponame>.zip`` plus the extracted files) but for zip
bytes that didn't come from a GitHub download. Reuses ``extract_zip`` so the
zip-slip guard is not duplicated.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from tools.github_downloader import _resolve_stage, extract_zip

_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FORBIDDEN_NAMES = {"", ".", ".."}


def repo_name_from_filename(filename: str) -> str:
    """Derive a safe ``stage/<reponame>`` name from an uploaded zip's filename.

    Discards any path components in ``filename`` and strips one ``.zip``
    suffix. Raises ``ValueError`` if the derived name is empty, ``.``, ``..``,
    or contains characters outside ``[A-Za-z0-9._-]``.
    """
    stem = Path(filename or "").name
    if stem.lower().endswith(".zip"):
        stem = stem[: -len(".zip")]
    if stem in _FORBIDDEN_NAMES or not _VALID_NAME_RE.match(stem):
        raise ValueError(f"Invalid or unsafe filename for staging: {filename!r}")
    return stem


def stage_zip_bytes(data: bytes, reponame: str, stage_dir: str | Path | None = None) -> Path:
    """Write ``data`` to ``stage/<reponame>/<reponame>.zip`` and extract it there.

    Returns the repo directory. Raises ``FileExistsError`` if ``reponame`` is
    already staged.
    """
    stage = _resolve_stage(stage_dir)
    repo_dir = stage / reponame
    repo_dir.mkdir(parents=True, exist_ok=False)
    zip_path = repo_dir / f"{reponame}.zip"
    zip_path.write_bytes(data)
    try:
        extract_zip(data, repo_dir)
    except Exception:
        # Don't leave a half-staged directory behind — it would permanently
        # block re-staging via the exist_ok=False guard above.
        shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    return repo_dir


def stage_uploaded_zip(
    data: bytes, filename: str, stage_dir: str | Path | None = None
) -> Path:
    """Stage an uploaded zip's bytes under a name derived from ``filename``."""
    return stage_zip_bytes(data, repo_name_from_filename(filename), stage_dir=stage_dir)
