"""Tests for the operation executor: dispatch, limits, caching, audit, errors."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.cache import (
    JsonFileCache,
    NullCache,
    cache_key,
    content_fingerprint,
    file_fingerprint,
)
from operations.executor import OperationExecutor
from operations.policy import ExecutionPolicy
from operations.schemas import (
    Confidence,
    OperationRequest,
    OperationResult,
    OperationType,
    RelationType,
    SymbolType,
)


def make_executor(root: Path, **kwargs: object) -> OperationExecutor:
    """Executor with no external tools available (fallback paths only)."""
    kwargs.setdefault("runner", StubCommandRunner())
    return OperationExecutor(root, repository=root.name, **kwargs)  # type: ignore[arg-type]


def run(
    executor: OperationExecutor, operation: OperationType, **fields: object
) -> OperationResult:
    return executor.execute(
        OperationRequest(operation=operation, repository=executor.repository, **fields)  # type: ignore[arg-type]
    )


@pytest.fixture()
def executor(python_repo: Path) -> OperationExecutor:
    return make_executor(python_repo)


# --------------------------------------------------------------------------
# Dispatch coverage
# --------------------------------------------------------------------------


def test_every_permitted_operation_has_a_handler(executor: OperationExecutor) -> None:
    assert len(executor.available_operations()) == len(OperationType) - 1
    assert "run_static_analysis" not in executor.available_operations()


def test_list_files(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.LIST_REPOSITORY_FILES)
    assert result.status == "success"
    assert {entry["path"] for entry in result.data["files"]} == {
        "shapes.py",
        "registry.py",
    }


def test_list_files_limit_marks_partial(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.LIST_REPOSITORY_FILES, arguments={"limit": 1})
    assert result.status == "partial"
    assert result.truncated is True


def test_count_and_languages(executor: OperationExecutor) -> None:
    counts = run(executor, OperationType.COUNT_FILES_AND_DIRECTORIES)
    assert counts.data["file_count"] == 2
    languages = run(executor, OperationType.DETECT_LANGUAGES)
    assert languages.data["primary_language"] == "python"


def test_find_class(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CLASS, symbol="Circle")
    assert result.status == "success"
    assert [match.symbol for match in result.matches] == ["Circle"]
    assert result.matches[0].symbol_type is SymbolType.CLASS
    assert result.matches[0].confidence is Confidence.HIGH
    assert result.matches[0].file_path == "shapes.py"


def test_find_interface(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_INTERFACE)
    assert [match.symbol for match in result.matches] == ["Renderer"]


def test_find_function_and_method(executor: OperationExecutor) -> None:
    functions = run(executor, OperationType.FIND_FUNCTION)
    assert "total_area" in {match.symbol for match in functions.matches}
    methods = run(executor, OperationType.FIND_METHOD, symbol="area", arguments={"exact": True})
    assert {match.symbol for match in methods.matches} == {"Circle.area", "Square.area", "Shape.area"}


def test_find_symbol_returns_declarations(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_SYMBOL, symbol="BoundingBox")
    assert result.matches[0].symbol_type is SymbolType.CLASS
    assert result.matches[0].confidence is Confidence.HIGH


def test_find_symbol_falls_back_to_low_confidence_candidates(
    executor: OperationExecutor,
) -> None:
    result = run(executor, OperationType.FIND_SYMBOL, symbol="renderer")
    assert result.matches
    assert all(match.confidence is Confidence.LOW for match in result.matches)
    assert all(match.metadata["kind"] == "text_candidate" for match in result.matches)
    assert any("text-search candidates" in warning for warning in result.warnings)


def test_find_symbol_requires_a_symbol(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_SYMBOL)
    assert result.status == "failed"
    assert result.data["error_category"] == "invalid_argument"


def test_find_references(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_REFERENCES, symbol="Shape")
    assert result.matches
    assert result.data["declaration_sites"] == [{"file_path": "shapes.py", "line": 16}]


def test_find_inheritance_and_implementations(executor: OperationExecutor) -> None:
    inheritance = run(executor, OperationType.FIND_INHERITANCE, symbol="Shape")
    assert {(match.symbol, match.target_symbol) for match in inheritance.matches} >= {
        ("Circle", "Shape"),
        ("Square", "Shape"),
    }
    implementations = run(executor, OperationType.FIND_IMPLEMENTATIONS, symbol="Shape")
    assert implementations.matches
    assert all(
        match.relationship is RelationType.IMPLEMENTS for match in implementations.matches
    )


def test_find_imports(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_IMPORTS)
    assert {match.target_symbol for match in result.matches} >= {"abc", "typing"}


def test_find_calls(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CALLS, symbol="area")
    assert result.matches


def test_read_file_range(executor: OperationExecutor) -> None:
    result = run(
        executor,
        OperationType.READ_FILE_RANGE,
        file_path="shapes.py",
        arguments={"start_line": 1, "end_line": 2},
    )
    assert result.status == "success"
    assert result.data["start_line"] == 1
    assert result.evidence


def test_read_file_range_requires_a_path(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.READ_FILE_RANGE)
    assert result.status == "failed"
    assert result.data["error_category"] == "invalid_argument"


def test_read_file_range_rejects_traversal(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.READ_FILE_RANGE, file_path="../../etc/passwd")
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"
    assert "traversal" in result.errors[0].lower()


def test_read_file_range_rejects_absolute_paths(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.READ_FILE_RANGE, file_path="/etc/passwd")
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"


def test_analyze_oop(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.ANALYZE_OOP)
    assert result.status == "success"
    assert result.data["summary"]["files_analyzed"] == 2
    assert result.data["polymorphism"] == {"Shape": ["Circle", "Square"]}
    assert result.data["encapsulation"]["member_count"] > 0
    assert result.evidence


def test_build_relationship_graph(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.BUILD_RELATIONSHIP_GRAPH)
    assert result.data["stats"]["node_count"] > 0
    assert result.data["nodes"]
    assert result.data["edges"]


def test_generate_diagrams_without_writing(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.GENERATE_CLASS_DIAGRAM)
    assert result.data["written_files"] == []
    assert result.data["diagrams"][0]["mermaid"].startswith("classDiagram")


def test_generate_diagram_writes_to_the_docs_root(python_repo: Path, tmp_path: Path) -> None:
    executor = make_executor(python_repo, docs_root=tmp_path / "docs")
    result = run(
        executor, OperationType.GENERATE_INHERITANCE_DIAGRAM, arguments={"write": True}
    )
    written = Path(result.data["written_files"][0])
    assert written.exists()
    assert written.parent == tmp_path / "docs" / "python_repo" / "diagrams"
    assert written.read_text(encoding="utf-8").startswith("classDiagram")


def test_generated_docs_never_land_inside_the_repository(
    python_repo: Path, tmp_path: Path
) -> None:
    executor = make_executor(python_repo, docs_root=tmp_path / "docs")
    run(executor, OperationType.GENERATE_CLASS_DIAGRAM, arguments={"write": True})
    assert not (python_repo / "diagrams").exists()
    assert sorted(path.name for path in python_repo.iterdir()) == [
        "registry.py",
        "shapes.py",
    ]


def test_sequence_diagram_requires_a_start_symbol(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.GENERATE_SEQUENCE_DIAGRAM)
    assert result.status == "failed"


# --------------------------------------------------------------------------
# Policy enforcement at the executor boundary
# --------------------------------------------------------------------------


def test_static_analysis_is_refused_by_the_analysis_profile(
    executor: OperationExecutor,
) -> None:
    result = run(executor, OperationType.RUN_STATIC_ANALYSIS)
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"


def test_static_analysis_requires_a_named_tool(python_repo: Path) -> None:
    # Even with the profile enabled, the caller names an approved tool; it can
    # never supply a command line. See test_operations_sandbox.py for the rest.
    executor = make_executor(
        python_repo, policy=ExecutionPolicy.development_validation(enabled=True)
    )
    result = run(executor, OperationType.RUN_STATIC_ANALYSIS)
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"
    assert "Approved tools" in result.errors[0]


def test_match_limits_are_validated(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CLASS, arguments={"limit": "many"})
    assert result.status == "failed"
    assert result.data["error_category"] == "invalid_argument"


def test_out_of_range_limits_are_rejected(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CLASS, arguments={"limit": 0})
    assert result.status == "failed"


def test_boolean_arguments_are_validated(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CLASS, arguments={"exact": 12})
    assert result.status == "failed"
    assert result.data["error_category"] == "invalid_argument"


def test_unknown_arguments_are_ignored(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_CLASS, arguments={"nonsense": True})
    assert result.status == "success"


def test_absurdly_long_symbol_is_rejected(executor: OperationExecutor) -> None:
    result = run(executor, OperationType.FIND_SYMBOL, symbol="A" * 5000)
    assert result.status == "failed"


def test_internal_errors_do_not_leak_tracebacks(
    executor: OperationExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret internal detail: token ghp_abcdefghijklmnopqrst")

    monkeypatch.setattr(executor, "analysis", explode)
    result = run(executor, OperationType.FIND_CLASS)
    assert result.status == "failed"
    assert result.data["error_category"] == "internal"
    assert "Traceback" not in result.errors[0]
    assert "ghp_" not in result.errors[0]


# --------------------------------------------------------------------------
# Missing external tools
# --------------------------------------------------------------------------


def test_analysis_works_with_no_external_tools(python_repo: Path) -> None:
    stub = StubCommandRunner()
    executor = make_executor(python_repo, runner=stub)
    result = run(executor, OperationType.ANALYZE_OOP)
    assert result.status == "success"
    assert result.matches
    assert stub.calls == []


def test_git_metadata_reports_a_missing_git_binary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    executor = make_executor(root, runner=StubCommandRunner())
    result = run(executor, OperationType.GIT_METADATA)
    assert result.status == "failed"
    assert "git is not installed" in result.errors[0]


def test_git_metadata_on_a_repository_without_git(python_repo: Path) -> None:
    executor = make_executor(python_repo)
    result = run(executor, OperationType.GIT_METADATA)
    assert result.status == "partial"
    assert result.data["is_git_repository"] is False
    assert result.warnings


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_cache_key_is_deterministic() -> None:
    arguments = {"b": 2, "a": 1}
    first = cache_key(
        repository="r",
        commit_sha="abc",
        operation="find_class",
        file_path=None,
        content_hash="h",
        arguments=arguments,
    )
    second = cache_key(
        repository="r",
        commit_sha="abc",
        operation="find_class",
        file_path=None,
        content_hash="h",
        arguments={"a": 1, "b": 2},
    )
    assert first == second


@pytest.mark.parametrize(
    "changed",
    [
        {"repository": "other"},
        {"commit_sha": "def"},
        {"operation": "find_interface"},
        {"file_path": "a.py"},
        {"content_hash": "different"},
        {"arguments": {"a": 2}},
    ],
)
def test_cache_key_changes_with_every_identity_component(changed: dict) -> None:
    base = {
        "repository": "r",
        "commit_sha": "abc",
        "operation": "find_class",
        "file_path": None,
        "content_hash": "h",
        "arguments": {"a": 1},
    }
    assert cache_key(**base) != cache_key(**{**base, **changed})


def test_content_fingerprint_changes_when_a_file_changes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = content_fingerprint(tmp_path, ["a.py"])
    (tmp_path / "a.py").write_text("x = 2222\n", encoding="utf-8")
    assert content_fingerprint(tmp_path, ["a.py"]) != before


def test_file_fingerprint_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert file_fingerprint(tmp_path / "absent") == ""


def test_null_cache_never_serves_a_hit(executor: OperationExecutor) -> None:
    assert isinstance(executor.cache, NullCache)
    first = run(executor, OperationType.FIND_CLASS)
    second = run(executor, OperationType.FIND_CLASS)
    assert first.cache_hit is False
    assert second.cache_hit is False


def test_json_cache_serves_a_second_identical_request(
    python_repo: Path, tmp_path: Path
) -> None:
    cache = JsonFileCache(tmp_path / "cache")
    first = run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    second = run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert [match.symbol for match in second.matches] == [
        match.symbol for match in first.matches
    ]


def test_json_cache_misses_after_the_repository_changes(
    tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    cache = JsonFileCache(tmp_path / "cache")

    first = run(make_executor(root, cache=cache), OperationType.FIND_CLASS)
    assert [match.symbol for match in first.matches] == ["A"]

    (root / "a.py").write_text("class A:\n    pass\n\n\nclass B:\n    pass\n", encoding="utf-8")
    second = run(make_executor(root, cache=cache), OperationType.FIND_CLASS)
    assert second.cache_hit is False
    assert [match.symbol for match in second.matches] == ["A", "B"]


def test_json_cache_ignores_a_corrupt_entry(python_repo: Path, tmp_path: Path) -> None:
    cache = JsonFileCache(tmp_path / "cache")
    run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    for entry in (tmp_path / "cache").glob("*.json"):
        entry.write_text("{not json", encoding="utf-8")
    result = run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    assert result.cache_hit is False
    assert result.matches


def test_json_cache_expires_entries(python_repo: Path, tmp_path: Path) -> None:
    cache = JsonFileCache(tmp_path / "cache", ttl_seconds=-1)
    run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    result = run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    assert result.cache_hit is False


def test_failed_results_are_not_cached(python_repo: Path, tmp_path: Path) -> None:
    cache = JsonFileCache(tmp_path / "cache")
    run(make_executor(python_repo, cache=cache), OperationType.FIND_SYMBOL)
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_json_cache_clear(python_repo: Path, tmp_path: Path) -> None:
    cache = JsonFileCache(tmp_path / "cache")
    run(make_executor(python_repo, cache=cache), OperationType.FIND_CLASS)
    cache.clear()
    assert list((tmp_path / "cache").glob("*.json")) == []


# --------------------------------------------------------------------------
# Auditing and evidence
# --------------------------------------------------------------------------


def test_operations_are_audited(
    executor: OperationExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="zdocs.operations"):
        run(executor, OperationType.FIND_CLASS, symbol="Circle")
    record = next(r for r in caplog.records if r.name == "zdocs.operations")
    message = record.getMessage()
    assert "operation=find_class" in message
    assert "status=success" in message
    assert "duration_ms=" in message
    assert "matches=1" in message


def test_audit_log_redacts_arguments(
    executor: OperationExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="zdocs.operations"):
        run(executor, OperationType.FIND_SYMBOL, symbol="ghp_abcdefghijklmnopqrstuv")
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "ghp_abcdefghijklmnopqrstuv" not in message
    assert "[REDACTED]" in message


def test_evidence_is_attached_and_capped(python_repo: Path) -> None:
    executor = make_executor(python_repo, evidence_limit=2)
    result = run(executor, OperationType.FIND_CLASS)
    assert len(result.matches) > 2
    assert len(result.evidence) == 2
    assert result.evidence[0].file_path.endswith(".py")
    assert result.evidence[0].excerpt


def test_evidence_excerpts_are_redacted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.py").write_text(
        'class Settings:\n    api_key = "sk-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8"
    )
    result = run(make_executor(root), OperationType.FIND_CLASS)
    assert "sk-abcdefghijklmnopqrstuvwx" not in result.evidence[0].excerpt
    assert "[REDACTED]" in result.evidence[0].excerpt


def test_duration_is_recorded(executor: OperationExecutor) -> None:
    assert run(executor, OperationType.ANALYZE_OOP).duration_ms >= 0


def test_repository_name_is_echoed(executor: OperationExecutor) -> None:
    assert run(executor, OperationType.DETECT_LANGUAGES).repository == "python_repo"


def test_executor_rejects_a_missing_repository(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OperationExecutor(tmp_path / "absent")
