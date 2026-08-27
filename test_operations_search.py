"""Tests for text search, bounded file reads and their no-ripgrep fallbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.errors import PathEscapeError
from operations.policy import ExecutionPolicy, resolve_repo_root
from operations.schemas import DetectionMethod
from operations.symbol_search import (
    excerpt,
    read_file_range,
    read_text_file,
    search_text,
    word_pattern,
)


@pytest.fixture()
def policy() -> ExecutionPolicy:
    return ExecutionPolicy.repository_analysis()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "service.py").write_text(
        "class UserService:\n"
        "    def get_user(self):\n"
        "        return build_user()\n"
        "\n"
        "def build_user():\n"
        "    return UserService()\n",
        encoding="utf-8",
    )
    (root / "notes.md").write_text("UserService is documented here.\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    return resolve_repo_root(root)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def test_word_pattern_escapes_regex_metacharacters() -> None:
    assert word_pattern("a.b") == r"\ba\.b\b"


def test_finds_symbol_occurrences(repo: Path, policy: ExecutionPolicy) -> None:
    matches, truncated, method = search_text(
        repo, policy, None, word_pattern("UserService")
    )
    found = {(match.file_path, match.line) for match in matches}
    assert ("pkg/service.py", 1) in found
    assert ("pkg/service.py", 6) in found
    assert ("notes.md", 1) in found
    assert truncated is False
    assert method is DetectionMethod.TEXT_SEARCH


def test_word_boundaries_prevent_substring_hits(repo: Path, policy: ExecutionPolicy) -> None:
    matches, _, _ = search_text(repo, policy, None, word_pattern("User"))
    assert matches == []


def test_search_can_be_restricted_to_a_language(repo: Path, policy: ExecutionPolicy) -> None:
    matches, _, _ = search_text(
        repo, policy, None, word_pattern("UserService"), languages={"python"}
    )
    assert {match.file_path for match in matches} == {"pkg/service.py"}


def test_search_respects_the_match_limit(repo: Path, policy: ExecutionPolicy) -> None:
    matches, truncated, _ = search_text(
        repo, policy, None, word_pattern("UserService"), max_matches=1
    )
    assert len(matches) == 1
    assert truncated is True


def test_search_skips_binary_files(repo: Path, policy: ExecutionPolicy) -> None:
    matches, _, _ = search_text(repo, policy, None, "binary")
    assert matches == []


def test_search_redacts_secrets_in_matched_lines(repo: Path, policy: ExecutionPolicy) -> None:
    (repo / "config.py").write_text(
        'api_key = "sk-abcdefghijklmnopqrstuvwxyz"\n', encoding="utf-8"
    )
    matches, _, _ = search_text(repo, policy, None, word_pattern("api_key"))
    assert matches
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in matches[0].text
    assert "[REDACTED]" in matches[0].text


def test_search_falls_back_to_python_without_ripgrep(
    repo: Path, policy: ExecutionPolicy
) -> None:
    runner = StubCommandRunner()
    matches, _, method = search_text(
        repo, policy, runner, word_pattern("UserService")  # type: ignore[arg-type]
    )
    assert matches
    assert method is DetectionMethod.TEXT_SEARCH
    assert runner.calls == []


# --------------------------------------------------------------------------
# Bounded reads
# --------------------------------------------------------------------------


def test_reads_a_line_range(repo: Path, policy: ExecutionPolicy) -> None:
    result = read_file_range(repo, policy, "pkg/service.py", start_line=1, end_line=2)
    assert result["start_line"] == 1
    assert result["end_line"] == 2
    assert result["total_lines"] == 6
    assert result["content"].splitlines() == [
        "class UserService:",
        "    def get_user(self):",
    ]


def test_reads_to_end_of_file_by_default(repo: Path, policy: ExecutionPolicy) -> None:
    result = read_file_range(repo, policy, "pkg/service.py")
    assert result["end_line"] == 6


def test_read_truncates_at_max_lines(repo: Path, policy: ExecutionPolicy) -> None:
    result = read_file_range(repo, policy, "pkg/service.py", max_lines=2)
    assert result["truncated"] is True
    assert len(result["content"].splitlines()) == 2


def test_read_redacts_secrets(repo: Path, policy: ExecutionPolicy) -> None:
    (repo / "config.py").write_text(
        "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz01'\n", encoding="utf-8"
    )
    result = read_file_range(repo, policy, "config.py")
    assert "ghp_" not in result["content"]
    assert "[REDACTED]" in result["content"]


def test_read_rejects_traversal(repo: Path, policy: ExecutionPolicy) -> None:
    with pytest.raises(PathEscapeError):
        read_file_range(repo, policy, "../secret.txt")


def test_read_rejects_absolute_paths(repo: Path, policy: ExecutionPolicy) -> None:
    with pytest.raises(PathEscapeError):
        read_file_range(repo, policy, "/etc/passwd")


def test_read_rejects_a_directory(repo: Path, policy: ExecutionPolicy) -> None:
    with pytest.raises((FileNotFoundError, ValueError)):
        read_file_range(repo, policy, "pkg")


def test_read_rejects_binary_files(repo: Path, policy: ExecutionPolicy) -> None:
    with pytest.raises(ValueError):
        read_file_range(repo, policy, "image.bin")


def test_read_text_file_refuses_oversized_files(repo: Path, policy: ExecutionPolicy) -> None:
    tight = policy.with_overrides(max_file_bytes=5)
    assert read_text_file(repo / "pkg" / "service.py", tight) is None


def test_excerpt_returns_a_single_line() -> None:
    assert excerpt("a\nb\nc", 2) == "b"


def test_excerpt_out_of_range_is_empty() -> None:
    assert excerpt("a\nb", 99) == ""
