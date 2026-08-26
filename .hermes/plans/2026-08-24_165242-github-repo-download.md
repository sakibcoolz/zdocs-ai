# GitHub Repo Download & Stage Feature Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Let a user pass a GitHub repository URL; the app downloads it as a zip, stores the zip inside a directory named after the repo (`stage/<reponame>/<reponame>.zip`), unzips it in place, then builds the agent pointed at that repo directory and triggers it.

**Architecture:** A new stdlib-only module `tools/github_downloader.py` does the work in three explicit steps — (1) fetch the codeload zip and save it to `stage/<reponame>/<reponame>.zip`, (2) extract it into `stage/<reponame>/` stripping the GitHub `{repo}-{ref}/` wrapper and guarding against zip-slip, (3) return the repo directory path. `runner.py` detects a GitHub URL argument, runs the download, then builds the agent with `stage_dir=<reponame dir>` and starts the REPL (or answers a one-shot question). `agent.py` is unchanged — `build_agent` already accepts a `stage_dir`.

**Tech Stack:** Python 3.12 stdlib (`urllib.request`, `zipfile`, `io`, `re`, `shutil`), `google-adk` (`Runner`, `LlmAgent`), `pytest` (deterministic/offline via `monkeypatch`). No new dependencies.

**Assumptions:**
- Only `github.com` URLs are supported (per the request). Non-GitHub URLs are rejected.
- Default branch is fetched via codeload's `HEAD` ref, so the caller never needs to know the branch name.
- The GitHub zip wraps everything in a top-level folder like `repo-ref/`; that wrapper is stripped so files land directly in `stage/<reponame>/`.
- The saved zip (`<reponame>.zip`) is kept alongside the extracted files (it is "stored inside the repo-named directory" per the spec).
- The agent is scoped to the downloaded repo dir (`stage/<reponame>/`), not the parent `stage/`, so it reads exactly that repo.

---

### Task 1: Parse and validate a GitHub URL

**Objective:** Extract `(owner, repo)` from a GitHub URL and reject anything that is not a plain GitHub repo URL (including path-traversal attempts in the repo name).

**Files:**
- Create: `tools/github_downloader.py`
- Test: `test_github_downloader.py`

**Step 1: Write failing test**

```python
# test_github_downloader.py
import pytest
from tools.github_downloader import parse_repo


def test_parse_repo_plain():
    assert parse_repo("https://github.com/octo/hello") == ("octo", "hello")


def test_parse_repo_with_git_suffix_and_slash():
    assert parse_repo("https://github.com/octo/hello.git/") == ("octo", "hello")


def test_parse_repo_with_www():
    assert parse_repo("http://www.github.com/octo/hello") == ("octo", "hello")


@pytest.mark.parametrize(
    "bad",
    [
        "https://gitlab.com/octo/hello",            # wrong host
        "https://github.com/octo",                 # missing repo
        "https://github.com/octo/hello/tree/main", # sub-path not allowed
        "not a url",
        "https://github.com/../..",                # traversal in owner/repo
        "https://github.com/octo/../secret",
    ],
)
def test_parse_repo_rejects_bad_urls(bad):
    with pytest.raises(ValueError):
        parse_repo(bad)
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_github_downloader.py::test_parse_repo_plain -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.github_downloader'`

**Step 3: Write minimal implementation**

```python
# tools/github_downloader.py
from __future__ import annotations

import re

_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?(?:\.git)?/?$"
)

_FORBIDDEN_NAMES = {"", ".", ".."}


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
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_github_downloader.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/github_downloader.py test_github_downloader.py
git commit -m "feat: parse and validate GitHub repo URLs"
```

---

### Task 2: Build the zip URL and fetch bytes

**Objective:** Add a pure URL builder (`_zip_url`) and a thin `_fetch` wrapper around `urllib.request`, kept separate so tests can stub the network.

**Files:**
- Modify: `tools/github_downloader.py`
- Test: `test_github_downloader.py`

**Step 1: Write failing test**

