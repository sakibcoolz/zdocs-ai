"""Tests for the ADK tool layer, the agent registry and the tool-detection CLI.

No LLM calls: agents are constructed and their tool schemas inspected, and the
underlying tool functions are invoked directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import (
    AGENT_BUILDERS,
    REPOSITORY_OPERATIONS_AGENT,
    available_agents,
    build_agent_by_name,
    build_repository_operations_agent,
)
from agents.repository_operations_agent import SYSTEM_INSTRUCTION
from operations.cli import main as cli_main
from operations.schemas import OperationType
from operations.tool_detection import (
    TOOL_SPECS,
    ToolLevel,
    ToolStatus,
    detect_tools,
    format_report,
    missing_required,
)
from tools.repository_operations import (
    MAX_TOOL_MATCHES,
    ExecutorProvider,
    RepositoryOperationsTool,
    run_operation,
)

#: Every operation the agent is expected to be able to reach as a tool.
EXPECTED_TOOLS = {
    "list_repository_files",
    "count_files_and_directories",
    "detect_languages",
    "find_class",
    "find_interface",
    "find_function",
    "find_method",
    "find_symbol",
    "find_references",
    "find_implementations",
    "find_inheritance",
    "find_imports",
    "find_calls",
    "read_file_range",
    "analyze_oop",
    "build_relationship_graph",
    "generate_class_diagram",
    "generate_inheritance_diagram",
    "generate_dependency_diagram",
    "git_metadata",
}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_repository_operations_agent_is_registered() -> None:
    assert REPOSITORY_OPERATIONS_AGENT == "repository_operations_agent"
    assert REPOSITORY_OPERATIONS_AGENT in available_agents()
    assert REPOSITORY_OPERATIONS_AGENT in AGENT_BUILDERS


def test_registry_only_lists_implemented_agents() -> None:
    # Placeholders for the other seven planned agents must not appear here.
    assert available_agents() == ["repository_operations_agent", "zdocs_assistant"]


def test_build_agent_by_name(python_repo: Path) -> None:
    agent = build_agent_by_name(REPOSITORY_OPERATIONS_AGENT, python_repo)
    assert agent.name == "repository_operations_agent"


def test_build_agent_by_name_rejects_unknown(python_repo: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        build_agent_by_name("architecture_agent", python_repo)
    assert "Registered agents" in str(excinfo.value)


def test_original_assistant_is_still_reachable(python_repo: Path) -> None:
    agent = build_agent_by_name("zdocs_assistant", python_repo)
    assert agent.name == "zdocs_assistant"
    assert len(agent.tools) == 2


# --------------------------------------------------------------------------
# Tool registration
# --------------------------------------------------------------------------


def test_operations_agent_registers_every_structured_tool(python_repo: Path) -> None:
    agent = build_repository_operations_agent(python_repo, repository="python_repo")
    assert {tool.name for tool in agent.tools} == EXPECTED_TOOLS


def test_tool_factory_returns_function_tools(python_repo: Path) -> None:
    from google.adk.tools import FunctionTool

    tools = RepositoryOperationsTool(python_repo)
    assert tools
    assert all(isinstance(tool, FunctionTool) for tool in tools)
    assert all(tool.description for tool in tools)


def test_no_tool_can_run_an_arbitrary_command(python_repo: Path) -> None:
    names = {tool.name for tool in RepositoryOperationsTool(python_repo)}
    for forbidden in ("run", "exec", "shell", "bash", "command", "run_static_analysis"):
        assert forbidden not in names


def test_every_tool_maps_to_an_approved_operation(python_repo: Path) -> None:
    approved = {operation.value for operation in OperationType}
    for name in EXPECTED_TOOLS:
        # Tool names either match an operation directly or are a documented alias.
        assert name in approved or name in {"find_calls"}


def test_tool_declarations_expose_no_internal_parameters(python_repo: Path) -> None:
    for tool in RepositoryOperationsTool(python_repo):
        declaration = tool._get_declaration()
        properties = (declaration.parameters.properties or {}) if declaration.parameters else {}
        assert "stage_dir" not in properties
        assert "repo_dir" not in properties
        assert "policy" not in properties
        assert "executor" not in properties


def test_agent_instruction_states_the_security_rules() -> None:
    lowered = SYSTEM_INSTRUCTION.lower()
    assert "untrusted" in lowered
    assert "you have no shell" in lowered
    assert "evidence" in lowered
    assert "confidence" in lowered


# --------------------------------------------------------------------------
# Tool behaviour
# --------------------------------------------------------------------------


@pytest.fixture()
def provider(python_repo: Path, tmp_path: Path) -> ExecutorProvider:
    return ExecutorProvider(python_repo, repository="python_repo", docs_root=tmp_path)


def test_run_operation_returns_a_compact_payload(provider: ExecutorProvider) -> None:
    payload = run_operation(provider, OperationType.FIND_CLASS, symbol="Circle")
    assert payload["status"] == "success"
    assert payload["operation"] == "find_class"
    assert payload["match_count"] == 1
    assert payload["matches"][0]["symbol"] == "Circle"
    assert payload["matches"][0]["confidence"] == "high"
    # Must be JSON-serialisable: it goes straight into a model's context.
    json.dumps(payload)


def test_run_operation_trims_long_lists(provider: ExecutorProvider) -> None:
    payload = run_operation(provider, OperationType.FIND_METHOD)
    assert len(payload["matches"]) <= MAX_TOOL_MATCHES


def test_tool_reports_a_policy_violation_instead_of_raising(
    provider: ExecutorProvider,
) -> None:
    payload = run_operation(
        provider,
        OperationType.READ_FILE_RANGE,
        file_path="../../etc/passwd",
    )
    assert payload["status"] == "failed"
    assert payload["errors"]
    assert "traversal" in payload["errors"][0].lower()


def test_provider_reuses_an_executor_for_an_unchanged_repository(
    provider: ExecutorProvider,
) -> None:
    assert provider.get() is provider.get()


def test_provider_rebuilds_after_the_repository_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    provider = ExecutorProvider(root, repository="repo")

    first = run_operation(provider, OperationType.FIND_CLASS)
    assert [match["symbol"] for match in first["matches"]] == ["A"]

    (root / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    second = run_operation(provider, OperationType.FIND_CLASS)
    assert [match["symbol"] for match in second["matches"]] == ["A", "B"]


def test_tools_are_bound_to_one_repository(python_repo: Path, go_repo: Path) -> None:
    python_provider = ExecutorProvider(python_repo, repository="python_repo")
    payload = run_operation(python_provider, OperationType.LIST_REPOSITORY_FILES)
    paths = {entry["path"] for entry in payload["data"]["files"]}
    assert paths == {"shapes.py", "registry.py"}
    assert not any("store.go" in path for path in paths)


# --------------------------------------------------------------------------
# Tool detection
# --------------------------------------------------------------------------


def test_detects_every_known_tool() -> None:
    reports = detect_tools()
    assert {report.spec.name for report in reports} == {
        spec.name for spec in TOOL_SPECS
    }
    assert all(isinstance(report.status, ToolStatus) for report in reports)


def test_no_required_tool_blocks_startup() -> None:
    # Every external tool must have a fallback or be optional, so that a bare
    # machine can still run the application.
    assert missing_required() == []


def test_tools_are_classified_by_level() -> None:
    levels = {spec.name: spec.level for spec in TOOL_SPECS}
    assert levels["rg"] is ToolLevel.RECOMMENDED
    assert levels["git"] is ToolLevel.RECOMMENDED
    # tree-sitter is recommended, not optional: it is the difference between
    # medium- and high-confidence results for four of the five languages.
    assert levels["tree-sitter"] is ToolLevel.RECOMMENDED
    assert levels["ast-grep"] is ToolLevel.OPTIONAL
    assert levels["ctags"] is ToolLevel.OPTIONAL


def test_tools_without_a_fallback_are_reported_as_such() -> None:
    reports = {report.spec.name: report for report in detect_tools()}
    git = reports["git"]
    assert git.spec.fallback is None
    assert git.status in (ToolStatus.INSTALLED, ToolStatus.MISSING_NO_FALLBACK)
    ripgrep = reports["rg"]
    assert ripgrep.spec.fallback
    assert ripgrep.status in (ToolStatus.INSTALLED, ToolStatus.MISSING_WITH_FALLBACK)


def test_every_probed_tool_is_also_allowlisted() -> None:
    from operations.policy import ANALYSIS_COMMANDS

    assert {spec.name for spec in TOOL_SPECS} <= set(ANALYSIS_COMMANDS)


def test_report_is_human_readable() -> None:
    report = format_report()
    assert "TOOL" in report
    assert "LEVEL" in report
    for spec in TOOL_SPECS:
        assert spec.name in report


def test_report_names_the_active_backend_per_language() -> None:
    from operations.languages import analyzer_backends

    report = format_report()
    assert "Active analyzer backend per language" in report
    for language, backend in analyzer_backends().items():
        assert language in report
        assert backend in report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_tools_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["tools"]) == 0
    assert "tool check" in capsys.readouterr().out


def test_cli_tools_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["tools", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {entry["name"] for entry in payload} == {spec.name for spec in TOOL_SPECS}


def test_cli_operations_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["operations", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "analyze_oop" in payload["operations"]
    assert "run_static_analysis" not in payload["operations"]


def test_cli_analyze(python_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["analyze", str(python_repo)]) == 0
    out = capsys.readouterr().out
    assert "repository: python_repo" in out
    assert "polymorphic abstractions" in out
    assert "Shape <- Circle, Square" in out


def test_cli_analyze_json(go_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["analyze", str(go_repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["data"]["polymorphism"] == {"Store": ["MemoryStore"]}


def test_cli_analyze_unknown_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["analyze", "no-such-repo-anywhere"]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_generates_diagrams(
    java_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["diagrams", str(java_repo), "--out", str(tmp_path), "--docs"]) == 0
    out = capsys.readouterr().out
    assert "class-diagram.mmd" in out
    written = tmp_path / "java_repo"
    assert (written / "diagrams" / "class-diagram.mmd").exists()
    assert (written / "OOP_ANALYSIS.md").exists()
    assert (written / "CLASS_CATALOG.md").exists()
    assert (
        "classDiagram"
        in (written / "diagrams" / "class-diagram.mmd").read_text(encoding="utf-8")
    )


def test_cli_diagram_documents_record_confidence(java_repo: Path, tmp_path: Path) -> None:
    cli_main(["diagrams", str(java_repo), "--out", str(tmp_path), "--docs"])
    report = (tmp_path / "java_repo" / "INTERFACE_IMPLEMENTATIONS.md").read_text(
        encoding="utf-8"
    )
    assert "Confidence" in report
    assert "AbstractShape" in report
    # Whichever backend is active, the detection method must be recorded.
    assert "tree_sitter" in report or "lexical_parse" in report
