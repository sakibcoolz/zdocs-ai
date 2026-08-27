"""Offline tests for file discovery, counting and language detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.inventory import (
    EXTENSION_LANGUAGE,
    IGNORED_DIRS,
    SUPPORTED_LANGUAGES,
    count_files_and_directories,
    detect_language,
    detect_languages,
    discover_files,
)
from operations.policy import ExecutionPolicy, resolve_repo_root


@pytest.fixture()
def policy() -> ExecutionPolicy:
    return ExecutionPolicy.repository_analysis()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository with nested source, an ignored tree and a hidden file."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "pkg" / "util.go").write_text("package pkg\n", encoding="utf-8")
    (root / "src" / "pkg" / "App.java").write_text("class App {}\n", encoding="utf-8")
    (root / "README.md").write_text("# hi\n", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "index.js").write_text("x\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return resolve_repo_root(root)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discovers_source_files(repo: Path, policy: ExecutionPolicy) -> None:
    files, truncated, _ = discover_files(repo, policy, None)
    paths = {file.path for file in files}
    assert paths == {"README.md", "src/main.py", "src/pkg/App.java", "src/pkg/util.go"}
    assert truncated is False


def test_ignores_dependency_and_vcs_directories(repo: Path, policy: ExecutionPolicy) -> None:
    files, _, _ = discover_files(repo, policy, None)
    paths = {file.path for file in files}
    assert not any(path.startswith("node_modules/") for path in paths)
    assert not any(path.startswith(".git/") for path in paths)


def test_discovery_respects_the_file_limit(repo: Path, policy: ExecutionPolicy) -> None:
    files, truncated, _ = discover_files(repo, policy, None, limit=2)
    assert len(files) == 2
    assert truncated is True


def test_discovery_respects_the_policy_scan_cap(repo: Path, policy: ExecutionPolicy) -> None:
    files, truncated, _ = discover_files(
        repo, policy.with_overrides(max_files_scanned=2), None
    )
    assert len(files) <= 2
    assert truncated is True


def test_discovery_filters_by_language(repo: Path, policy: ExecutionPolicy) -> None:
    files, _, _ = discover_files(repo, policy, None, languages={"python", "go"})
    assert {file.path for file in files} == {"src/main.py", "src/pkg/util.go"}


def test_discovery_filters_by_extension(repo: Path, policy: ExecutionPolicy) -> None:
    files, _, _ = discover_files(repo, policy, None, extensions={".md"})
    assert [file.path for file in files] == ["README.md"]


def test_discovery_can_be_scoped_to_a_subdirectory(repo: Path, policy: ExecutionPolicy) -> None:
    files, _, _ = discover_files(repo, policy, None, subdir="src/pkg")
    assert {file.path for file in files} == {"src/pkg/App.java", "src/pkg/util.go"}


def test_discovery_rejects_a_traversing_subdirectory(repo: Path, policy: ExecutionPolicy) -> None:
    from operations.errors import PathEscapeError

    with pytest.raises(PathEscapeError):
        discover_files(repo, policy, None, subdir="../")


def test_discovery_skips_symlinks(repo: Path, policy: ExecutionPolicy) -> None:
    (repo / "link.py").symlink_to(repo / "src" / "main.py")
    files, _, _ = discover_files(repo, policy, None)
    assert "link.py" not in {file.path for file in files}


def test_discovery_does_not_follow_symlinked_directories(
    repo: Path, policy: ExecutionPolicy
) -> None:
    outside = repo.parent / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    files, _, _ = discover_files(repo, policy, None)
    assert not any("secret" in file.path for file in files)


def test_discovery_works_without_ripgrep(repo: Path, policy: ExecutionPolicy) -> None:
    # The whole point of the fallback: a machine with no external tools still
    # gets correct results, not an error and not an empty list.
    runner = StubCommandRunner()
    files, _, method = discover_files(repo, policy, runner)  # type: ignore[arg-type]
    assert {file.path for file in files} == {
        "README.md",
        "src/main.py",
        "src/pkg/App.java",
        "src/pkg/util.go",
    }
    assert method.value == "filesystem"
    assert runner.calls == []


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def test_counts_files_and_directories(repo: Path, policy: ExecutionPolicy) -> None:
    counts = count_files_and_directories(repo, policy, None)
    assert counts["file_count"] == 4
    assert counts["directory_count"] == 2  # src, src/pkg
    assert counts["total_bytes"] > 0
    assert counts["files_by_extension"][".py"] == 1
    assert counts["truncated"] is False


def test_counting_an_empty_repository(tmp_path: Path, policy: ExecutionPolicy) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    counts = count_files_and_directories(resolve_repo_root(root), policy, None)
    assert counts["file_count"] == 0
    assert counts["directory_count"] == 0
    assert counts["total_bytes"] == 0


# --------------------------------------------------------------------------
# Language detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a/b/main.py", "python"),
        ("main.pyi", "python"),
        ("cmd/server/main.go", "go"),
        ("src/App.java", "java"),
        ("src/app.js", "javascript"),
        ("src/app.jsx", "javascript"),
        ("src/app.ts", "typescript"),
        ("src/app.tsx", "typescript"),
        ("Dockerfile", "dockerfile"),
        ("Makefile", "make"),
        ("go.mod", "go"),
        ("README.md", "markdown"),
        ("mystery.qqq", None),
        ("noextension", None),
    ],
)
def test_detects_language_from_path(path: str, language: str | None) -> None:
    assert detect_language(path) == language


def test_language_breakdown(repo: Path, policy: ExecutionPolicy) -> None:
    report = detect_languages(repo, policy, None)
    by_name = {entry["language"]: entry for entry in report["languages"]}
    assert by_name["python"]["files"] == 1
    assert by_name["go"]["files"] == 1
    assert by_name["java"]["files"] == 1
    assert by_name["markdown"]["supported"] is False
    assert by_name["python"]["supported"] is True
    assert report["primary_language"] in {"python", "go", "java"}
    assert set(report["supported_languages"]) == SUPPORTED_LANGUAGES


def test_language_percentages_are_reported(repo: Path, policy: ExecutionPolicy) -> None:
    report = detect_languages(repo, policy, None)
    total = sum(entry["percent_of_classified_files"] for entry in report["languages"])
    assert 99.0 <= total <= 101.0


def test_unclassified_files_are_counted(tmp_path: Path, policy: ExecutionPolicy) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "data.qqq").write_text("?", encoding="utf-8")
    report = detect_languages(resolve_repo_root(root), policy, None)
    assert report["unclassified_files"] == 1
    assert report["primary_language"] is None


def test_supported_languages_all_have_extensions() -> None:
    mapped = set(EXTENSION_LANGUAGE.values())
    assert SUPPORTED_LANGUAGES.issubset(mapped)


def test_ignored_dirs_include_the_usual_suspects() -> None:
    assert {".git", "node_modules", "__pycache__", "vendor", "dist"} <= IGNORED_DIRS