```python
# test_github_downloader.py (append)
from tools.github_downloader import _zip_url


def test_zip_url_defaults_to_head():
    assert _zip_url("octo", "hello") == "https://codeload.github.com/octo/hello/zip/HEAD"


def test_zip_url_explicit_branch():
    assert _zip_url("octo", "hello", "main") == (
        "https://codeload.github.com/octo/hello/zip/main"
    )
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_github_downloader.py::test_zip_url_defaults_to_head -q`
Expected: FAIL — `ImportError: cannot import name '_zip_url'`

**Step 3: Write minimal implementation**

```python
# tools/github_downloader.py (append)
import urllib.request

_ARCHIVE_BASE = "https://codeload.github.com"


def _zip_url(owner: str, repo: str, branch: str | None = None) -> str:
    ref = branch or "HEAD"
    return f"{_ARCHIVE_BASE}/{owner}/{repo}/zip/{ref}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_github_downloader.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/github_downloader.py test_github_downloader.py
git commit -m "feat: github archive url builder and fetcher"
```

---

### Task 3: Extract a zip safely, stripping the top-level wrapper

**Objective:** Add `extract_zip(data, dest)` that writes archive members under `dest`, strips the GitHub `{repo}-{ref}/` wrapper, and refuses any entry that would escape `dest` (zip-slip protection).

**Files:**
- Modify: `tools/github_downloader.py`
- Test: `test_github_downloader.py`

**Step 1: Write failing test**

```python
# test_github_downloader.py (append)
import io
import zipfile
from pathlib import Path

from tools.github_downloader import extract_zip


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_zip_strips_top_level_dir(tmp_path: Path):
    dest = tmp_path / "hello"
    data = _make_zip(
        {
            "hello-main/README.md": b"# hello\n",
            "hello-main/src/main.py": b"print('hi')\n",
        }
    )
    extract_zip(data, dest)
    assert (dest / "README.md").read_bytes() == b"# hello\n"
    assert (dest / "src" / "main.py").read_bytes() == b"print('hi')\n"
    assert not (dest / "hello-main").exists()  # wrapper folder stripped


def test_extract_zip_flat_archive(tmp_path: Path):
    dest = tmp_path / "flat"
    extract_zip(_make_zip({"a.txt": b"a"}), dest)
    assert (dest / "a.txt").read_bytes() == b"a"


def test_extract_zip_rejects_zip_slip(tmp_path: Path):
    dest = tmp_path / "safe"
    data = _make_zip({"../../evil.txt": b"owned"})
    with pytest.raises(ValueError):
        extract_zip(data, dest)
    assert not (tmp_path / "evil.txt").exists()
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_github_downloader.py::test_extract_zip_strips_top_level_dir -q`
Expected: FAIL — `ImportError: cannot import name 'extract_zip'`

**Step 3: Write minimal implementation**

```python
# tools/github_downloader.py (append)
import io
import shutil
import zipfile
from pathlib import Path


def _common_prefix(names: list[str]) -> str:
    """Return the shared top-level directory of all non-empty entry names, or ''."""
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while prefix and not name.startswith(prefix):
            prefix = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
    return prefix


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
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_github_downloader.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/github_downloader.py test_github_downloader.py
git commit -m "feat: safe zip extraction with wrapper strip and zip-slip guard"
```

---

### Task 4: Resolve the stage dir and save the zip inside the repo-named directory

**Objective:** Add `_resolve_stage` (fail loudly if the stage dir is missing) and `download_zip(url, stage_dir=None, branch=None)`, which fetches the archive and saves it as `stage/<reponame>/<reponame>.zip`, creating the repo-named directory. Refuse to overwrite an already-staged repo.

**Files:**
- Modify: `tools/github_downloader.py`
- Test: `test_github_downloader.py`

**Step 1: Write failing test**

