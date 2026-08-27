"""Tests for the relationship graph and Mermaid diagram generation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.diagram_generator import (
    diagrams_to_markdown,
    escape_label,
    generate_class_diagram,
    generate_class_diagrams,
    generate_component_diagram,
    generate_inheritance_diagram,
    generate_package_dependency_diagram,
    generate_sequence_diagram,
    sanitize_identifier,
)
from operations.oop_analyzer import RepositoryAnalysis, analyze_repository
from operations.policy import ExecutionPolicy, resolve_repo_root
from operations.relationship_graph import RelationshipGraph, build_graph
from operations.schemas import (
    Confidence,
    DetectionMethod,
    RelationshipInfo,
    RelationType,
    SymbolInfo,
    SymbolType,
)


def analyze(path: Path) -> RepositoryAnalysis:
    return analyze_repository(
        resolve_repo_root(path),
        ExecutionPolicy.repository_analysis(),
        StubCommandRunner(),  # type: ignore[arg-type]
        repository=path.name,
    )


@pytest.fixture()
def java_graph(java_repo: Path) -> RelationshipGraph:
    return build_graph(analyze(java_repo), include_calls=False)


def synthetic_analysis(type_count: int, *, package_count: int = 1) -> RepositoryAnalysis:
    """Build an analysis with a known number of types in a chain of inheritance."""
    analysis = RepositoryAnalysis(repository="synthetic")
    for index in range(type_count):
        package = f"pkg{index % package_count}"
        analysis.symbols.append(
            SymbolInfo(
                name=f"Type{index}",
                symbol_type=SymbolType.CLASS,
                file_path=f"{package}/type{index}.py",
                line=1,
                language="python",
                package=package,
            )
        )
        if index:
            analysis.relationships.append(
                RelationshipInfo(
                    source=f"Type{index}",
                    target=f"Type{index - 1}",
                    relation=RelationType.INHERITS,
                    file_path=f"{package}/type{index}.py",
                    line=1,
                    language="python",
                    confidence=Confidence.HIGH,
                )
            )
    return analysis


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------


def test_graph_has_a_node_per_declared_type(java_graph: RelationshipGraph) -> None:
    assert {node.name for node in java_graph.type_nodes()} == {
        "Measurable",
        "Drawable",
        "AbstractShape",
        "Rectangle",
        "Logger",
    }


def test_graph_resolves_declared_relationships(java_graph: RelationshipGraph) -> None:
    edges = {
        (edge.source_name, edge.target_name, edge.relation.value)
        for edge in java_graph.edges
        if edge.resolution == "resolved"
    }
    assert ("Rectangle", "AbstractShape", "INHERITS") in edges
    assert ("AbstractShape", "Drawable", "IMPLEMENTS") in edges


def test_unresolved_targets_become_external_nodes(java_graph: RelationshipGraph) -> None:
    external = {node.name for node in java_graph.nodes if node.external}
    assert "java.util.List" in external
    unresolved = [edge for edge in java_graph.edges if edge.resolution == "unresolved"]
    assert unresolved
    # Detection confidence is independent of whether the name resolved.
    assert any(edge.confidence is Confidence.HIGH for edge in unresolved)


def test_ambiguous_names_are_reported_not_guessed() -> None:
    analysis = RepositoryAnalysis(repository="ambiguous")
    for package in ("alpha", "beta"):
        analysis.symbols.append(
            SymbolInfo(
                name="Shared",
                symbol_type=SymbolType.CLASS,
                file_path=f"{package}/shared.py",
                line=1,
                language="python",
                package=package,
            )
        )
    analysis.symbols.append(
        SymbolInfo(
            name="Client",
            symbol_type=SymbolType.CLASS,
            file_path="client.py",
            line=1,
            language="python",
            package="root",
        )
    )
    analysis.relationships.append(
        RelationshipInfo(
            source="Client",
            target="Shared",
            relation=RelationType.INHERITS,
            file_path="client.py",
            line=1,
            language="python",
        )
    )
    graph = build_graph(analysis)
    edge = next(edge for edge in graph.edges if edge.source_name == "Client")
    assert edge.resolution == "ambiguous"
    assert edge.target_id is None
    assert len(edge.candidates) == 2
    assert graph.warnings


def test_calls_can_be_excluded(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    with_calls = build_graph(analysis, include_calls=True)
    without_calls = build_graph(analysis, include_calls=False)
    assert with_calls.edges_of([RelationType.CALLS])
    assert without_calls.edges_of([RelationType.CALLS]) == []


def test_field_access_is_excluded_by_default(python_repo: Path) -> None:
    graph = build_graph(analyze(python_repo))
    assert graph.edges_of([RelationType.READS, RelationType.WRITES]) == []
    with_access = build_graph(analyze(python_repo), include_field_access=True)
    assert with_access.edges_of([RelationType.READS])


def test_duplicate_edges_are_collapsed() -> None:
    analysis = synthetic_analysis(2)
    analysis.relationships.append(analysis.relationships[0].model_copy())
    graph = build_graph(analysis)
    assert len(graph.edges_of([RelationType.INHERITS])) == 1


def test_stats_are_self_consistent(java_graph: RelationshipGraph) -> None:
    stats = java_graph.stats()
    assert stats["node_count"] == len(java_graph.nodes)
    assert stats["edge_count"] == len(java_graph.edges)
    assert sum(stats["edges_by_relation"].values()) == len(java_graph.edges)


def test_packages_group_internal_nodes(java_graph: RelationshipGraph) -> None:
    assert "com.example" in java_graph.packages()


def test_package_dependency_edges_are_weighted(all_fixtures_repo: Path) -> None:
    graph = build_graph(analyze(all_fixtures_repo), include_calls=False)
    internal = set(graph.packages())
    dependencies = [
        edge for edge in graph.package_dependency_edges() if edge[1] in internal
    ]
    assert ("python_repo.registry", "python_repo.shapes", 1) in dependencies


# --------------------------------------------------------------------------
# Limits and splitting
# --------------------------------------------------------------------------


def test_limited_graph_keeps_the_most_connected_nodes() -> None:
    graph = build_graph(synthetic_analysis(30))
    limited = graph.limited(max_nodes=5, max_edges=50)
    assert len(limited.nodes) == 5
    assert len(limited.omitted_nodes) == 25


def test_limited_graph_reports_omitted_edges() -> None:
    graph = build_graph(synthetic_analysis(30))
    limited = graph.limited(max_nodes=30, max_edges=3)
    assert len(limited.edges) == 3
    assert limited.omitted_edge_count >= 26


def test_limiting_is_a_no_op_when_under_the_cap() -> None:
    graph = build_graph(synthetic_analysis(3))
    assert graph.limited(max_nodes=100, max_edges=100) is graph


def test_limiting_is_deterministic() -> None:
    graph = build_graph(synthetic_analysis(30))
    first = graph.limited(max_nodes=7, max_edges=10)
    second = graph.limited(max_nodes=7, max_edges=10)
    assert [node.id for node in first.nodes] == [node.id for node in second.nodes]


def test_split_by_package_produces_bounded_chunks() -> None:
    graph = build_graph(synthetic_analysis(20, package_count=4))
    chunks = graph.split_by_package(max_nodes=3)
    assert len(chunks) > 4
    assert all(len(chunk.nodes) <= 3 for _name, chunk in chunks)


def test_subgraph_records_what_it_dropped() -> None:
    graph = build_graph(synthetic_analysis(5))
    keep = {graph.nodes[0].id}
    sub = graph.subgraph(keep)
    assert len(sub.nodes) == 1
    assert len(sub.omitted_nodes) == 4


# --------------------------------------------------------------------------
# Mermaid: sanitizing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Circle", "Circle"),
        ("com.example.Circle", "com_example_Circle"),
        ("List<Foo>", "List_Foo"),
        ("my-class", "my_class"),
        ("#private", "private"),
        ("9Lives", "n_9Lives"),
        ("", "unnamed"),
        ("   ", "unnamed"),
        ("!!!", "unnamed"),
    ],
)
def test_sanitize_identifier(raw: str, expected: str) -> None:
    assert sanitize_identifier(raw) == expected


def test_sanitized_identifiers_are_mermaid_safe() -> None:
    for raw in ("a b", "a{b}", 'a"b', "a|b", "a;b", "a\nb", "class"):
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sanitize_identifier(raw))


def test_sanitize_caps_length() -> None:
    assert len(sanitize_identifier("A" * 500)) == 64


def test_escape_label_strips_mermaid_syntax() -> None:
    assert '"' not in escape_label('say "hi"')
    assert "|" not in escape_label("a|b")
    assert "\n" not in escape_label("a\nb")


def test_escape_label_redacts_secrets() -> None:
    assert "[REDACTED]" in escape_label('api_key = "sk-abcdefghijklmnopqrstuvwx"')


# --------------------------------------------------------------------------
# Mermaid: diagrams
# --------------------------------------------------------------------------


def test_class_diagram_is_valid_mermaid(java_graph: RelationshipGraph) -> None:
    diagram = generate_class_diagram(java_graph)
    assert diagram.mermaid.startswith("classDiagram")
    assert "class Rectangle {" in diagram.mermaid
    assert "AbstractShape <|-- Rectangle" in diagram.mermaid
    assert diagram.mermaid.count("{") == diagram.mermaid.count("}")


def test_class_diagram_marks_interfaces_and_abstractions(java_graph: RelationshipGraph) -> None:
    mermaid = generate_class_diagram(java_graph).mermaid
    assert "<<interface>>" in mermaid
    assert "<<abstract>>" in mermaid


def test_class_diagram_shows_members_with_visibility(java_graph: RelationshipGraph) -> None:
    mermaid = generate_class_diagram(java_graph).mermaid
    assert "+draw()" in mermaid
    assert "#name" in mermaid  # protected field
    assert "-logger" in mermaid  # private field


def test_class_diagram_caps_member_count(java_graph: RelationshipGraph) -> None:
    mermaid = generate_class_diagram(java_graph, max_members=1).mermaid
    assert "more members not shown" in mermaid


def test_class_diagram_labels_inferred_edges(python_repo: Path) -> None:
    graph = build_graph(analyze(python_repo), include_calls=False)
    mermaid = generate_class_diagram(graph).mermaid
    assert "medium confidence" in mermaid


def test_class_diagram_reports_omitted_types() -> None:
    graph = build_graph(synthetic_analysis(30))
    diagram = generate_class_diagram(graph, max_nodes=5)
    assert diagram.node_count <= 5
    assert diagram.omitted_nodes
    assert any("omitted" in warning for warning in diagram.warnings)


def test_large_graph_splits_into_several_diagrams() -> None:
    graph = build_graph(synthetic_analysis(24, package_count=4))
    diagrams = generate_class_diagrams(graph, max_nodes=6)
    assert len(diagrams) > 1
    assert len({diagram.filename for diagram in diagrams}) == len(diagrams)
    assert all(diagram.mermaid.startswith("classDiagram") for diagram in diagrams)


def test_small_graph_stays_one_diagram(java_graph: RelationshipGraph) -> None:
    assert len(generate_class_diagrams(java_graph, max_nodes=60)) == 1


def test_split_by_package_can_be_forced(java_graph: RelationshipGraph) -> None:
    diagrams = generate_class_diagrams(java_graph, split_by_package=True)
    assert all("class-diagram-" in diagram.filename for diagram in diagrams)


def test_inheritance_diagram_only_shows_hierarchy(java_graph: RelationshipGraph) -> None:
    diagram = generate_inheritance_diagram(java_graph)
    assert diagram.mermaid.startswith("classDiagram")
    assert "AbstractShape <|-- Rectangle" in diagram.mermaid
    assert "Drawable <|.. AbstractShape" in diagram.mermaid
    assert "*--" not in diagram.mermaid  # composition belongs to the class diagram


def test_go_structural_implementation_appears_in_the_diagram(go_repo: Path) -> None:
    graph = build_graph(analyze(go_repo), include_calls=False)
    mermaid = generate_inheritance_diagram(graph).mermaid
    assert "Store <|.. MemoryStore" in mermaid
    assert "ReadOnlyStore" not in mermaid.split("Store <|.. MemoryStore")[1]


def test_package_dependency_diagram(all_fixtures_repo: Path) -> None:
    graph = build_graph(analyze(all_fixtures_repo), include_calls=False)
    diagram = generate_package_dependency_diagram(graph)
    assert diagram.mermaid.startswith("flowchart LR")
    assert "python_repo_registry --> python_repo_shapes" in diagram.mermaid


def test_package_dependency_diagram_excludes_external_by_default(java_graph: RelationshipGraph) -> None:
    diagram = generate_package_dependency_diagram(java_graph)
    assert "java.util.List" not in diagram.mermaid
    with_external = generate_package_dependency_diagram(java_graph, include_external=True)
    assert "java.util.List" in with_external.mermaid


def test_component_diagram_groups_by_package(java_graph: RelationshipGraph) -> None:
    diagram = generate_component_diagram(java_graph)
    assert diagram.mermaid.startswith("flowchart TB")
    assert "subgraph" in diagram.mermaid
    assert diagram.mermaid.count("subgraph") == diagram.mermaid.count("    end")


def test_sequence_diagram_from_call_evidence(python_repo: Path) -> None:
    graph = build_graph(analyze(python_repo), include_calls=True)
    diagram = generate_sequence_diagram(graph, start_symbol="Shape.describe")
    assert diagram.mermaid.startswith("sequenceDiagram")
    assert "participant" in diagram.mermaid
    assert diagram.edge_count > 0


def test_sequence_diagram_refuses_to_invent_evidence(python_repo: Path) -> None:
    graph = build_graph(analyze(python_repo), include_calls=True)
    diagram = generate_sequence_diagram(graph, start_symbol="NoSuchSymbol")
    assert diagram.edge_count == 0
    assert any("no calls evidence" in warning.lower() for warning in diagram.warnings)


def test_diagrams_to_markdown_wraps_in_mermaid_fences(java_graph: RelationshipGraph) -> None:
    markdown = diagrams_to_markdown([generate_class_diagram(java_graph)])
    assert "```mermaid" in markdown
    assert markdown.count("```") == 2


def test_empty_graph_still_produces_valid_diagrams() -> None:
    empty = RelationshipGraph(repository="empty")
    for diagram in (
        generate_class_diagram(empty),
        generate_inheritance_diagram(empty),
        generate_package_dependency_diagram(empty),
        generate_component_diagram(empty),
    ):
        assert diagram.mermaid.splitlines()[0] in (
            "classDiagram",
            "flowchart LR",
            "flowchart TB",
        )


def test_pathological_symbol_names_do_not_break_mermaid() -> None:
    analysis = RepositoryAnalysis(repository="weird")
    for name in ('Foo"Bar', "Baz<T>", "a-b-c", "class"):
        analysis.symbols.append(
            SymbolInfo(
                name=name,
                symbol_type=SymbolType.CLASS,
                file_path="weird.py",
                line=1,
                language="python",
                package="weird",
                detection_method=DetectionMethod.PYTHON_AST,
            )
        )
    analysis.relationships.append(
        RelationshipInfo(
            source='Foo"Bar',
            target="Baz<T>",
            relation=RelationType.INHERITS,
            file_path="weird.py",
            line=1,
            language="python",
        )
    )
    mermaid = generate_class_diagram(build_graph(analysis)).mermaid
    assert '"' not in mermaid.replace('note for', '').split("\n")[0]
    for line in mermaid.splitlines():
        if line.strip().startswith("class ") and line.strip().endswith("{"):
            identifier = line.strip().split()[1]
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier)
