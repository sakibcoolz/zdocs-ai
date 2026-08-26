"""Download a GitHub repository as a zip and stage it for the agent.

Fetches the codeload zip for a ``github.com/<owner>/<repo>`` URL, saves it to
``stage/<repo>/<repo>.zip``, and extracts it into ``stage/<repo>/`` with the
GitHub ``{repo}-{ref}/`` wrapper folder stripped. Extraction guards against
zip-slip (entries that would write outside the destination directory).
"""

from __future__ import annotations

import io
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?(?:\.git)?/?$"
)

_FORBIDDEN_NAMES = {"", ".", ".."}

_ARCHIVE_BASE = "https://codeload.github.com"


def parse_repo(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` for a GitHub repo URL; raise ValueError otherwise."""
    match = _GITHUB_REPO_RE.match((url or "").strip())
    if not match:
        raise ValueError(f"Not a GitHub repository URL: {url!r}")
    owner = match.group("owner")
    repo = match.group("repo").removesuffix(".git")
    if owner in _FORBIDDEN_NAMES or repo in _FORBIDDEN_NAMES:
        raise ValueError(f"Invalid GitHub repository URL: {url!r}")
    return owner, repo


def _zip_url(owner: str, repo: str, branch: str | None = None) -> str:
    ref = branch or "HEAD"
    return f"{_ARCHIVE_BASE}/{owner}/{repo}/zip/{ref}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _common_prefix(names: list[str]) -> str:
    """Return the shared top-level directory (e.g. ``"repo-main/"``) of all
    entry names, or ``""`` if they don't all share one first path segment.

    Only a whole leading path segment counts as a wrapper — never a partial
    string match — so a lone entry like ``a.txt`` (no ``/``) or a mismatched
    set of top-level names never gets misidentified as sharing a directory.
    """
    if not names:
        return ""
    first_segments = set()
    for name in names:
        if "/" not in name:
            return ""
        first_segments.add(name.split("/", 1)[0])
    if len(first_segments) != 1:
        return ""
    return next(iter(first_segments)) + "/"


def extract_zip(data: bytes, dest: Path) -> None:
    """Extract ``data`` (zip bytes) into ``dest``, stripping the common top-level dir.

    Raises ``ValueError`` if any entry would write outside ``dest`` (zip-slip).
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        prefix = _common_prefix([n for n in zf.namelist() if not n.endswith("/")])
        for member in zf.infolist():
            rel = member.filename
            if prefix and rel.startswith(prefix):
                rel = rel[len(prefix):].lstrip("/")
            if not rel or member.is_dir():
                continue
            target = (dest / rel).resolve()
            try:
                target.relative_to(dest)
            except ValueError as exc:
                raise ValueError(
                    f"Zip entry escapes destination: {member.filename!r}"
                ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _default_stage_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "stage"


def _resolve_stage(stage_dir: str | Path | None = None) -> Path:
    stage = (Path(stage_dir) if stage_dir is not None else _default_stage_dir()).resolve()
    if not stage.is_dir():
        raise FileNotFoundError(f"Stage directory does not exist: {stage}")
    return stage


def download_zip(
    url: str,
    stage_dir: str | Path | None = None,
    branch: str | None = None,
) -> Path:
    """Download a repo zip and save it to ``stage/<repo>/<repo>.zip``.

    Returns the zip file path. Raises ``FileExistsError`` if the repo-named
    directory already exists.
    """
    owner, repo = parse_repo(url)
    stage = _resolve_stage(stage_dir)
    repo_dir = stage / repo
    repo_dir.mkdir(parents=True, exist_ok=False)
    zip_path = repo_dir / f"{repo}.zip"
    try:
        zip_path.write_bytes(_fetch(_zip_url(owner, repo, branch)))
    except Exception:
        # A failed fetch (e.g. GitHub 404 for a bad repo/branch, a network
        # error) must not leave a bare, empty repo_dir behind — it would
        # permanently block retrying this repo via the exist_ok=False guard
        # above.
        shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    return zip_path


def download_repo(
    url: str,
    stage_dir: str | Path | None = None,
    branch: str | None = None,
) -> Path:
    """Download a GitHub repo zip, unzip it into ``stage/<repo>/``, return that dir.

    Raises ``FileExistsError`` if already staged, ``ValueError`` for a non-GitHub
    URL, and ``ValueError`` if the archive contains a zip-slip entry.
    """
    repo_dir = _resolve_stage(stage_dir) / parse_repo(url)[1]
    if repo_dir.exists():
        raise FileExistsError(f"Repository already staged: {repo_dir}")
    zip_path = download_zip(url, stage_dir=stage_dir, branch=branch)
    try:
        extract_zip(zip_path.read_bytes(), repo_dir)
    except Exception:
        # Don't leave a half-staged directory behind (bare zip, no content) —
        # it would permanently block re-staging via the exist_ok=False guard
        # above and show up as a phantom "staged" repo in listings.
        shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    return repo_dir
