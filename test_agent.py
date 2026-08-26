"""Deterministic tests for the file-reader tool and agent wiring.

These tests exercise the tool layer and agent construction directly and do NOT
make any LLM/network calls, so they run instantly and offline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent import build_agent
from tools import file_reader
from tools.file_reader import (
    FileReaderTool,
    list_files,
    read_file,
    read_file_with_limit,
)


@pytest.fixture()
def stage(tmp_path: Path) -> Path:
    """Create a stage directory with a couple of files."""
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "data.txt").write_text("nested data", encoding="utf-8")
    return tmp_path


def test_read_file(stage: Path) -> None:
    assert read_file("hello.txt", stage_dir=stage) == "hello world\n"


def test_read_nested_file(stage: Path) -> None:
    assert read_file("nested/data.txt", stage_dir=stage) == "nested data"


def test_read_missing_file_raises(stage: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_file("does-not-exist.txt", stage_dir=stage)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    with pytest.raises((ValueError, FileNotFoundError)):
        read_file("../secret.txt", stage_dir=stage)

    # Absolute path to a file outside stage must also be rejected.
    with pytest.raises((ValueError, FileNotFoundError)):
        read_file(str(secret), stage_dir=stage)


def test_missing_stage_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_file("x.txt", stage_dir=tmp_path / "nope")


def test_list_files(stage: Path) -> None:
    listing = list_files(stage_dir=stage)
    assert "hello.txt" in listing
    assert "nested/data.txt" in listing


def test_list_files_empty(tmp_path: Path) -> None:
    assert list_files(stage_dir=tmp_path) == "No files in stage directory."


def test_read_file_with_limit_truncates(stage: Path) -> None:
    (stage / "big.txt").write_text("x" * 100, encoding="utf-8")
    out = read_file_with_limit("big.txt", max_chars=10, stage_dir=stage)
    assert "truncated" in out
    assert "[... truncated ...]" in out


def test_read_file_with_limit_no_truncation(stage: Path) -> None:
    out = read_file_with_limit("hello.txt", max_chars=1000, stage_dir=stage)
    assert out == "hello world\n"


def test_build_agent_wires_tools() -> None:
    agent = build_agent()
    assert agent.name == "zdocs_assistant"
    # Two tools (read_file_with_limit, list_files) should be attached.
    assert len(agent.tools) == 2


def test_file_reader_tool_factory(stage: Path) -> None:
    tools = FileReaderTool(stage_dir=stage)
    assert len(tools) == 2
    assert all(hasattr(t, "name") for t in tools)
    names = {t.name for t in tools}
    assert "read_file_with_limit" in names
    assert "list_files" in names


def test_default_stage_dir_resolves_to_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The module default points at <project>/stage, which exists in the repo.
    resolved = file_reader._resolve_stage_dir()
    assert resolved.name == "stage"
