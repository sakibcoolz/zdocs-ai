"""Repository inventory: file discovery, counting and language detection.

Deterministic and offline. Uses ``rg --files`` when ripgrep is installed
(fast, and it is the tool the rest of the search layer already depends on) and
falls back to an equivalent pure-Python walk otherwise — both apply the *same*
ignore rules and the same policy limits, so results do not change shape with
the environment.

Symlinks are never followed: a symlinked directory inside a repository could
otherwise walk straight out of the sandbox (or into an infinite loop).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from operations.command_runner import CommandRunner
from operations.policy import ExecutionPolicy, relative_to_repo, resolve_repo_path
from operations.schemas import DetectionMethod

#: Directories never walked: VCS internals, dependency trees, build output and
#: tool caches. They dominate file counts and contain no first-party source.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".bzr",
        "node_modules", "bower_components", "vendor", "third_party",
        ".venv", "venv", "env", "virtualenv", "__pycache__", ".tox", ".nox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
        "dist", "build", "out", "target", "bin", "obj", ".next", ".nuxt",
        ".gradle", ".idea", ".vscode", ".terraform", "coverage", ".eggs",
    }
)

#: Extension → language name. Only the five languages with real analyzers are
#: claimed as *supported*; the rest are reported for inventory purposes only.
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    # Reported, but not analyzed — see SUPPORTED_LANGUAGES.
    ".rb": "ruby", ".rs": "rust", ".php": "php", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".scala": "scala", ".sh": "shell", ".bash": "shell",
    ".sql": "sql", ".md": "markdown", ".rst": "restructuredtext",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".html": "html", ".htm": "html", ".css": "css",
    ".scss": "css", ".proto": "protobuf", ".tf": "terraform",
    ".dockerfile": "dockerfile", ".gradle": "gradle", ".ipynb": "notebook",
}

#: Filenames (no useful extension) mapped to a language.
FILENAME_LANGUAGE: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "make",
    "go.mod": "go",
    "go.sum": "go",
    "Gemfile": "ruby",
    "Rakefile": "ruby",
}

#: Languages with a dedicated analyzer in :mod:`operations.languages`.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"python", "go", "java", "javascript", "typescript"}
)


@dataclass(frozen=True)
class RepoFile:
    """One discovered repository file."""

    path: str
    """Repository-relative POSIX path."""
    size: int
    language: str | None


def detect_language(path: str) -> str | None:
    """Language for a repository-relative path, or ``None`` if unrecognised."""
    name = PurePath_name(path)
    if name in FILENAME_LANGUAGE:
        return FILENAME_LANGUAGE[name]
    suffix = os.path.splitext(name)[1].lower()
    if not suffix and name.lower().startswith("dockerfile"):
        return "dockerfile"
    return EXTENSION_LANGUAGE.get(suffix)


def PurePath_name(path: str) -> str:  # noqa: N802 - tiny helper, kept explicit
    """Final path component of a POSIX-style relative path."""
    return path.rsplit("/", 1)[-1]


def _is_ignored(relative_parts: tuple[str, ...]) -> bool:
    return any(part in IGNORED_DIRS for part in relative_parts)


def _walk_python(root: Path, policy: ExecutionPolicy) -> tuple[list[str], int, bool]:
    """Pure-Python file walk. Returns ``(paths, directory_count, truncated)``."""
    paths: list[str] = []
    directories = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_parts = current.relative_to(root).parts if current != root else ()
        if _is_ignored(rel_parts):
            dirnames[:] = []
            continue
        # Prune ignored and symlinked directories in place so os.walk skips them.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in IGNORED_DIRS
            and (policy.follow_symlinks or not (current / d).is_symlink())
        )
        directories += len(dirnames)
        for filename in sorted(filenames):
            file_path = current / filename
            if not policy.follow_symlinks and file_path.is_symlink():
                continue
            paths.append(relative_to_repo(root, file_path))
            if len(paths) >= policy.max_files_scanned:
                return paths, directories, True
    return paths, directories, truncated


def _walk_ripgrep(
    root: Path, policy: ExecutionPolicy, runner: CommandRunner
) -> tuple[list[str], bool] | None:
    """File list via ``rg --files``; ``None`` if ripgrep is unusable here.

    ``--no-ignore``/``--hidden`` are passed so ripgrep and the Python fallback
    see the same set of files — the ignore policy is ours (``IGNORED_DIRS``),
    not the repository's ``.gitignore``, which keeps results reproducible.
    """
    if not runner.is_available("rg"):
        return None
    argv = ["rg", "--files", "--hidden", "--no-ignore", "--no-messages"]
    for ignored in sorted(IGNORED_DIRS):
        argv += ["--glob", f"!{ignored}/"]
    if not policy.follow_symlinks:
        argv.append("--no-follow")
    result = runner.run(argv, cwd=root)
    # rg exits 1 when it matched nothing, which is not an error for --files.
    if result.timed_out or result.exit_code not in (0, 1):
        return None
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    truncated = len(paths) > policy.max_files_scanned
    return sorted(paths)[: policy.max_files_scanned], truncated or result.truncated


def discover_files(
    root: Path,
    policy: ExecutionPolicy,
    runner: CommandRunner | None = None,
    *,
    subdir: str | None = None,
    languages: set[str] | None = None,
    extensions: set[str] | None = None,
    limit: int | None = None,
) -> tuple[list[RepoFile], bool, DetectionMethod]:
    """Discover repository files under the policy's limits.

    Args:
        root: Resolved repository root.
        policy: Active execution policy (limits, symlink rules).
        runner: Command runner used for the ripgrep fast path. ``None`` forces
            the Python fallback.
        subdir: Restrict the walk to this repository-relative directory.
        languages: Keep only files detected as one of these languages.
        extensions: Keep only files with one of these extensions (``.py``).
        limit: Cap on returned files (in addition to the policy cap).

    Returns:
        ``(files, truncated, detection_method)``.
    """
    base = resolve_repo_path(root, subdir, follow_symlinks=policy.follow_symlinks) if subdir else root
    if base != root and not base.is_dir():
        raise NotADirectoryError(f"Not a directory: {subdir!r}")

    method = DetectionMethod.FILESYSTEM
    rg_result = _walk_ripgrep(base, policy, runner) if runner is not None else None
    if rg_result is not None:
        raw_paths, truncated = rg_result
        method = DetectionMethod.RIPGREP
    else:
        raw_paths, _dirs, truncated = _walk_python(base, policy)

    prefix = relative_to_repo(root, base) if base != root else ""
    files: list[RepoFile] = []
    for rel in raw_paths:
        full_rel = f"{prefix}/{rel}" if prefix and not rel.startswith(prefix) else rel
        language = detect_language(full_rel)
        if languages is not None and language not in languages:
            continue
        if extensions is not None and os.path.splitext(full_rel)[1].lower() not in extensions:
            continue
        try:
            size = (root / full_rel).stat().st_size
        except OSError:
            continue
        files.append(RepoFile(path=full_rel, size=size, language=language))
        if limit is not None and len(files) >= limit:
            truncated = True
            break
    files.sort(key=lambda f: f.path)
    return files, truncated, method


def count_files_and_directories(
    root: Path, policy: ExecutionPolicy, runner: CommandRunner | None = None
) -> dict[str, object]:
    """Count files, directories and bytes, ignoring :data:`IGNORED_DIRS`."""
    files, truncated, method = discover_files(root, policy, runner)
    directories = {
        parent
        for file in files
        for parent in _ancestor_dirs(file.path)
    }
    by_extension: dict[str, int] = {}
    for file in files:
        ext = os.path.splitext(file.path)[1].lower() or "(none)"
        by_extension[ext] = by_extension.get(ext, 0) + 1
    return {
        "file_count": len(files),
        "directory_count": len(directories),
        "total_bytes": sum(f.size for f in files),
        "files_by_extension": dict(sorted(by_extension.items(), key=lambda kv: (-kv[1], kv[0]))),
        "ignored_directory_names": sorted(IGNORED_DIRS),
        "truncated": truncated,
        "detection_method": method.value,
    }


def _ancestor_dirs(relative_path: str) -> list[str]:
    """Every directory prefix of a repository-relative file path."""
    parts = relative_path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def detect_languages(
    root: Path, policy: ExecutionPolicy, runner: CommandRunner | None = None
) -> dict[str, object]:
    """Per-language file/byte breakdown for the repository.

    ``supported`` marks the languages this build can analyze structurally; the
    rest are counted but explicitly not claimed as analyzable.
    """
    files, truncated, method = discover_files(root, policy, runner)
    stats: dict[str, dict[str, int]] = {}
    unknown_files = 0
    for file in files:
        if file.language is None:
            unknown_files += 1
            continue
        entry = stats.setdefault(file.language, {"files": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += file.size

    total_files = sum(entry["files"] for entry in stats.values()) or 1
    languages = [
        {
            "language": language,
            "files": entry["files"],
            "bytes": entry["bytes"],
            "percent_of_classified_files": round(100.0 * entry["files"] / total_files, 2),
            "supported": language in SUPPORTED_LANGUAGES,
        }
        for language, entry in sorted(
            stats.items(), key=lambda kv: (-kv[1]["files"], kv[0])
        )
    ]
    primary = next(
        (entry["language"] for entry in languages if entry["supported"]),
        languages[0]["language"] if languages else None,
    )
    return {
        "languages": languages,
        "primary_language": primary,
        "unclassified_files": unknown_files,
        "supported_languages": sorted(SUPPORTED_LANGUAGES),
        "truncated": truncated,
        "detection_method": method.value,
    }