```python
# test_github_downloader.py (append)
import tools.github_downloader as gd
from tools.github_downloader import download_zip, _resolve_stage


def test_resolve_stage_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        _resolve_stage(tmp_path / "nope")


def test_download_zip_saves_inside_repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(gd, "_fetch", lambda url: _make_zip({"hello-main/README.md": b"# hi\n"}))

    zip_path = download_zip("https://github.com/octo/hello", stage_dir=stage)

    assert zip_path == stage / "hello" / "hello.zip"
    assert zip_path.is_file()


def test_download_zip_existing_repo_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    (stage / "hello").mkdir(parents=True)
    monkeypatch.setattr(gd, "_fetch", lambda url: b"")
    with pytest.raises(FileExistsError):
        download_zip("https://github.com/octo/hello", stage_dir=stage)
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_github_downloader.py::test_download_zip_saves_inside_repo_dir -q`
Expected: FAIL — `ImportError: cannot import name 'download_zip'`

**Step 3: Write minimal implementation**

```python
# tools/github_downloader.py (append)
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
    repo_dir.mkdir(parents=True, exist_ok=False)  # raises FileExistsError if staged
    zip_path = repo_dir / f"{repo}.zip"
    zip_path.write_bytes(_fetch(_zip_url(owner, repo, branch)))
    return zip_path
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_github_downloader.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/github_downloader.py test_github_downloader.py
git commit -m "feat: save repo zip inside the repo-named stage directory"
```

---

### Task 5: Orchestrate download → unzip → return repo directory

**Objective:** Add `download_repo(url, stage_dir=None, branch=None)` that composes `download_zip` + `extract_zip` and returns the extracted repo directory path (`stage/<reponame>/`).

**Files:**
- Modify: `tools/github_downloader.py`
- Test: `test_github_downloader.py`

**Step 1: Write failing test**

```python
# test_github_downloader.py (append)
from tools.github_downloader import download_repo


def test_download_repo_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(
        gd, "_fetch", lambda url: _make_zip({"hello-main/README.md": b"# hi\n"})
    )

    repo_dir = download_repo("https://github.com/octo/hello", stage_dir=stage)

    assert repo_dir == stage / "hello"
    assert (repo_dir / "README.md").read_bytes() == b"# hi\n"   # unzipped content
    assert (repo_dir / "hello.zip").is_file()                    # zip kept alongside


def test_download_repo_existing_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    (stage / "hello").mkdir(parents=True)
    monkeypatch.setattr(gd, "_fetch", lambda url: b"")
    with pytest.raises(FileExistsError):
        download_repo("https://github.com/octo/hello", stage_dir=stage)
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_github_downloader.py::test_download_repo_end_to_end -q`
Expected: FAIL — `ImportError: cannot import name 'download_repo'`

**Step 3: Write minimal implementation**

```python
# tools/github_downloader.py (append)
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
    extract_zip(zip_path.read_bytes(), repo_dir)
    return repo_dir
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_github_downloader.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add tools/github_downloader.py test_github_downloader.py
git commit -m "feat: download_repo orchestrates download and unzip"
```

---

### Task 6: Runner accepts a GitHub URL, then builds the agent pointed at the repo dir

**Objective:** In `runner.py`, accept a `stage_dir` on `run_once`/`repl`, add `is_github_url`, and in `__main__` — when the first arg is a GitHub URL — download+unzip, then build the agent with `stage_dir=<reponame dir>` and trigger it (one-shot with the remaining args, or REPL).

**Files:**
- Modify: `runner.py:30-101`
- Test: `test_runner.py` (new)

**Step 1: Write failing test**

```python
# test_runner.py
import pytest
import runner


def test_is_github_url():
    assert runner.is_github_url("https://github.com/octo/hello")
    assert not runner.is_github_url("summarize sample.md")
```

**Step 2: Run test to verify failure**

Run: `python -m pytest test_runner.py::test_is_github_url -q`
Expected: FAIL — `AttributeError: module 'runner' has no attribute 'is_github_url'`

**Step 3: Write minimal implementation**

