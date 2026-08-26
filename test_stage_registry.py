"""Deterministic, offline tests for tools/stage_registry.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.stage_registry import is_staged, list_staged_repos, staged_repo_dir


@pytest.fixture()
def stage(tmp_path: Path) -> Path:
    (tmp_path / "hello").mkdir()
    (tmp_path / "world").mkdir()
    (tmp_path / "notes.txt").write_text("not a repo dir", encoding="utf-8")
    return tmp_path


def test_list_staged_repos(stage: Path):
    assert list_staged_repos(stage_dir=stage) == ["hello", "world"]


def test_list_staged_repos_empty(tmp_path: Path):
    assert list_staged_repos(stage_dir=tmp_path) == []


def test_is_staged_true_and_false(stage: Path):
    assert is_staged("hello", stage_dir=stage)
    assert not is_staged("nope", stage_dir=stage)


def test_staged_repo_dir_returns_path(stage: Path):
    assert staged_repo_dir("hello", stage_dir=stage) == (stage / "hello").resolve()


def test_staged_repo_dir_missing_raises(stage: Path):
    with pytest.raises(FileNotFoundError):
        staged_repo_dir("nope", stage_dir=stage)


def test_staged_repo_dir_rejects_traversal(stage: Path):
    with pytest.raises(ValueError):
        staged_repo_dir("../../etc", stage_dir=stage)


def test_is_staged_false_for_traversal(stage: Path):
    assert not is_staged("../../etc", stage_dir=stage)