```python
# runner.py — add import + helper, and thread stage_dir through
from tools.github_downloader import download_repo, parse_repo


def is_github_url(arg: str) -> bool:
    try:
        parse_repo(arg)
        return True
    except ValueError:
        return False


def _build_runner(stage_dir=None):
    return Runner(
        app_name=APP_NAME,
        agent=build_agent(stage_dir=stage_dir),
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
    )
```

Refactor `run_once(prompt, stage_dir=None)` and `repl(stage_dir=None)` to call `_build_runner(stage_dir)`. Replace the `__main__` block:

```python
if __name__ == "__main__":
    if len(sys.argv) > 1 and is_github_url(sys.argv[1]):
        repo_dir = download_repo(sys.argv[1])
        print(f"\nDownloaded and staged repository at: {repo_dir}\n")
        if len(sys.argv) > 2:
            run_once(" ".join(sys.argv[2:]), stage_dir=repo_dir)
        else:
            repl(stage_dir=repo_dir)
    elif len(sys.argv) > 1:
        run_once(" ".join(sys.argv[1:]))
    else:
        repl()
```

**Step 4: Run test to verify pass**

Run: `python -m pytest test_runner.py -q`
Expected: PASS

**Step 5: Full-suite check**

Run: `python -m pytest -q`
Expected: PASS — all existing 12 tests plus the new downloader/runner tests, offline.

**Step 6: Manual end-to-end (optional, needs network)**

Run: `python runner.py https://github.com/octocat/Hello-World "What is in this repo?"`
Expected: prints "Downloaded and staged repository at: .../stage/Hello-World", then the agent's answer about the staged repo.

**Step 7: Commit**

```bash
git add runner.py test_runner.py
git commit -m "feat: runner downloads a github url then triggers the agent"
```

---

### Task 7: Document the new flow

**Objective:** Update `README.md` to describe passing a GitHub URL and note the security properties.

**Files:**
- Modify: `README.md`

**Step 1: Edit** — add a "Download a repository" section after "Run":

```markdown
## Download a repository

Pass a GitHub repository URL and zdocs-ai will download it as a zip, save the zip
inside `stage/<reponame>/<reponame>.zip`, unzip it in place, then start the agent
scoped to that repository directory:

```bash
# download + ask a question
python runner.py https://github.com/octocat/Hello-World "What is in this repo?"

# download + drop into interactive mode
python runner.py https://github.com/octocat/Hello-World
```

The downloader only accepts `github.com` URLs, strips the archive's top-level
wrapper folder, and refuses any zip entry that would escape the repo directory
(zip-slip).
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document github download flow"
```

---

## Tests / Validation (summary)

Run after all tasks:

```bash
python -m pytest -q
```

Expected: all existing 12 tests plus ~15 new tests pass, fully offline (network stubbed via `monkeypatch`).

## Files Likely to Change

- Create: `tools/github_downloader.py`
- Create: `test_github_downloader.py`
- Create: `test_runner.py`
- Modify: `runner.py`, `README.md`
- Unchanged: `agent.py`, `tools/file_reader.py`, `test_agent.py`

## Risks, Tradeoffs, Open Questions

- **Zip-slip / symlinks:** entries are written as regular files via `copyfileobj`, so a symlink entry degrades to a plain text file rather than a live symlink — safe, but a symlink-heavy repo would lose links. Acceptable for a doc/code assistant.
- **Zip kept alongside files:** `<reponame>.zip` remains in the repo dir after unzip (per the spec that the zip is "stored inside the repo-named directory"). It will appear in `list_files`; trivial to delete post-extract if the user prefers.
- **Large repos:** `read_file_with_limit` truncates oversized files, but a huge repo still yields a large `list_files` output. Out of scope.
- **Only GitHub:** the request specifies GitHub. Generalizing to GitLab/other hosts would mean extending `parse_repo`/`_zip_url`.
- **No `git` dependency:** we fetch the tarball via codeload rather than shelling out to `git`, so `git` need not be installed and `stage/` stays a plain directory tree.
- **Open question:** provenance manifest (URL, branch, timestamp) in the repo dir — deferred (YAGNI) unless wanted.
